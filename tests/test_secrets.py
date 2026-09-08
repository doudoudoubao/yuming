"""在聊天里写凭据：白名单、消息删除、不落日志、热重载。"""

import json
import os

import httpx
import pytest

from domain_monitor.notify.telegram import (
    TelegramBot,
    TelegramClient,
    _carries_a_secret,
)
from domain_monitor.config import TelegramConfig
from domain_monitor.storage import Storage
from domain_monitor.utils import mask_secret, write_dotenv
from tests.conftest import FakeRdapServer
from tests.test_engine import build_engine

SECRET = "sk-live-abcdef1234567890"


# ------------------------------------------------------------------ 写入文件

def test_write_dotenv_creates_with_600(tmp_path):
    """密钥文件从诞生起就该是 600，不能有一段时间是默认权限。"""
    path = tmp_path / ".env"
    write_dotenv(path, "NAMESILO_API_KEY", SECRET)

    assert oct(os.stat(path).st_mode)[-3:] == "600"
    assert path.read_text(encoding="utf-8").strip() == f"NAMESILO_API_KEY={SECRET}"


def test_write_dotenv_replaces_existing(tmp_path):
    path = tmp_path / ".env"
    write_dotenv(path, "A", "1")
    write_dotenv(path, "B", "2")
    write_dotenv(path, "A", "3")

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines == ["A=3", "B=2"]


def test_write_dotenv_handles_export_prefix(tmp_path):
    path = tmp_path / ".env"
    path.write_text("export TOKEN=old\n", encoding="utf-8")
    write_dotenv(path, "TOKEN", "new")
    assert "TOKEN=new" in path.read_text(encoding="utf-8")
    assert "old" not in path.read_text(encoding="utf-8")


def test_mask_secret_hides_almost_everything():
    assert mask_secret("abcdefgh") == "****efgh"
    assert mask_secret("ab") == "**"
    assert SECRET[:-4] not in mask_secret(SECRET)


# -------------------------------------------------------------------- 命令

async def test_setkey_is_off_by_default(rdap_server, storage):
    engine = build_engine(rdap_server, storage)

    reply = await engine.cmd_setkey("NAMESILO_API_KEY", SECRET)

    assert "没有开启" in reply
    assert SECRET not in reply


async def test_setkey_rejects_names_outside_the_whitelist(rdap_server, storage, tmp_path):
    """拿到聊天权限的人不能往 .env 里写任意变量（比如把流量代理走）。"""
    engine = build_engine(rdap_server, storage,
                          telegram={"enabled": False, "allow_secret_input": True})
    engine.config.path = str(tmp_path / "config.yaml")

    for name in ("HTTPS_PROXY", "PATH", "TG_BOT_TOKEN", "LD_PRELOAD"):
        reply = await engine.cmd_setkey(name, "x")
        assert "不允许写入" in reply, name
    assert not (tmp_path / ".env").exists()


async def test_setkey_writes_and_masks(rdap_server, storage, tmp_path):
    engine = build_engine(rdap_server, storage,
                          telegram={"enabled": False, "allow_secret_input": True})
    engine.config.path = str(tmp_path / "config.yaml")

    reply = await engine.cmd_setkey("NAMESILO_API_KEY", SECRET)

    assert SECRET not in reply                      # 回显必须打码
    assert mask_secret(SECRET) in reply
    assert (tmp_path / ".env").read_text(encoding="utf-8").strip().endswith(SECRET)
    assert oct(os.stat(tmp_path / ".env").st_mode)[-3:] == "600"


async def test_setkey_never_logs_the_value(rdap_server, storage, tmp_path, caplog):
    engine = build_engine(rdap_server, storage,
                          telegram={"enabled": False, "allow_secret_input": True})
    engine.config.path = str(tmp_path / "config.yaml")

    with caplog.at_level("DEBUG"):
        await engine.cmd_setkey("NAMESILO_API_KEY", SECRET)

    assert SECRET not in caplog.text
    # 事件流里也不能有
    assert all(SECRET not in event.message for event in storage.recent_events(10))


async def test_setkey_listing_shows_no_values(rdap_server, storage):
    engine = build_engine(rdap_server, storage,
                          telegram={"enabled": False, "allow_secret_input": True})

    reply = await engine.cmd_setkey(None, None)

    assert "NAMESILO_API_KEY" in reply
    assert "不保证成功" in reply          # 如实说明删除可能失败


# ------------------------------------------------------------ 消息自动删除

@pytest.mark.parametrize(
    "text,expected",
    [
        ("/setkey NAMESILO_API_KEY abc", True),
        ("/setkey@bot K v", True),
        ("/密钥 K v", True),
        ("/setkey", False),          # 没带参数就没有明文
        ("/list", False),
        ("/mode 真实 确认", False),
    ],
)
def test_which_commands_carry_a_secret(text, expected):
    assert _carries_a_secret(text) is expected


class DeleteRecorder:
    def __init__(self, can_delete=True):
        self.sent: list[str] = []
        self.deleted: list[int] = []
        self.can_delete = can_delete

    def handler(self, request):
        method = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            self.sent.append(body["text"])
        if method == "deleteMessage":
            if not self.can_delete:
                return httpx.Response(
                    200, json={"ok": False, "description": "message can't be deleted"}
                )
            self.deleted.append(body["message_id"])
        return httpx.Response(200, json={"ok": True, "result": True})


class Stub:
    def __init__(self): self.calls = []
    async def cmd_setkey(self, n, v): self.calls.append((n, v)); return "OK"
    async def cmd_list(self): self.calls.append(("list",)); return "LIST"


def make_bot(recorder):
    config = TelegramConfig(enabled=True, bot_token="t", chat_id="42")
    client = TelegramClient(
        config, client=httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    )
    return TelegramBot(client, config, Stub())


def secret_message():
    return {
        "update_id": 1,
        "message": {"text": f"/setkey NAMESILO_API_KEY {SECRET}",
                    "chat": {"id": 42}, "from": {"id": 42}, "message_id": 77},
    }


async def test_secret_message_is_deleted():
    recorder = DeleteRecorder()
    bot = make_bot(recorder)

    await bot._dispatch(secret_message())

    assert recorder.deleted == [77]
    assert all(SECRET not in text for text in recorder.sent)


async def test_user_is_warned_when_deletion_fails():
    """删不掉必须如实说，不能让用户以为已经清理干净了。"""
    recorder = DeleteRecorder(can_delete=False)
    bot = make_bot(recorder)

    await bot._dispatch(secret_message())

    assert recorder.deleted == []
    assert any("删不掉" in text for text in recorder.sent)
    assert any("手动长按删除" in text for text in recorder.sent)


async def test_ordinary_commands_are_not_deleted():
    recorder = DeleteRecorder()
    bot = make_bot(recorder)

    await bot._dispatch({
        "update_id": 1,
        "message": {"text": "/list", "chat": {"id": 42},
                    "from": {"id": 42}, "message_id": 5},
    })

    assert recorder.deleted == []


# ---------------------------------------------------------------- 热重载

async def test_reload_picks_up_a_freshly_written_key(tmp_path, monkeypatch):
    """写完密钥不重启就能生效——否则手机上写完还得去登服务器，功能没意义。"""
    from domain_monitor.app import Application
    from domain_monitor.config import load_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"state_dir: '{tmp_path}'\n"
        "database: 's.db'\n"
        "rdap: {rps_per_host: 1000}\n"
        "dns: {enabled: false}\n"
        "telegram: {enabled: false, allow_secret_input: true}\n"
        "registrar:\n"
        "  provider: namesilo\n"
        "  options: {api_key: '${NAMESILO_API_KEY}'}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NAMESILO_API_KEY", raising=False)

    async with Application(load_config(config_path)) as app:
        assert not app.pool.primary.options.get("api_key")

        await app.engine.cmd_setkey("NAMESILO_API_KEY", SECRET)
        reply = await app.engine.cmd_reload()

        assert "已重新加载" in reply
        assert app.pool.primary.options.get("api_key") == SECRET


async def test_reload_keeps_working_config_when_the_new_one_is_broken(tmp_path):
    """重载失败必须保住原来能用的配置，不能把服务搞挂。"""
    from domain_monitor.app import Application
    from domain_monitor.config import load_config

    config_path = tmp_path / "config.yaml"
    good = (
        f"state_dir: '{tmp_path}'\n"
        "database: 's.db'\n"
        "dns: {enabled: false}\n"
        "telegram: {enabled: false}\n"
    )
    config_path.write_text(good, encoding="utf-8")

    async with Application(load_config(config_path)) as app:
        before = app.pool.labels
        config_path.write_text("poll:\n  这个键不存在: 1\n", encoding="utf-8")

        reply = await app.engine.cmd_reload()

        assert "配置有误" in reply
        assert app.pool.labels == before        # 通道还在


async def test_reload_without_a_config_file(rdap_server, storage):
    engine = build_engine(rdap_server, storage)
    assert "不支持热重载" in await engine.cmd_reload()


# ------------------------------------------------------------ 故障告警

async def test_send_standalone_uses_env_only(monkeypatch):
    """配置解析失败时也要能报警，所以不能依赖配置对象。"""
    import domain_monitor.notify.telegram as tg

    sent: list[str] = []

    def handler(request):
        body = json.loads(request.content or b"{}")
        sent.append(body.get("text", ""))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    monkeypatch.setenv("TG_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_CHAT_ID", "42")

    original = tg.TelegramClient.start

    async def patched(self):
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self._owns_client = True

    monkeypatch.setattr(tg.TelegramClient, "start", patched)
    try:
        assert await tg.send_standalone("炸了") is True
        assert sent == ["炸了"]
    finally:
        monkeypatch.setattr(tg.TelegramClient, "start", original)


async def test_send_standalone_without_credentials(monkeypatch):
    from domain_monitor.notify.telegram import send_standalone

    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)

    assert await send_standalone("x") is False      # 静默失败，不抛异常


async def test_send_standalone_honours_api_base(monkeypatch):
    """走镜像/反代的用户，在最需要报警的时候不能因为地址不对而发不出去。"""
    import domain_monitor.notify.telegram as tg

    monkeypatch.setenv("TG_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    monkeypatch.setenv("TG_API_BASE", "https://my-mirror.example")

    seen: list[str] = []

    async def patched(self):
        seen.append(self.config.api_base)
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"ok": True, "result": {}})
            )
        )
        self._owns_client = True

    monkeypatch.setattr(tg.TelegramClient, "start", patched)
    await tg.send_standalone("x")

    assert seen == ["https://my-mirror.example"]


async def test_send_standalone_never_raises(monkeypatch):
    """报警本身失败，绝不能再制造一次故障。"""
    import domain_monitor.notify.telegram as tg

    monkeypatch.setenv("TG_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_CHAT_ID", "42")

    async def boom(self):
        raise RuntimeError("网络没了")

    monkeypatch.setattr(tg.TelegramClient, "start", boom)
    assert await tg.send_standalone("x") is False
