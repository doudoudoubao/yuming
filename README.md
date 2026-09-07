# 域名监控 & 自动抢注

盯住你想要的域名，一旦被释放就立刻下单注册，全程 Telegram 推送和遥控。

用 **RDAP**（WHOIS 的官方继任者，返回 JSON，不用装 `whois` 命令）判断状态，
用 **DNS 直连注册局权威服务器**做冲刺阶段的高频探测，
用**注册商 API** 完成下单。

---

## 它能做什么

- **全生命周期跟踪** — 正常注册 → 到期 → 续费宽限期 → 赎回期 → pendingDelete → 释放，每一步状态变化都推送
- **自动预测释放时间** — 从 `pendingDelete` 起点推算，精确到小时级；据此自动决定何时提速
- **分档提速轮询** — 平时 6 小时一次，临近释放 1 分钟一次，最后 5 分钟进入亚秒级冲刺
- **释放瞬间抢注** — 并发 + 重试下单，抢到立刻通知
- **Telegram 双向控制** — 直接发域名即可加监控，另有 `/list` `/buy` `/pause` 等命令
- **误报防护** — 查询失败绝不当可注册；可疑的状态跳变会自动复核后再行动
- **7 个注册商适配器** — NameSilo / Dynadot / GoDaddy / Namecheap / 阿里云 / 演练模式 / 任意外部脚本
- **多通道比价 + 并发抢** — 下单前并发问每家要价挑最便宜的；冲刺时同时向多家下单提高命中率
- **一堆防误操作的闸门** — 价格上限、每日预算、演练模式、TG 二次确认（详见[安全闸门](#安全闸门)）

---

## 5 分钟上手

```bash
git clone https://github.com/doudoudoubao/yuming.git
cd yuming
pip install -r requirements.txt

# 生成配置文件
python -m domain_monitor init config.yaml

# 改 config.yaml：填上你想盯的域名
# 然后自检一下，确认 RDAP / 注册商 / Telegram 都通
python -m domain_monitor test

# 先只监控、不下单，跑起来看看
python -m domain_monitor run
```

默认配置是**只监控不下单**（`purchase.enabled: false`），
先跑通再考虑开抢注 —— 见下面的[开启真实下单](#开启真实下单)。

---

## 工作原理

### 分档提速

RDAP 服务器都有速率限制，打太猛会被限流甚至封 IP。所以按「距离预测释放还有多久」分档：

| 档位 | 触发条件 | 查询间隔 | 用什么查 |
|------|---------|---------|---------|
| `idle` | 距释放 > 3 天 | 6 小时 | RDAP |
| `watch` | 距释放 < 3 天 | 30 分钟 | RDAP |
| `near` | 距释放 < 2 小时 | 1 分钟 | RDAP |
| `sprint` | 距释放 < 5 分钟 | 0.5 秒 | **DNS 探测** |

冲刺阶段之所以换成 DNS，是因为它便宜得多：直接问 `.com` 的权威服务器
要目标域名的 NS 记录，`NXDOMAIN` 就说明注册局里已经没这条委派了 ——
这是域名被删除的强信号，而且不会被 RDAP 限流。

> ⚠️ DNS 探测是**信号不是结论**：注册着但没设 NS 的域名同样返回 NXDOMAIN。
> 所以命中之后直接让注册商 API 去做最终判定 —— 它才是唯一的裁判，
> 而且顺手就把单下了。如果域名其实还没释放，注册商只会返回一个错误，代价极低。

### 释放时间怎么算

gTLD 的标准删除流程（ICANN 到期恢复政策）：

```
到期 ──45天续费宽限期──> ──30天赎回期──> ──5天 pendingDelete──> 释放
```

预测优先级从准到糙：

1. **观测到 `pendingDelete`** → 起点 + 5 天（误差小时级，最可靠）
2. **观测到赎回期** → 起点 + 35 天
3. **只知道到期时间** → 到期 + 80 天（很粗，只用来决定什么时候开始盯紧）

各 TLD 的周期不一样，可以在 `lifecycle.tlds` 里单独配。

---

## Telegram 配置

### 1. 建机器人

1. Telegram 里找 [@BotFather](https://t.me/BotFather)，发 `/newbot`
2. 起个名字，拿到形如 `123456789:AAxxxxxxxxxxxxxxxxxxxxx` 的 token
3. 找 [@userinfobot](https://t.me/userinfobot) 拿到你自己的数字 user id
4. **主动给你的机器人发一句 `/start`** —— 不这么做机器人没法给你发消息

### 2. 写进配置

```yaml
telegram:
  enabled: true
  bot_token: "${TG_BOT_TOKEN}"      # 放环境变量，别写进文件
  chat_id: "${TG_CHAT_ID}"
  allowed_user_ids: [你的user_id]    # 只有名单里的人能发命令
  commands: true
```

```bash
export TG_BOT_TOKEN="123456789:AAxx..."
export TG_CHAT_ID="你的user_id"
python -m domain_monitor test        # 会给你发一条测试消息
```

### 3. 直接发域名就能加监控

不用打命令，把域名扔给机器人就行，一次发多个也可以：

```
你：  mydream.com
机器人：✅ 已加入监控：mydream.com

你：  a.com b.net，c.io
机器人：✅ 已加入监控：a.com / b.net / c.io
```

> 🔐 **永远不要把账号、密码或 API Key 发给机器人。** 抢注不需要这些。
> 机器人识别到疑似密钥会拒绝处理、不写日志、并提醒你去吊销——
> 但最好的做法是根本别发。凭据只写在跑本程序那台服务器的环境变量里。

### 4. 能用的命令

| 命令 | 作用 |
|------|------|
| `/list` | 监控列表和当前状态 |
| `/status` | 系统运行状态、统计、今日花费 |
| `/check <域名>` | 立即查一个域名（不用加进监控） |
| `/info <域名>` | 某个监控中域名的详细信息 |
| `/add <域名> ...` | 加入监控（支持一次加多个） |
| `/del <域名>` | 移出监控 |
| `/buy <域名>` | 立即尝试注册 |
| `/pause` / `/resume` | 暂停 / 恢复自动抢注（仍继续监控） |
| `/log [数量]` | 最近事件 |

> 通过 `/add` 加的域名存在数据库里，**不会**被配置文件的同步覆盖掉；
> 只有配置文件里删掉的域名才会被清理。

---

## 注册商配置

| provider | 下单方式 | 适合谁 | 注意 |
|----------|---------|--------|------|
| `dryrun` | 假装成功 | 调试链路 | **默认值，永远不花钱** |
| `namesilo` | 账户余额 | 便宜、API 极简 | 需预先充值 |
| `dynadot` | 账户余额 | 响应快，抢注常用 | 后台开 API + IP 白名单 |
| `godaddy` | 绑定的支付方式 | 域名多的老账号 | 生产 API 有账户门槛，先用 `ote: true` 沙箱 |
| `namecheap` | 账户余额 | 综合性价比 | **必须把出口 IP 加进白名单** |
| `aliyun` | 账户余额 | 国内用户、`.cn` | 需实名信息模板 ID |
| `exec` | 你自己的脚本 | 接任何没内置的注册商 | 见下 |

具体每家要填哪些字段，`config.example.yaml` 里都有注释好的示例。
可以只配一家（`registrar:`），也可以配一组做比价和多通道抢注（`registrars:`，见[比价与多通道](#比价与多通道)）。

### 用 `exec` 接自定义注册商

不经过 shell（没有命令注入面），参数和环境变量一起传：

```yaml
registrar:
  provider: exec
  options:
    command: ["/opt/scripts/buy.sh"]
    timeout: 30
```

脚本约定：

- 域名同时作为**第一个位置参数**和环境变量 `DM_DOMAIN` 传入
- 另有 `DM_YEARS` `DM_MAX_PRICE` `DM_PRIVACY` `DM_AUTO_RENEW` `DM_NAMESERVERS`
- 退出码 `0` = 成功，`2` = **不可重试的硬错误**（会立刻停止抢注），其它 = 可重试失败
- stdout 输出 JSON 会被解析：`{"success":true,"order_id":"X","price":9.9}`

```sh
#!/bin/sh
# 最小示例
if my-registrar-cli register "$DM_DOMAIN" --years "$DM_YEARS"; then
  echo '{"success":true,"order_id":"'"$ORDER"'","price":9.99}'
else
  echo '{"success":false,"message":"domain not available"}'
  exit 1
fi
```

---

## 抢注要准备什么

只想**监控 + 通知**的话：填好域名、开好 Telegram 就够了，下面这些都不用。

要真的**自动下单**，得先做完这几步——都在你自己这边，
Telegram 只负责收通知和发指令，**全程不需要通过它传任何凭据**：

**① 选一家（或几家）注册商，去官网开户**
挑哪家可以先看[比价站](#关于比价网站米情局--哪煮米--tld-list)。
注册 `.cn` 等国别域名还需要完成实名认证。

**② 充值**
NameSilo / Dynadot / Namecheap / 阿里云都是**从账户余额扣款**。
余额不足时下单会直接失败——本程序会把这家通道熔断并告警，但钱得你提前充。

**③ 在注册商后台开 API**
生成 API Key。Dynadot、Namecheap 还要求把**服务器出口 IP 加进白名单**
（填错 IP 是最常见的失败原因）。查出口 IP：`curl ifconfig.me`。

**④ 把密钥写进服务器的环境变量**

```bash
# /etc/domain-monitor/secrets.env  （chmod 600，只有 root 能读）
TG_BOT_TOKEN=123456789:AAxx...
TG_CHAT_ID=你的user_id
NAMESILO_API_KEY=你的key
```

配置文件里只写 `${NAMESILO_API_KEY}` 这样的占位符，密钥本身不进仓库。

**⑤ 填联系人资料**（只有 GoDaddy / Namecheap 需要）
注册域名要提交注册人信息，写在 `registrar.contact` 里。阿里云用后台的
「信息模板 ID」代替，NameSilo / Dynadot 用账户默认资料。

**⑥ 自检**

```bash
python -m domain_monitor test
```

会逐个通道验证凭据是否可用、余额多少、Telegram 通不通。

**⑦ 先演练，再来真的**

```yaml
purchase:
  enabled: true
  dry_run: true      # 先这样跑几天，确认状态判断和通知都对
```

确认无误后再改 `dry_run: false`，并把 `daily_budget` 设小一点试第一单。

---

## 会不会误报？

会有，但设计上**误报只会浪费一次 API 调用，不会误买**。分开说：

### 不会发生的事

- **查询失败被当成「可注册」** — RDAP 超时、被限流、返回 5xx，一律记为「查询失败」
  并退避重试，绝不触发抢注
- **重复购买** — 同一域名成功买过就不会再买，数据库里有记录
- **买贵了** — 超过 `max_price` 直接放弃；多通道时先比价再买
- **刷爆账户** — 每日预算是硬上限

### 可能发生的事，以及怎么防的

**① DNS 探测的假阳性**
冲刺阶段靠 DNS `NXDOMAIN` 判断域名是否从注册局消失，但**注册着却没设 NS
的域名也返回 NXDOMAIN**。这是设计上就存在的，所以它只是「信号」不是「结论」——
命中后直接让注册商 API 去做最终判定。域名要是还没释放，注册商只会返回一个错误，
代价就是一次白跑的 HTTP 请求。想更稳可以设 `skip_rdap_confirm: false`，
下单前多做一次 RDAP 复核（慢一个往返）。

**② RDAP 服务器抽风返回 404**
一个还在正常注册期的域名突然 404，更可能是服务器出问题、或者兜底入口不认识
这个后缀，而不是真被删了。所以：**从「已注册」直接跳到「可注册」会自动复核一次**
（默认延迟 3 秒再查），复核不通过就当抖动忽略，不推送也不下单，并记一条
`false_positive` 事件。

走完删除流程（宽限期 → 赎回期 → pendingDelete）掉出来的属于预期内，
不复核、立刻抢——这才是真正要抢的那一瞬间，不能浪费时间。

```yaml
rdap:
  reverify_available: true   # 关掉可以省 3 秒，但误报会变多
  reverify_delay: 3.0
```

**③ 释放时间预测偏差**
`pendingDelete + 5 天` 的推算误差在小时级，但注册局不承诺精确时刻。
预测偏了也不会漏——过了预测时间还没掉，会自动退回加密监控继续等，
并按 `sprint_tail`（默认 90 分钟）在预测点之后继续冲刺。

**④ 刚加监控的域名本来就是空的**
如果你 `/add` 一个当前就没人注册的域名，且已开启真实下单，它会**立刻买下来**。
这是预期行为（首次检查为可注册 = 可信），但如果你只是想先看看，
用 `/check 域名` 查询而不是 `/add`。

---

## 比价与多通道

### 钱花在哪：`price` 命令

```
$ python -m domain_monitor price mydream.com

mydream.com
  注册商        可注册  价格            备注
  ----------------------------------------------------
  namesilo      是      8.88 USD
  dynadot       是      10.20 USD
  aliyun        是      55.00 CNY
  exec          未知    -               该注册商不支持查价
  → 最便宜且在上限(12.00)内：namesilo 8.88 USD
```

价格来自**各注册商自己的 API**，是你账户的真实成交价——含会员等级折扣、
当期促销、溢价域名加价。这比第三方比价站的挂牌价准，因为挂牌价不知道你是谁。

### 配多个通道

```yaml
registrars:
  - provider: namesilo
    options: {api_key: "${NAMESILO_API_KEY}"}
  - provider: dynadot
    options: {api_key: "${DYNADOT_API_KEY}"}
  - provider: aliyun
    options: {access_key_id: "${ALIYUN_AK}", access_key_secret: "${ALIYUN_SK}",
              registrant_profile_id: "123456"}
```

配了之后：

- **平时下单（`/buy`、RDAP 发现可注册）** → 先并发比价，把最便宜的一家提到队首再下单
- **冲刺抢注** → **不比价**，直接同时向所有通道开抢

为什么冲刺时不比价：抢注是毫秒级竞争，多几个 HTTP 往返就是把域名让给别人。
标准 TLD 各家差价通常几美元，抢到的价值远大于价差。

**多通道并发不会重复扣款** —— 同一个域名在注册局只能被注册一次，
其余通道只会收到「已被注册」的错误。多通道纯粹是多几条赛道，
这也正是专业抢注商的做法（他们握着几十上百个注册商资质）。

**单通道故障不会拖死整轮**：某家返回「余额不足 / 认证失败」这类硬错误时，
只把**那一家**摘掉，其余通道继续抢；全部通道都废了才停止并告警。

> 前提是每家都**事先**开好户、充好值、配好 API。抢注时来不及现场注册账号，
> 所以「哪家最便宜」这个选型决定要提前做 —— 见下面一节。

### 关于比价网站（米情局 / 哪煮米 / TLD-List）

[米情局](https://miqingju.com/)、[哪煮米](https://www.nazhumi.com/)、
[TLD-List](https://zh-hans.tld-list.com/) 这类站点覆盖几十家注册商的挂牌价，
适合回答**「我该去哪家开户」**——这是个一次性的人工决策，直接开网页看就行。

本项目**没有**去爬它们，原因是：

1. 它们没有公开 API，靠爬 HTML 页面，改版就断，属于长期维护负担
2. 挂牌价 ≠ 你的成交价（等级折扣、促销、续费价差异都不体现）
3. 真正决定买卖的是「你有账号的那几家现在报价多少」，
   这个问题注册商 API 能权威回答，比价站不能

所以分工是：**用比价站选注册商开户，用本项目的 `price` 命令决定这一单买哪家。**
如果你确实想把比价站数据接进来做选型辅助，可以用 `exec` 适配器或者提个 issue。

---

## 安全闸门

自动花钱的脚本最怕失控，所以设了好几道闸：

| 闸门 | 配置项 | 默认 | 作用 |
|------|--------|------|------|
| 总开关 | `purchase.enabled` | `false` | 关着就只监控只推送，绝不下单 |
| 演练模式 | `purchase.dry_run` | `true` | 走完整流程但不产生真实订单 |
| 单价上限 | `purchase.max_price` | 50 | 超过就放弃（防溢价域名天价） |
| 每日预算 | `purchase.daily_budget` | 200 | 当天累计花费超了就停手 |
| 重复购买保护 | 自动 | — | 同一域名成功买过就不会再买 |
| 二次确认 | `purchase.confirm_via_telegram` | `false` | 下单前要在 TG 点按钮（会慢几秒） |
| 运行时暂停 | `/pause` | — | 随时刹车，不用重启 |
| 硬错误熔断 | 自动 | — | 余额不足 / 认证失败立刻停，不空转 |

还有两条重要的行为约定：

- **查询失败绝不当成「可注册」。** RDAP 挂了、超时了、被限流了，状态一律记为「查询失败」并退避重试，
  绝不会因为查不到就去下单。
- **配置自相矛盾会被拒绝启动。** 比如 `dry_run: false` 却把 provider 留成 `dryrun` ——
  这种「以为自己在抢注、其实什么都没做」的组合直接报错。

### 开启真实下单

确认演练模式跑通之后：

```yaml
purchase:
  enabled: true
  dry_run: false        # <- 从这一刻起会真的花钱
  max_price: 30
  daily_budget: 100
registrar:
  provider: namesilo    # 必须是真实注册商
  options:
    api_key: "${NAMESILO_API_KEY}"
```

启动时 Telegram 会收到一条明确写着「⚠️ 真实下单」的开机通知。

---

## 关于抢注成功率（请先读这段）

**这个脚本抢不到热门域名。** 说清楚免得期望落空：

真正值钱的域名在释放的那一刻会被专业抢注商（DropCatch、SnapNames、Pheenix，
国内的阿里云 / 西部数码预订服务）拿下。他们手里握着几十上百个注册商资质，
在删除窗口对注册局的 EPP 接口每秒打出成百上千个请求。
一个走单一注册商 REST API 的脚本，在这种量级的竞争里没有胜算。

**它真正适合的场景：**

- 没人跟你抢的小众域名、个人项目名、公司旧域名 —— 这类占绝大多数，成功率相当高
- 盯着某个域名等它到期，第一时间知道状态变化（哪怕不自动买）
- 到期前的续费提醒 / 域名资产监控
- 想接手一个别人忘了续费的域名

**如果你盯的是高价值域名**，正确做法是花钱买抢注商的预订服务（backorder），
同时用这个脚本做监控和通知 —— 把 `purchase.enabled` 关掉当纯监控用就行。

---

## 命令行

```bash
python -m domain_monitor [-c 配置文件] <子命令>
```

| 子命令 | 作用 |
|--------|------|
| `run` | 启动常驻监控（默认） |
| `once` | 跑一轮就退出，适合配 cron |
| `test` | 自检 RDAP / 注册商 / Telegram / DNS |
| `check <域名>...` | 立即查询域名状态 |
| `price <域名>...` | 向注册商查价 |
| `add` / `rm` / `list` | 管理监控列表 |
| `log [-n 数量]` | 查看事件流 |
| `init [路径]` | 生成配置模板 |

```bash
# 查一下某个域名现在什么状态
python -m domain_monitor check example.com mydream.io

# 不想常驻，用 cron 每 10 分钟跑一轮
*/10 * * * * cd /opt/domain-monitor && python -m domain_monitor once
```

> 常驻模式（`run`）才有冲刺抢注能力，`once` 只做常规巡检。
> 认真想抢的话请用 `run` + systemd。

---

## 部署

### systemd（推荐）

```bash
sudo cp deploy/domain-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now domain-monitor
journalctl -u domain-monitor -f
```

密钥写在 `/etc/domain-monitor/secrets.env`（`chmod 600`），
service 文件已经配好从那里读，并做了 systemd 沙箱化。

### Docker

```bash
cd deploy
mkdir -p data && cp ../config.example.yaml data/config.yaml   # 改好配置
TG_BOT_TOKEN=xxx TG_CHAT_ID=yyy docker compose up -d
```

---

## 目录结构

```
domain_monitor/
├── cli.py              命令行入口
├── app.py              组件装配 + 优雅退出
├── engine.py           调度、状态机、抢注（核心）
├── rdap.py             RDAP 客户端（bootstrap 缓存 + 限速 + 退避）
├── dnsprobe.py         冲刺阶段的 DNS 快速探测
├── storage.py          SQLite 持久化
├── config.py           配置加载与校验
├── models.py           数据模型与生命周期推算
├── notify/telegram.py  推送 + 命令机器人 + 二次确认
└── registrars/         注册商适配器（7 个）+ pool.py 多通道比价与并发抢
```

---

## 开发

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest              # 244 个测试，全部离线，约 8 秒
```

测试用 `httpx.MockTransport` 顶掉所有网络调用，不碰真实注册商、不发真实 TG 消息。
包含一个完整的端到端用例：正常注册 → pendingDelete → 释放 → 抢注成功。

---

## 常见问题

**Q: 提示「找不到 RDAP 服务器」？**
少数后缀（尤其是一些国别域名）还没接入 RDAP。在 `rdap.overrides` 里手动指定，
或者依赖默认的 `rdap.org` 兜底跳转。

**Q: DNS 探测显示不可用？**
装一下 `pip install dnspython`。没有它冲刺阶段会退化成按 `sprint_interval`
直接问注册商，慢一些但仍然可用。

**Q: 会不会因为脚本 bug 把我账户刷爆？**
每日预算 + 单价上限 + 重复购买保护三道闸都是硬拦截，而且默认 `dry_run: true`。
建议第一次上真实注册商时把 `daily_budget` 设得很小。

**Q: 时间都是 UTC 吗？**
是。日志、`drop_at`、删除窗口全部用 UTC，避免夏令时和时区把删除窗口算错。

**Q: 能同时监控几个域名？**
几百个没问题。注意 RDAP 限速是**按服务器**算的，
监控 500 个 `.com` 会共用 Verisign 那一个令牌桶，把 `poll.idle_interval` 调大些。
