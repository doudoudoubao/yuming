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
    async def cmd_tlds(self, name): self.calls.append(("tlds", name)); return "TLDS"


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


async def test_plain_domain_is_added():
    """直接发域名就能加监控，不用打 /add。"""
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("example.com"))

    assert controller.calls == [("add", ("example.com",))]


async def test_plain_multiple_domains_various_separators():
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("a.com b.net，c.io\nd.org"))

    assert controller.calls == [("add", ("a.com", "b.net", "c.io", "d.org"))]


async def test_plain_text_without_domain_gets_hint():
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("随便聊两句"))

    assert controller.calls == []
    assert "没认出域名" in recorder.sent[0]["text"]


async def test_mixed_content_is_not_auto_added():
    """消息里混了别的内容时不擅自替用户决定，只给出确认用的命令。"""
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("good.com 不是域名"))

    assert controller.calls == []
    assert "/add good.com" in recorder.sent[0]["text"]


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/anthropics/claude-code",   # 网址不该被剥成 github.com
        "看看 https://example.com/x 这个",
        "user@example.com",
        "example.com:8080",
    ],
)
async def test_urls_are_never_auto_added(text):
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message(text))

    assert controller.calls == []


@pytest.mark.parametrize(
    "domain",
    ["token.io", "apikey.com", "mysecret.com", "password-manager.io",
     "my-super-long-brand-2026.com"],
)
async def test_credential_looking_domains_are_still_added(domain):
    """字面像密钥但确实是合法域名的，必须能正常加入监控。"""
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message(domain))

    assert controller.calls == [("add", (domain,))]


async def test_chinese_full_stop_separator():
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("a.com。b.net"))

    assert controller.calls == [("add", ("a.com", "b.net"))]


async def test_unauthorized_plain_text_is_silently_ignored():
    """机器人待在群里时，不能把每一句闲聊都回一遍「未授权」。"""
    recorder = Recorder()
    config, client = make_client(recorder, allowed_user_ids=[999])
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("example.com", user_id=1))
    await bot._dispatch(make_message("今天天气不错", user_id=1))

    assert controller.calls == []
    assert recorder.sent == []          # 完全沉默


async def test_unauthorized_command_still_gets_one_reply():
    """明确发命令的人应该被告知为什么没反应。"""
    recorder = Recorder()
    config, client = make_client(recorder, allowed_user_ids=[999])
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("/list", user_id=1))

    assert controller.calls == []
    assert "未授权" in recorder.sent[0]["text"]


# ------------------------------------------------------------- 凭据泄露防护

@pytest.mark.parametrize(
    "text",
    [
        "我的 api_key 是 abc123",
        "NAMESILO_API_KEY=k9x7m2p4q8w1e5r3t6y0",
        "sk-abc123def456ghi789jkl012mno",
        "密码 hunter2",
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    ],
)
async def test_credential_like_messages_are_refused(text):
    """用户很容易顺手把密钥粘进聊天框——必须拦住，且不能进日志。"""
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message(text))

    assert controller.calls == []                       # 不当成域名去处理
    reply = recorder.sent[0]["text"]
    assert "密钥" in reply or "密码" in reply
    assert "吊销" in reply                               # 提示用户补救
    assert text not in reply                            # 绝不回显原文


async def test_credential_content_not_logged(caplog):
    recorder = Recorder()
    config, client = make_client(recorder)
    bot = TelegramBot(client, config, StubController())

    secret = "sk-supersecret1234567890abcdef"
    with caplog.at_level("WARNING"):
        await bot._dispatch(make_message(secret))

    assert secret not in caplog.text


async def test_long_domain_is_not_mistaken_for_a_secret():
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("very-long-subdomain-name-here.example.com"))

    assert controller.calls == [("add", ("very-long-subdomain-name-here.example.com",))]


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


async def test_plain_pattern_is_expanded():
    """直接发 mydream.{com,net,io} 就能一次盯一批。"""
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("mydream.{com,net,io}"))

    assert controller.calls == [("add", ("mydream.{com,net,io}",))]


async def test_pattern_commas_are_not_split_as_separators():
    """花括号里的逗号不能被当成分隔符切开。"""
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("a.{com,net} b.io"))

    assert controller.calls == [("add", ("a.{com,net}", "b.io"))]


async def test_malformed_pattern_is_not_treated_as_domain():
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("a.{com"))

    assert controller.calls == []


async def test_group_pattern_is_accepted():
    """vps.{@two} 要能被识别成域名模式，而不是被 @ 挡掉。"""
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    controller.pattern_groups = {"two": ["io", "co", "ai"]}
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("vps.{@two}"))

    assert controller.calls == [("add", ("vps.{@two}",))]


async def test_at_sign_outside_braces_still_rejected():
    """放宽 @ 不能把 user@example.com 这种一起放进来。"""
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    controller.pattern_groups = {"two": ["io"]}
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("user@example.com"))

    assert controller.calls == []


async def test_tlds_command_routes():
    recorder = Recorder()
    config, client = make_client(recorder)
    controller = StubController()
    bot = TelegramBot(client, config, controller)

    await bot._dispatch(make_message("/tlds"))
    await bot._dispatch(make_message("/tlds two"))

    assert controller.calls == [("tlds", None), ("tlds", "two")]
