"""Bot 消息排版：长度、HTML 配对、转义、留白。

这些问题肉眼看不出来，但会让 Telegram 渲染出错或直接拒收消息。
"""

import datetime
import re

import pytest

from domain_monitor.models import DomainState, Phase
from domain_monitor.notify.telegram import HELP_SECTIONS, MAX_MESSAGE
from domain_monitor.storage import Storage
from domain_monitor.utils import utcnow
from tests.conftest import FakeRdapServer, rdap_payload
from tests.test_engine import build_engine

_TAGS = re.compile(r"</?(b|i|code|pre)>")


def assert_well_formed(text: str, label: str) -> None:
    assert len(text) <= MAX_MESSAGE, f"{label} 超过单条上限（{len(text)}）"

    for tag in ("b", "i", "code"):
        assert text.count(f"<{tag}>") == text.count(f"</{tag}>"), \
            f"{label} 的 <{tag}> 标签不配对"

    # 去掉合法标签后不该还有裸露的尖括号——Telegram 会解析失败并拒收
    stripped = _TAGS.sub("", text).replace("&lt;", "").replace("&gt;", "").replace("&amp;", "")
    assert "<" not in stripped and ">" not in stripped, f"{label} 有未转义的尖括号"

    plain = _TAGS.sub("", text)
    assert "\n\n\n" not in plain, f"{label} 有连续空行"
    assert plain.rstrip() == plain, f"{label} 结尾有多余空白"
    for line in plain.splitlines():
        assert line == line.rstrip(), f"{label} 有行尾空格"


@pytest.mark.parametrize("name", sorted(HELP_SECTIONS))
def test_help_sections_are_well_formed(name):
    assert_well_formed(HELP_SECTIONS[name], f"/help {name}")


@pytest.fixture
def populated(rdap_server, storage):
    """造一份有内容的监控列表，让各命令都渲染出真实形态。"""
    engine = build_engine(
        rdap_server, storage, purchase={"enabled": True, "dry_run": True}
    )
    now = utcnow()
    for domain, state, phase in (
        ("mydream.com", DomainState.PENDING_DELETE, Phase.SPRINT),
        ("xn--0zwm56d.com", DomainState.REDEMPTION, Phase.WATCH),   # 中文域名
        ("off.com", DomainState.AVAILABLE, Phase.IDLE),
    ):
        storage.upsert_domain(domain, group="prefix:x", stop_after_first=True,
                              source="telegram", auto_buy=(domain == "off.com"))
        storage.update_domain(
            domain, state=state, phase=phase, registrar="演示注册商 <测试>",
            drop_at=now + datetime.timedelta(hours=3),
            expires_at=now + datetime.timedelta(days=100),
        )
    storage.add_event("acquired", domain="mydream.com",
                      message="注册商=namesilo，尝试 3 次", level="warning")
    rdap_server.set("target.com", rdap_payload())
    return engine


async def test_every_command_renders_cleanly(populated):
    """每个命令的输出都要能被 Telegram 正常渲染。"""
    cases = {
        "/list": populated.cmd_list(),
        "/status": populated.cmd_status(),
        "/info": populated.cmd_info("mydream.com"),
        "/info 中文域名": populated.cmd_info("测试.com"),
        "/log": populated.cmd_log(5),
        "/check": populated.cmd_check("target.com"),
        "/check 非法": populated.cmd_check("不是域名"),
        "/tlds": populated.cmd_tlds(None),
        "/tlds two": populated.cmd_tlds("two"),
        "/tlds 未知": populated.cmd_tlds("nope"),
        "/mode": populated.cmd_mode(None),
        "/mode 真实": populated.cmd_mode("真实"),
        "/auto": populated.cmd_auto("mydream.com", None),
        "/auto 开": populated.cmd_auto("mydream.com", "开"),
        "/setkey": populated.cmd_setkey(None, None),
        "/add": populated.cmd_add(["new.com"]),
        "/add 批量": populated.cmd_add(["vps.{@all}"]),
        "/add 非法": populated.cmd_add(["不是域名"]),
        "/del": populated.cmd_remove("new.com"),
        "/del 不存在": populated.cmd_remove("nope.com"),
        "/pause": populated.cmd_pause(True),
        "/buy": populated.cmd_buy("x.com"),
        "/reload": populated.cmd_reload(),
    }
    for label, coro in cases.items():
        assert_well_formed(await coro, label)


async def test_empty_states_render_cleanly(rdap_server, storage):
    """空列表、无事件这些边界情况也不能出现残缺排版。"""
    engine = build_engine(rdap_server, storage)

    assert_well_formed(await engine.cmd_list(), "空 /list")
    assert_well_formed(await engine.cmd_log(5), "空 /log")
    assert_well_formed(await engine.cmd_status(), "空 /status")


async def test_hostile_input_is_escaped(rdap_server, storage):
    """备注、注册商名里的尖括号必须转义，否则整条消息会被 Telegram 拒收。"""
    engine = build_engine(rdap_server, storage)
    storage.upsert_domain("evil.com", note="<script>alert(1)</script>")
    storage.update_domain("evil.com", registrar="<b>假注册商</b>")

    text = await engine.cmd_info("evil.com")

    assert_well_formed(text, "/info 含尖括号")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


async def test_long_list_stays_within_one_message(rdap_server, storage):
    """域名很多时列表不能超过单条上限——超了会被截断成半截。"""
    engine = build_engine(rdap_server, storage)
    for index in range(200):
        storage.upsert_domain(f"domain{index:03d}.example")

    assert_well_formed(await engine.cmd_list(), "200 个域名的 /list")


async def test_long_event_messages_are_truncated(rdap_server, storage):
    """事件单条截到 120 字还不够——30 条加起来照样超上限。"""
    engine = build_engine(rdap_server, storage)
    for index in range(30):
        storage.add_event("x", domain="a.com", message="很长的消息内容" * 50)

    text = await engine.cmd_log(30)

    assert_well_formed(text, "长事件 /log")
    assert "更早的" in text, "截掉的部分要说清楚，不能悄悄少列"


async def test_long_domains_do_not_overflow_list(rdap_server, storage):
    """域名最长 253 字符，30 条就能撑爆——条数闸门之外还要有长度闸门。"""
    engine = build_engine(rdap_server, storage)
    long = ("a" * 60 + ".") * 3 + "example"
    for index in range(40):
        storage.upsert_domain(f"{index:02d}{long}")

    text = await engine.cmd_list()

    assert_well_formed(text, "长域名 /list")
    assert "还有" in text


async def test_bulk_add_replies_stay_short(rdap_server, storage):
    """一次加 200 个（pattern_limit 上限），三种回执都不能刷屏。"""
    engine = build_engine(rdap_server, storage)
    names = [f"repeat{index:03d}.example" for index in range(200)]

    assert_well_formed(await engine.cmd_add(names), "批量 /add 新增")
    assert_well_formed(await engine.cmd_add(names), "批量 /add 重复")
    assert_well_formed(
        await engine.cmd_add([f"不合法{index}" for index in range(200)]),
        "批量 /add 非法",
    )
