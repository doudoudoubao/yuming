"""Telegram 集成：状态推送 + 双向命令控制。

两部分：
* :class:`TelegramClient` —— Bot API 的薄封装
* :class:`TelegramBot`    —— getUpdates 长轮询，把 /add /list /buy 等命令
  转发给引擎，并支持下单前的 inline 按钮二次确认

只处理白名单内的 chat / user，其余一律忽略。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Protocol

import httpx

from ..config import TelegramConfig
from ..models import DomainState
from ..utils import (
    PatternError,
    escape_html,
    expand_pattern,
    is_valid_domain,
    normalize_domain,
    truncate,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE = 4000  # Telegram 上限 4096，留点余量

# /help 的内容按主题拆开：单条 Telegram 消息最多 4096 字符，
# 全部规则塞不进一条，所以 /help 分条发送，/help <主题> 只发对应那节。
HELP_SECTIONS: dict[str, str] = {}

HELP_SECTIONS["命令"] = """<b>📖 域名监控机器人 · 使用说明 (1/4)</b>

盯住你想要的域名，一旦被释放就立刻下单注册。

<b>━━ 查看 ━━</b>
/list — 监控列表与状态
/status — 运行状态、统计、今日花费
/check &lt;域名&gt; — 立即查一个域名（不用先加监控）
/info &lt;域名&gt; — 某个监控中域名的详情
/log [数量] — 最近事件，默认 15 条
/tlds [合集名] — 查看后缀合集

<b>━━ 管理 ━━</b>
/add &lt;域名&gt; ... — 加入监控
/del &lt;域名&gt; — 移出监控
/buy &lt;域名&gt; — 立即尝试注册
/pause — 暂停自动抢注（仍继续监控推送）
/resume — 恢复自动抢注

<b>━━ 帮助 ━━</b>
/help — 完整说明（本文，共 4 条）
/help 模式 — 批量写法
/help 抢注 — 抢注规则与安全闸门
/help 状态 — 状态含义与推送说明

<b>💡 最常用的：直接把域名发给我</b>
不用打命令，整条消息只放域名即可：
<code>mydream.com</code>
<code>a.com b.net c.io</code>"""

HELP_SECTIONS["模式"] = """<b>📖 使用说明 (2/4) · 批量写法</b>

<b>━━ 花括号 ━━</b>
<code>mydream.{com,net,io}</code> → 三个域名
<code>{short,tiny}.com</code> → 两个前缀
<code>{vps,host}.{com,io}</code> → 四个组合

<b>━━ 后缀合集 ━━</b>
<code>vps.{@all}</code> → 一次盯全部 58 个无限制后缀
<code>vps.{@two}</code> → 只要 33 个两位后缀
<code>vps.{@gtld}</code> → 只要 25 个通用后缀
<code>vps.{@two,com,net}</code> → 合集和具体后缀混写

另有两组<b>有注册限制</b>的，故意不含在 @all 里：
<code>@europe</code> ⚠️ 多数要求当地实体或居民身份
<code>@china</code> ⚠️ .cn 需要实名认证

发 /tlds 看全部合集，/tlds all 看某个合集的完整内容。

<b>━━ 抢到一个就收工 ━━</b>
同一个前缀展开出来的域名算作一组。如果配置里开了
stop_after_first（prefixes 段默认开），抢到组里任意一个之后，
其余的会自动停止监控并通知你。
「这个名字我要，哪个后缀都行」就该这么用。

<b>━━ 注意数量 ━━</b>
<code>vps.{@all}</code> 就是 58 个域名，每个都要轮询。
一条模式最多展开 200 个（pattern_limit），超了会报错。"""

HELP_SECTIONS["抢注"] = """<b>📖 使用说明 (3/4) · 抢注与安全</b>

<b>━━ 🔐 永远不要发凭据给我 ━━</b>
抢注<b>不需要</b>通过 Telegram 传任何账号、密码或 API Key。
凭据只写在跑本程序那台服务器的 .env 文件里。
我识别到疑似密钥会拒绝处理、不写日志，并提醒你去吊销。

<b>━━ 默认不会花钱 ━━</b>
purchase.enabled 默认 false：只监控只推送，绝不下单。
要开自动抢注得改配置文件，并且建议先用 dry_run 演练几天。

<b>━━ 花钱前的几道闸 ━━</b>
· 单价上限 — 超过 max_price 直接放弃
· 每日预算 — 当天累计超过 daily_budget 就停手
· 重复购买保护 — 同一域名买到过就不会再买
· /pause — 随时刹车，不用重启
· 硬错误熔断 — 余额不足 / 认证失败立刻停，不空转
· 查询失败绝不当「可注册」，只退避重试

<b>━━ 抢不到热门域名 ━━</b>
值钱的域名在释放那一刻会被专业抢注商拿走，他们握着几十上百个
注册商通道。本程序适合<b>没人跟你抢</b>的域名：小众名字、
个人项目名、别人忘了续费的域名 —— 这类占绝大多数。

<b>━━ /buy 手动下单 ━━</b>
需要配置里已开启 purchase.enabled，否则会拒绝。
不在监控列表里的域名会自动先加进去。"""

HELP_SECTIONS["状态"] = """<b>📖 使用说明 (4/4) · 状态与推送</b>

<b>━━ 域名的一生 ━━</b>
🔒 已注册 → ⏰ 已过期(宽限期) → 🩹 赎回期
→ 🔥 待删除 → 🟢 可注册 → 🎉 已抢注

gTLD 标准流程：到期后 45 天续费宽限期，30 天赎回期，
5 天 pendingDelete，然后释放。
❔ 未知 = 还没查过　⚠️ 查询失败 = RDAP 出错，会自动重试

<b>━━ 什么时候查得勤 ━━</b>
平时 6 小时一次；进入删除流程 30 分钟一次；
临近预测释放 1 分钟一次；最后 5 分钟用 DNS 高频探测。
预测释放时间从 pendingDelete 起点推算，误差在小时级。

<b>━━ 你会收到哪些推送 ━━</b>
· 状态变化（可注册、待删除会震手机，其余静音）
· 🟢 域名可以注册了
· 🎉 抢注成功 / ❌ 抢注失败
· 🧹 同组已抢到，其余停止监控
· ⚠️ 疑似误报、预算拦截、通道停用等

<b>━━ 误报怎么处理的 ━━</b>
一个还在正常注册期的域名突然查不到，更可能是服务器抽风。
这种可疑跳变会自动复核一次再当真。
走完删除流程掉出来的属于预期内，不复核、立刻抢。"""

HELP_ORDER = ["命令", "模式", "抢注", "状态"]

# 主题别名，中英文都认
HELP_ALIASES = {
    "命令": "命令", "commands": "命令", "cmd": "命令", "1": "命令",
    "模式": "模式", "pattern": "模式", "patterns": "模式", "批量": "模式", "2": "模式",
    "抢注": "抢注", "buy": "抢注", "购买": "抢注", "安全": "抢注", "3": "抢注",
    "状态": "状态", "state": "状态", "status": "状态", "推送": "状态", "4": "状态",
}

# 兼容旧引用
HELP_TEXT = HELP_SECTIONS["命令"]

BOT_COMMANDS = [
    {"command": "list", "description": "监控列表与状态"},
    {"command": "status", "description": "系统运行状态"},
    {"command": "check", "description": "立即查询一个域名"},
    {"command": "info", "description": "查看域名详情"},
    {"command": "add", "description": "加入监控"},
    {"command": "del", "description": "移出监控"},
    {"command": "buy", "description": "立即尝试注册"},
    {"command": "pause", "description": "暂停自动抢注"},
    {"command": "resume", "description": "恢复自动抢注"},
    {"command": "tlds", "description": "查看后缀合集"},
    {"command": "log", "description": "最近事件"},
    {"command": "help", "description": "完整使用说明"},
]


class Controller(Protocol):
    """引擎需要向机器人暴露的能力。"""

    async def cmd_list(self) -> str: ...
    async def cmd_status(self) -> str: ...
    async def cmd_check(self, domain: str) -> str: ...
    async def cmd_info(self, domain: str) -> str: ...
    async def cmd_add(self, domains: list[str]) -> str: ...
    async def cmd_remove(self, domain: str) -> str: ...
    async def cmd_buy(self, domain: str) -> str: ...
    async def cmd_pause(self, paused: bool) -> str: ...
    async def cmd_log(self, limit: int) -> str: ...
    async def cmd_tlds(self, name: str | None) -> str: ...


class TelegramClient:
    """Bot API 薄封装。"""

    def __init__(
        self, config: TelegramConfig, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None

    async def start(self) -> None:
        if self._client is None:
            # 长轮询要比 poll_timeout 多留一点余量
            timeout = max(self.config.timeout, self.config.poll_timeout + 15)
            self._client = httpx.AsyncClient(timeout=timeout)
            self._owns_client = True

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("TelegramClient 未初始化")
        return self._client

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.bot_token)

    async def call(self, method: str, **payload: Any) -> dict[str, Any] | None:
        """调用一个 Bot API 方法，失败只记日志不抛异常——通知不该拖垮主流程。"""
        if not self.enabled:
            return None
        url = f"{self.config.api_base}/bot{self.config.bot_token}/{method}"
        try:
            response = await self.client.post(url, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("Telegram %s 请求失败: %s", method, exc)
            return None

        try:
            data = response.json()
        except ValueError:
            logger.warning("Telegram %s 返回体异常: %s", method, response.text[:200])
            return None

        if not data.get("ok"):
            logger.warning(
                "Telegram %s 失败: %s %s",
                method,
                data.get("error_code"),
                data.get("description"),
            )
            return None
        return data.get("result")

    async def send(
        self,
        text: str,
        *,
        chat_id: str | int | None = None,
        reply_markup: dict[str, Any] | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any] | None:
        target = chat_id or self.config.chat_id
        if not target:
            logger.debug("未配置 chat_id，跳过推送")
            return None
        payload: dict[str, Any] = {
            "chat_id": target,
            "text": truncate(text, MAX_MESSAGE),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", **payload)

    async def get_me(self) -> dict[str, Any] | None:
        return await self.call("getMe")

    async def set_commands(self) -> None:
        await self.call("setMyCommands", commands=BOT_COMMANDS)

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            offset=offset,
            timeout=self.config.poll_timeout,
            allowed_updates=["message", "callback_query"],
        )
        return result or []

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def edit_text(self, chat_id: Any, message_id: int, text: str) -> None:
        await self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=truncate(text, MAX_MESSAGE),
            parse_mode="HTML",
        )


class Notifier:
    """把领域事件翻译成推送消息。"""

    def __init__(self, client: TelegramClient, config: TelegramConfig) -> None:
        self.client = client
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    def should_notify(self, state: DomainState) -> bool:
        return state.value in self.config.notify_states

    async def send(self, text: str, *, quiet: bool = False) -> None:
        await self.client.send(text, disable_notification=quiet)

    async def state_changed(
        self,
        domain: str,
        old: DomainState,
        new: DomainState,
        *,
        detail: str = "",
    ) -> None:
        if not self.should_notify(new):
            return
        lines = [
            f"{new.emoji} <b>{escape_html(domain)}</b> 状态变化",
            f"{old.label} → <b>{new.label}</b>",
        ]
        if detail:
            lines.append(escape_html(detail))
        # 只有「可注册 / 待删除」值得震手机，其余状态变化按 silent_idle 决定是否静音
        urgent = new in (DomainState.AVAILABLE, DomainState.PENDING_DELETE)
        await self.send("\n".join(lines), quiet=self.config.silent_idle and not urgent)

    async def available(self, domain: str, detail: str = "") -> None:
        text = f"🟢 <b>{escape_html(domain)} 现在可以注册了！</b>"
        if detail:
            text += f"\n{escape_html(detail)}"
        await self.send(text)

    async def acquired(self, domain: str, detail: str) -> None:
        await self.send(
            f"🎉🎉 <b>抢注成功：{escape_html(domain)}</b>\n{escape_html(detail)}"
        )

    async def failed(self, domain: str, detail: str) -> None:
        await self.send(
            f"❌ <b>{escape_html(domain)} 抢注失败</b>\n{escape_html(detail)}"
        )

    async def error(self, message: str) -> None:
        await self.send(f"⚠️ {escape_html(message)}")


class TelegramBot:
    """长轮询命令机器人。"""

    def __init__(
        self,
        client: TelegramClient,
        config: TelegramConfig,
        controller: Controller,
    ) -> None:
        self.client = client
        self.config = config
        self.controller = controller
        self._offset: int | None = None
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._stop = asyncio.Event()

    # -------------------------------------------------------------- 权限控制

    def _authorized(self, chat_id: Any, user_id: Any) -> bool:
        allowed_users = {str(item) for item in self.config.allowed_user_ids}
        if allowed_users:
            return str(user_id) in allowed_users
        if self.config.chat_id:
            return str(chat_id) == str(self.config.chat_id)
        return False

    # ------------------------------------------------------------------ 主循环

    async def run(self) -> None:
        if not self.client.enabled or not self.config.commands:
            logger.info("Telegram 命令交互未启用")
            return

        me = await self.client.get_me()
        if me:
            logger.info("Telegram 机器人已连接: @%s", me.get("username"))
            await self.client.set_commands()
        else:
            logger.warning("Telegram getMe 失败，请检查 bot_token")

        failures = 0
        while not self._stop.is_set():
            try:
                updates = await self.client.get_updates(self._offset)
                failures = 0
            except Exception as exc:  # noqa: BLE001 - 轮询循环不能被任何异常打断
                failures += 1
                delay = min(60.0, 2.0 * failures)
                logger.warning("Telegram 轮询异常: %s，%.0fs 后重试", exc, delay)
                await asyncio.sleep(delay)
                continue

            for update in updates:
                self._offset = int(update["update_id"]) + 1
                try:
                    await self._dispatch(update)
                except Exception:  # noqa: BLE001 - 单条消息处理失败不影响后续
                    logger.exception("处理 Telegram 更新失败")

    def stop(self) -> None:
        self._stop.set()

    async def _dispatch(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        message = update.get("message")
        if not message:
            return

        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        text = (message.get("text") or "").strip()
        if not text:
            return

        if not self._authorized(chat_id, user_id):
            logger.warning("忽略未授权的 Telegram 消息 chat=%s user=%s", chat_id, user_id)
            # 只对明确的命令回一句「未授权」。普通聊天一律沉默——
            # 否则机器人待在群里会把每一句闲聊都回一遍。
            if text.startswith("/"):
                await self.client.send(
                    "⛔️ 未授权。请把你的 user id 加入配置的 telegram.allowed_user_ids。\n"
                    f"你的 user id: <code>{escape_html(user_id)}</code>",
                    chat_id=chat_id,
                )
            return

        if text.startswith("/"):
            reply = await self._handle_command(text)
        else:
            reply = await self._handle_plain_text(text)

        # /help 会返回多条（单条 Telegram 消息装不下全部规则）
        for message in [reply] if isinstance(reply, str) else reply:
            if message:
                await self.client.send(message, chat_id=chat_id)

    async def _handle_command(self, text: str) -> str | list[str]:
        parts = text.split()
        command = parts[0].lstrip("/").split("@", 1)[0].lower()
        args = parts[1:]

        if command in ("start", "help", "帮助", "说明"):
            return self._help(args)
        if command == "list":
            return await self.controller.cmd_list()
        if command == "status":
            return await self.controller.cmd_status()
        if command == "check":
            if not args:
                return "用法：/check &lt;域名&gt;"
            return await self.controller.cmd_check(args[0])
        if command == "info":
            if not args:
                return "用法：/info &lt;域名&gt;"
            return await self.controller.cmd_info(args[0])
        if command == "add":
            if not args:
                return "用法：/add &lt;域名&gt; [域名2 ...]"
            return await self.controller.cmd_add(args)
        if command in ("del", "delete", "rm", "remove"):
            if not args:
                return "用法：/del &lt;域名&gt;"
            return await self.controller.cmd_remove(args[0])
        if command == "buy":
            if not args:
                return "用法：/buy &lt;域名&gt;"
            return await self.controller.cmd_buy(args[0])
        if command == "pause":
            return await self.controller.cmd_pause(True)
        if command == "resume":
            return await self.controller.cmd_pause(False)
        if command in ("tlds", "tld", "合集"):
            return await self.controller.cmd_tlds(args[0] if args else None)
        if command == "log":
            limit = 15
            if args:
                try:
                    limit = max(1, min(50, int(args[0])))
                except ValueError:
                    pass
            return await self.controller.cmd_log(limit)
        return f"未知命令 /{escape_html(command)}，发送 /help 查看可用命令"

    async def _handle_plain_text(self, text: str) -> str:
        """不带 / 的消息：能认出域名就直接加监控，否则给点提示。

        顺带拦一道凭据泄露——用户很容易顺手把 API Key 粘进聊天框，
        而抢注**从来不需要**通过 Telegram 传任何账号或密钥。
        """
        groups = getattr(self.controller, "pattern_groups", None)
        tokens = [item for item in _SEPARATORS.split(text) if item]
        # 先按「域名清单」解析。全部是裸域名才自动加监控——
        # 这既避免了从聊天里的网址/散句误提取域名，也让 token.io、apikey.com
        # 这类**合法但字面像密钥**的域名不会被下面的凭据检查拦掉。
        if tokens and len(tokens) <= 20 and all(
            _is_bare_domain(item, groups) for item in tokens
        ):
            # 原样交给 cmd_add：花括号模式要由它来展开
            return await self.controller.cmd_add(tokens)

        if _looks_like_secret(text):
            # 注意：绝不把可疑内容写进日志或回显到消息里
            logger.warning("收到疑似凭据的消息，已拒绝处理（内容未记录）")
            return (
                "🔐 <b>这看起来像密钥或密码，我不会处理它。</b>\n\n"
                "抢注<b>不需要</b>通过 Telegram 发送任何账号、密码或 API Key。\n"
                "凭据只写在运行本程序那台服务器的环境变量里。\n\n"
                "⚠️ 如果你刚刚真的发了密钥，请立刻去注册商后台<b>吊销并重新生成</b>，"
                "并删除这条消息。"
            )

        maybe = [item for item in tokens if _is_bare_domain(item, groups)][:5]
        hint = (
            "没认出域名。直接把域名发给我就能加入监控（整条消息只放域名），例如：\n"
            "<code>example.com</code>\n"
            "<code>a.com b.net c.io</code>\n\n"
            "或者发 /help 查看全部命令。"
        )
        if maybe:
            # 消息里混了别的内容，不擅自替用户决定加哪个，给出明确命令让他确认
            hint = (
                "消息里还有别的内容，没有自动添加。如果你要加这些域名，发：\n"
                f"<code>/add {escape_html(' '.join(maybe))}</code>"
            )
        return hint

    @staticmethod
    def _help(args: list[str]) -> str | list[str]:
        """不带参数就把全部规则分条发出来，带主题只发那一节。"""
        if not args:
            return [HELP_SECTIONS[key] for key in HELP_ORDER]

        wanted = args[0].strip().lower().lstrip("/")
        key = HELP_ALIASES.get(wanted)
        if key is None:
            topics = "、".join(HELP_ORDER)
            return (
                f"没有「{escape_html(args[0])}」这个主题。\n"
                f"可选：{topics}\n"
                f"直接发 /help 查看全部。"
            )
        return HELP_SECTIONS[key]

    # ---------------------------------------------------------------- 二次确认

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id"))
        user_id = (callback.get("from") or {}).get("id")
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")

        if not self._authorized(chat_id, user_id):
            await self.client.answer_callback(callback_id, "未授权")
            return

        action, _, token = data.partition(":")
        future = self._pending.get(token)
        if future is None or future.done():
            await self.client.answer_callback(callback_id, "该确认已失效")
            return

        approved = action == "ok"
        future.set_result(approved)
        await self.client.answer_callback(callback_id, "已确认" if approved else "已取消")
        if message.get("message_id"):
            await self.client.edit_text(
                chat_id,
                int(message["message_id"]),
                f"{message.get('text', '')}\n\n{'✅ 已确认下单' if approved else '🚫 已取消'}",
            )

    async def confirm_purchase(
        self, domain: str, price: float | None, currency: str, timeout: float
    ) -> bool:
        """发一条带按钮的确认消息，等待用户点击；超时视为拒绝。"""
        if not self.client.enabled:
            return False

        token = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[token] = future

        price_text = f"{price:.2f} {currency}" if price is not None else "未知"
        markup = {
            "inline_keyboard": [[
                {"text": "✅ 确认注册", "callback_data": f"ok:{token}"},
                {"text": "🚫 取消", "callback_data": f"no:{token}"},
            ]]
        }
        sent = await self.client.send(
            f"❓ <b>{escape_html(domain)}</b> 可以注册了\n"
            f"价格：{escape_html(price_text)}\n"
            f"请在 {int(timeout)} 秒内确认是否下单：",
            reply_markup=markup,
        )
        if sent is None:
            self._pending.pop(token, None)
            return False

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("%s 的下单确认超时（%.0fs），按拒绝处理", domain, timeout)
            await self.client.send(f"⌛️ <b>{escape_html(domain)}</b> 确认超时，已放弃本次下单")
            return False
        finally:
            self._pending.pop(token, None)


# 分隔符：空白 + 中英文常见标点（中文句号会被 IDNA 当成标签分隔符，必须单列）
# 分隔符：空白 + 中英文常见标点。花括号里的逗号要留着，
# 否则 mydream.{com,net} 会被切成两半。
_SEPARATORS = re.compile(r"(?![^{]*\})[\s,;:，、；：。]+")

# 常见的密钥字样。只在「整条消息不是域名清单」时才检查，
# 所以 token.io / apikey.com 这类合法域名不会走到这里。
_SECRET_HINTS = (
    "api_key", "apikey", "api key", "secret", "token", "password", "passwd",
    "access_key", "accesskey", "bearer", "私钥", "密钥", "密码", "口令",
)
# 长串随机字符：≥24 位、字母数字混合，典型的 API Key 形状
_SECRET_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")

# 裸主机名：不能带协议、路径、查询串、端口或用户名
_NOT_BARE = ("://", "/", "?", "#", "@", ":")
_BRACES = re.compile(r"\{[^{}]*\}")


def _is_bare_domain(token: str, groups: dict[str, list[str]] | None = None) -> bool:
    """是不是一个干净的域名，或者一条能展开成域名的模式。

    ``mydream.{com,net,io}`` 和 ``vps.{@two}`` 都算——
    直接发这种写法就能一次盯一批。
    """
    # 禁用字符只在花括号**之外**判断：@ 在 user@example.com 里要挡，
    # 但在 vps.{@two} 里是合集引用，不能一起误伤。
    outside = _BRACES.sub("", token)
    if any(mark in outside for mark in _NOT_BARE):
        return False
    try:
        candidates = expand_pattern(token, groups=groups)
    except PatternError:
        return False
    return bool(candidates) and all(
        is_valid_domain(normalize_domain(item)) for item in candidates
    )


def _looks_like_secret(text: str) -> bool:
    """粗略判断一段文本是不是凭据，宁可多拦也不要让密钥进日志。"""
    lowered = text.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return True
    for token in _SECRET_TOKEN.findall(text):
        if any(char.isdigit() for char in token) and any(
            char.isalpha() for char in token
        ):
            return True
    return False


def format_duration_row(label: str, value: str) -> str:
    return f"{label}：{escape_html(value)}"


class NullBot:
    """Telegram 未启用时的占位实现。"""

    async def run(self) -> None:
        return None

    def stop(self) -> None:
        return None

    async def confirm_purchase(self, *args: Any, **kwargs: Any) -> bool:
        # 没有 TG 就无法确认，保守起见按「不批准」处理
        logger.warning("要求 Telegram 确认但 Telegram 未启用，放弃下单")
        return False
