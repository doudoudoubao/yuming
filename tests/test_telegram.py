import asyncio
import json

import httpx
import pytest

from domain_monitor.config import TelegramConfig
from domain_monitor.models import DomainState
from domain_monitor.notify.telegram import (
    Notifier,
    NullBot,
    TelegramBot,
    TelegramClient,
)


class Recorder:
    """记录 Bot API 调用，并可脚本化 getUpdates 的返回。"""

    def __init__(self, updates=None):
        self.sent: list[dict] = []
        self.calls: list[str] = []
        self._updates = list(updates or [])

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append(method)
        if method == "sendMessage":
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "bot"}})
        if method == "getUpdates":
            batch = self._updates.pop(0) if self._updates else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        return httpx.Response(200, json={"ok": True, "result": True})


def make_client(recorder, **overrides):
    config = TelegramConfig(
        enabled=True, bot_token="token", chat_id="42", **overrides
    )
    client = TelegramClient(
        config, client=httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    )
    return config, client


class StubController:
    def __init__(self):
        self.calls: list[tuple] = []

    async def cmd_list(self): self.calls.append(("list",)); return "LIST"
    async def cmd_status(self): self.calls.append(("status",)); return "STATUS"
    async def cmd_check(self, d): self.calls.append(("check", d)); return f"CHECK {d}"
    async def cmd_info(self, d): self.calls.append(("info", d)); return f"INFO {d}"
    async def cmd_add(self, d): self.calls.append(("add", tuple(d))); return "ADDED"
    async def cmd_remove(self, d): self.calls.append(("remove", d)); return "REMOVED"
    async def cmd_buy(self, d): self.calls.append(("buy", d)); return "BUYING"
    async def cmd_pause(self, p): self.calls.append(("pause", p)); return "PAUSED"
    async def cmd_log(self, n): self.calls.append(("log", n)); return "LOG"


# ------------------------------------------------------------------- 客户端

async def test_disabled_client_sends_nothing():
    recorder = Recorder()
    config = TelegramConfig(enabled=False)
    client = TelegramClient(
        config, client=httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    )
    assert await client.send("hi") is None
    assert recorder.calls == []


async def test_api_failure_is_swallowed():
    """通知失败不能把主流程带崩。"""
    client = TelegramClient(
        TelegramConfig(enabled=True, bot_token="t", chat_id="1"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={"ok": False, "description": "unauthorized"})
        )),
    )
    assert await client.send("hi") is None  # 不抛异常


async def test_long_message_truncated():
    recorder = Recorder()
    _, client = make_client(recorder)
    await client.send("x" * 9000)
    assert len(recorder.sent[0]["text"]) <= 4000


async def test_html_parse_mode_used():
    recorder = Recorder()
    _, client = make_client(recorder)
    await client.send("<b>hi</b>")
    assert recorder.sent[0]["parse_mode"] == "HTML"


# --------------------------------------------------------------------- 通知

async def test_notifier_respects_notify_states():
    recorder = Recorder()
    config, client = make_client(recorder, notify_states=["available"])
    notifier = Notifier(client, config)

    await notifier.state_changed("a.com", DomainState.REGISTERED, DomainState.EXPIRED)
    assert recorder.sent == []      # expired 不在白名单里

    await notifier.state_changed("a.com", DomainState.EXPIRED, DomainState.AVAILABLE)
    assert len(recorder.sent) == 1


async def test_notifier_escapes_html():
    recorder = Recorder()
    config, client = make_client(recorder)
    notifier = Notifier(client, config)
    await notifier.acquired("a<script>.com", "detail & more")
    text = recorder.sent[0]["text"]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp;" in text


# ----------------------------------------------------------------- 权限控制

def make_message(text, *, user_id=42, chat_id=42):
    return {
        "update_id": 1,
        "message": {"text": text, "chat": {"id": chat_id}, "from": {"id": user_id}},
    }


async def test_unauthorized_user_rejected():
    recorder = Recorder()
    config, client = make_client(recorder, allowed_user_ids=[999])
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("/list", user_id=1))

    assert controller.calls == []       # 命令没有被执行
    assert "未授权" in recorder.sent[0]["text"]


async def test_allowed_user_accepted():
    recorder = Recorder()
    config, client = make_client(recorder, allowed_user_ids=[999])
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("/list", user_id=999))

    assert controller.calls == [("list",)]


async def test_chat_id_fallback_when_no_user_allowlist():
    recorder = Recorder()
    config, client = make_client(recorder)     # chat_id=42，无 user 白名单
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("/list", chat_id=42, user_id=7))
    assert controller.calls == [("list",)]

    controller.calls.clear()
    await bot._dispatch(make_message("/list", chat_id=99, user_id=7))
    assert controller.calls == []


# --------------------------------------------------------------------- 命令

@pytest.mark.parametrize(
    "text,expected",
    [
        ("/list", ("list",)),
        ("/status", ("status",)),
        ("/check a.com", ("check", "a.com")),
        ("/info a.com", ("info", "a.com")),
        ("/add a.com b.com", ("add", ("a.com", "b.com"))),
        ("/del a.com", ("remove", "a.com")),
        ("/rm a.com", ("remove", "a.com")),
        ("/buy a.com", ("buy", "a.com")),
        ("/pause", ("pause", True)),
        ("/resume", ("pause", False)),
        ("/log 5", ("log", 5)),
        ("/list@mybot", ("list",)),          # 群里 @ 机器人的写法
    ],
)
async def test_command_routing(text, expected):
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message(text))

    assert controller.calls == [expected]


async def test_help_and_unknown_commands():
    recorder = Recorder()
    config, client = make_client(recorder)
    bot = TelegramBot(client, config, StubController())

    assert "域名监控机器人" in await bot._handle_command("/help")
    assert "用法" in await bot._handle_command("/check")
    assert "未知命令" in await bot._handle_command("/nonsense")


async def test_log_limit_is_clamped():
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._handle_command("/log 9999")
    await bot._handle_command("/log abc")

    assert controller.calls == [("log", 50), ("log", 15)]


async def test_non_command_text_ignored():
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("随便聊两句"))

    assert controller.calls == []
    assert recorder.sent == []


# ----------------------------------------------------------------- 二次确认

async def test_confirm_purchase_approved():
    recorder = Recorder()
    config, client = make_client(recorder)
    bot = TelegramBot(client, config, StubController())

    task = asyncio.create_task(bot.confirm_purchase("a.com", 9.99, "USD", timeout=5))
    await asyncio.sleep(0.05)

    token = list(bot._pending)[0]
    await bot._handle_callback({
        "id": "cb1", "data": f"ok:{token}",
        "from": {"id": 42}, "message": {"chat": {"id": 42}, "message_id": 7, "text": "q"},
    })

    assert await task is True


async def test_confirm_purchase_declined():
    recorder = Recorder()
    config, client = make_client(recorder)
    bot = TelegramBot(client, config, StubController())

    task = asyncio.create_task(bot.confirm_purchase("a.com", None, "USD", timeout=5))
    await asyncio.sleep(0.05)

    token = list(bot._pending)[0]
    await bot._handle_callback({
        "id": "cb1", "data": f"no:{token}",
        "from": {"id": 42}, "message": {"chat": {"id": 42}, "message_id": 7, "text": "q"},
    })

    assert await task is False


async def test_confirm_purchase_timeout_declines():
    """超时必须按「不批准」处理——宁可错过也不误买。"""
    recorder = Recorder()
    config, client = make_client(recorder)
    bot = TelegramBot(client, config, StubController())

    assert await bot.confirm_purchase("a.com", 9.99, "USD", timeout=0.15) is False
    assert bot._pending == {}


async def test_confirm_callback_from_stranger_ignored():
    recorder = Recorder()
    config, client = make_client(recorder, allowed_user_ids=[999])
    bot = TelegramBot(client, config, StubController())

    task = asyncio.create_task(bot.confirm_purchase("a.com", 1.0, "USD", timeout=0.4))
    await asyncio.sleep(0.05)
    token = list(bot._pending)[0]

    await bot._handle_callback({
        "id": "cb1", "data": f"ok:{token}",
        "from": {"id": 1}, "message": {"chat": {"id": 42}, "message_id": 7, "text": "q"},
    })

    assert await task is False      # 陌生人点的按钮不算数，最终超时拒绝


async def test_null_bot_declines_confirmation():
    """没配 Telegram 却要求确认时，保守地拒绝而不是放行。"""
    assert await NullBot().confirm_purchase("a.com", 1.0, "USD", 1.0) is False


async def test_silent_idle_mutes_non_urgent_states():
    """silent_idle=true 时普通状态变化静音推送，可注册/待删除仍然震手机。"""
    recorder = Recorder()
    config, client = make_client(
        recorder, silent_idle=True,
        notify_states=["expired", "pending_delete", "available"],
    )
    notifier = Notifier(client, config)

    await notifier.state_changed("a.com", DomainState.REGISTERED, DomainState.EXPIRED)
    await notifier.state_changed("a.com", DomainState.EXPIRED, DomainState.PENDING_DELETE)

    assert recorder.sent[0]["disable_notification"] is True    # 到期：静音
    assert recorder.sent[1]["disable_notification"] is False   # 待删除：提醒


async def test_silent_idle_off_notifies_everything():
    recorder = Recorder()
    config, client = make_client(recorder, silent_idle=False, notify_states=["expired"])
    notifier = Notifier(client, config)

    await notifier.state_changed("a.com", DomainState.REGISTERED, DomainState.EXPIRED)

    assert recorder.sent[0]["disable_notification"] is False
