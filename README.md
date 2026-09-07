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
- **Telegram 双向控制** — 不只是推送，还能用 `/add` `/list` `/buy` `/pause` 直接遥控
- **7 个注册商适配器** — NameSilo / Dynadot / GoDaddy / Namecheap / 阿里云 / 演练模式 / 任意外部脚本
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

### 3. 能用的命令

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
└── registrars/         注册商适配器（7 个）
```

---

## 开发

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest              # 188 个测试，全部离线，约 5 秒
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
