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
from ..utils import escape_html, is_valid_domain, normalize_domain, truncate

logger = logging.getLogger(__name__)

MAX_MESSAGE = 4000  # Telegram 上限 4096，留点余量

HELP_TEXT = """<b>域名监控机器人</b>

<b>查看</b>
/list — 监控列表与状态
/status — 系统运行状态与统计
/check &lt;域名&gt; — 立即查询一个域名
/info &lt;域名&gt; — 查看某域名的详细信息
/log [数量] — 最近事件

<b>管理</b>
直接把域名发给我就能加入监控（不用打命令）
/add &lt;域名&gt; [域名2 ...] — 加入监控
/del &lt;域名&gt; — 移出监控
/pause — 暂停自动抢注（仍继续监控）
/resume — 恢复自动抢注
/buy &lt;域名&gt; — 立即尝试注册（需确认）

/help — 显示本帮助"""

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
    {"command": "log", "description": "最近事件"},
    {"command": "help", "description": "帮助"},
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
        if reply:
            await self.client.send(reply, chat_id=chat_id)

    async def _handle_command(self, text: str) -> str:
        parts = text.split()
        command = parts[0].lstrip("/").split("@", 1)[0].lower()
        args = parts[1:]

        if command in ("start", "help"):
            return HELP_TEXT
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

        tokens = [item for item in re.split(r"[\s,;，、]+", text) if item]
        domains, rejected = [], []
        for token in tokens[:20]:
            name = normalize_domain(token)
            (domains if is_valid_domain(name) else rejected).append(token)

        if not domains:
            return (
                "没认出域名。直接把域名发给我就能加入监控，例如：\n"
                "<code>example.com</code>\n"
                "<code>a.com b.net c.io</code>\n\n"
                "或者发 /help 查看全部命令。"
            )

        reply = await self.controller.cmd_add(domains)
        if rejected:
            reply += "\n\n（忽略了无法识别的内容：" + escape_html(
                " ".join(rejected[:5])
            ) + "）"
        return reply

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


# 常见的密钥字样；命中任一即视为可疑
_SECRET_HINTS = (
    "api_key", "apikey", "api key", "secret", "token", "password", "passwd",
    "access_key", "accesskey", "bearer", "私钥", "密钥", "密码", "口令",
)
# 长串随机字符：≥24 位、字母数字混合，典型的 API Key 形状
_SECRET_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _looks_like_secret(text: str) -> bool:
    """粗略判断一段文本是不是凭据，宁可多拦也不要让密钥进日志。"""
    lowered = text.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return True
    for token in _SECRET_TOKEN.findall(text):
        if normalize_domain(token) and is_valid_domain(normalize_domain(token)):
            continue  # 长域名不算凭据
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
