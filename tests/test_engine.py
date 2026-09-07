from datetime import timedelta

import httpx
import pytest

from domain_monitor.config import RegistrarConfig, load_config
from domain_monitor.dnsprobe import DnsProbe, ProbeResult
from domain_monitor.engine import Engine
from domain_monitor.models import DomainState, Phase
from domain_monitor.notify.telegram import Notifier, TelegramClient
from domain_monitor.rdap import RdapClient
from domain_monitor.registrars import build_registrar
from domain_monitor.storage import Storage
from domain_monitor.utils import utcnow
from tests.conftest import FakeTelegram, FakeRdapServer, rdap_payload


def build_engine(rdap_server, storage, *, bot=None, **overrides):
    """按需要拼一台引擎；默认离线、不下单。"""
    data = {
        "rdap": {"rps_per_host": 10000},
        "dns": {"enabled": False},
        "telegram": {"enabled": False},
        "poll": {"jitter": 0.0},
        "registrar": {"provider": "dryrun", "options": {}},
        "purchase": {},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value

    # 配置校验会故意拦下「dry_run=false 却指向假注册商」这种自欺组合。
    # 但测试恰恰需要用假注册商去走「真实下单」那条代码路径，所以这里先拿一个
    # 真实 provider 通过校验，再把适配器实例换回测试想要的那个。
    wanted = dict(data["registrar"])
    real_purchase = data["purchase"].get("enabled") and not data["purchase"].get("dry_run", True)
    if real_purchase and wanted.get("provider", "dryrun") == "dryrun":
        data["registrar"] = {"provider": "namesilo", "options": {"api_key": "test"}}

    config = load_config(data=data)

    rdap = RdapClient(
        config.rdap, client=httpx.AsyncClient(transport=httpx.MockTransport(rdap_server.handler))
    )
    tg_transport = httpx.MockTransport(bot.handler) if bot else None
    tg_client = TelegramClient(
        config.telegram,
        client=httpx.AsyncClient(transport=tg_transport) if tg_transport else httpx.AsyncClient(),
    )
    registrar = build_registrar(
        RegistrarConfig(
            provider=wanted.get("provider", "dryrun"),
            options=wanted.get("options", {}),
            contact=wanted.get("contact", {}),
        ),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        ),
    )
    engine = Engine(
        config, storage, rdap, registrar, Notifier(tg_client, config.telegram),
        probe=DnsProbe(config.dns), bot=bot,
    )
    return engine


# ------------------------------------------------------------------ 状态推进

async def test_registered_domain_scheduled_at_idle(rdap_server, storage):
    rdap_server.set("target.com", rdap_payload())
    engine = build_engine(rdap_server, storage, poll={"idle_interval": 3600})
    storage.upsert_domain("target.com")

    await engine.run_once()

    item = storage.get_domain("target.com")
    assert item.state is DomainState.REGISTERED
    assert item.phase is Phase.IDLE
    remaining = (item.next_check_at - utcnow()).total_seconds()
    assert 3500 < remaining <= 3600


async def test_pending_delete_sets_drop_estimate(rdap_server, storage):
    changed = utcnow() - timedelta(days=1)
    rdap_server.set(
        "target.com",
        rdap_payload(statuses=["pending delete"], changed=changed.isoformat()),
    )
    engine = build_engine(rdap_server, storage)
    storage.upsert_domain("target.com")

    await engine.run_once()

    item = storage.get_domain("target.com")
    assert item.state is DomainState.PENDING_DELETE
    # pendingDelete 起点 + 5 天，且对齐到 .com 的经验删除窗口
    expected = changed + timedelta(days=5)
    assert abs((item.drop_at - expected).total_seconds()) < 24 * 3600
    assert item.pending_delete_since is not None


async def test_drop_estimate_does_not_slide_without_last_changed(rdap_server, storage):
    """RDAP 不返回 last changed 时，预测时间必须钉在首次观测，而不能每轮往后滑。"""
    rdap_server.set("target.com", rdap_payload(statuses=["redemption period"]))
    engine = build_engine(rdap_server, storage)
    storage.upsert_domain("target.com")

    await engine.run_once()
    first = storage.get_domain("target.com").drop_at

    storage.update_domain("target.com", next_check_at=utcnow() - timedelta(seconds=1))
    await engine.run_once()
    second = storage.get_domain("target.com").drop_at

    assert abs((second - first).total_seconds()) < 2


async def test_query_error_never_reads_as_available(rdap_server, storage):
    """核心安全性质：RDAP 挂了不能触发抢注。"""
    def handler(request):
        if "dns.json" in str(request.url):
            return httpx.Response(200, json={"services": [[["com"], ["https://r/"]]]})
        return httpx.Response(500)

    engine = build_engine(rdap_server, storage,
                          rdap={"max_retries": 0},
                          purchase={"enabled": True, "dry_run": True})
    engine.rdap = RdapClient(
        engine.config.rdap,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    storage.upsert_domain("target.com")

    await engine.run_once()

    item = storage.get_domain("target.com")
    assert item.state is DomainState.UNKNOWN      # 状态没有被改写
    assert item.last_error is not None
    assert storage.has_successful_purchase("target.com") is False


# ---------------------------------------------------------------------- 档位

@pytest.mark.parametrize(
    "remaining_seconds,expected",
    [
        (10 * 24 * 3600, Phase.IDLE),
        (2 * 24 * 3600, Phase.WATCH),
        (3600, Phase.NEAR),
        (60, Phase.SPRINT),
        (-60, Phase.SPRINT),
        (-999999, Phase.WATCH),      # 预测偏了，退回加密监控继续等
    ],
)
def test_phase_escalation(rdap_server, storage, remaining_seconds, expected):
    engine = build_engine(rdap_server, storage)
    now = utcnow()
    drop_at = now + timedelta(seconds=remaining_seconds)
    assert engine._phase_for(DomainState.PENDING_DELETE, drop_at, now) is expected


def test_phase_without_estimate(rdap_server, storage):
    engine = build_engine(rdap_server, storage)
    now = utcnow()
    assert engine._phase_for(DomainState.REGISTERED, None, now) is Phase.IDLE
    assert engine._phase_for(DomainState.PENDING_DELETE, None, now) is Phase.WATCH


# ---------------------------------------------------------------------- 抢注

async def test_available_triggers_acquisition(rdap_server, storage):
    rdap_server.set("target.com", None)  # 404 = 可注册
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "max_price": 50},
        registrar={"provider": "dryrun", "options": {"price": 9.99}},
    )
    storage.upsert_domain("target.com")

    await engine.run_once()

    item = storage.get_domain("target.com")
    assert item.state is DomainState.ACQUIRED
    assert item.acquired_at is not None
    assert storage.has_successful_purchase("target.com") is True
    assert storage.spend_today() == 9.99


async def test_monitor_only_mode_does_not_buy(rdap_server, storage):
    rdap_server.set("target.com", None)
    engine = build_engine(rdap_server, storage, purchase={"enabled": False})
    storage.upsert_domain("target.com")

    await engine.run_once()

    assert storage.get_domain("target.com").state is DomainState.AVAILABLE
    assert storage.has_successful_purchase("target.com") is False


async def test_dry_run_records_but_does_not_spend(rdap_server, storage):
    rdap_server.set("target.com", None)
    engine = build_engine(rdap_server, storage,
                          purchase={"enabled": True, "dry_run": True})
    storage.upsert_domain("target.com")

    await engine.run_once()

    assert storage.get_domain("target.com").state is DomainState.ACQUIRED
    assert storage.spend_today() == 0.0   # 演练不计入花费


async def test_pause_blocks_acquisition(rdap_server, storage):
    rdap_server.set("target.com", None)
    engine = build_engine(rdap_server, storage,
                          purchase={"enabled": True, "dry_run": True})
    storage.upsert_domain("target.com")
    engine.set_paused(True)

    await engine.run_once()

    assert storage.has_successful_purchase("target.com") is False
    assert storage.get_domain("target.com").state is DomainState.AVAILABLE


async def test_price_above_limit_aborts(rdap_server, storage):
    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "max_price": 20},
        registrar={"provider": "dryrun", "options": {"price": 999.0}},
    )
    storage.upsert_domain("target.com")

    await engine.run_once()

    assert storage.has_successful_purchase("target.com") is False
    kinds = [event.kind for event in storage.recent_events(20)]
    assert "price_reject" in kinds


async def test_per_domain_price_limit_overrides_global(rdap_server, storage):
    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "max_price": 500},
        registrar={"provider": "dryrun", "options": {"price": 100.0}},
    )
    storage.upsert_domain("target.com", max_price=10)

    await engine.run_once()

    assert storage.has_successful_purchase("target.com") is False


async def test_daily_budget_blocks_acquisition(rdap_server, storage):
    from domain_monitor.models import RegistrationResult

    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "max_price": 50, "daily_budget": 30},
        registrar={"provider": "dryrun", "options": {"price": 25.0}},
    )
    storage.upsert_domain("target.com")
    # 今天已经花掉 20，再买 25 会超预算
    storage.record_purchase(
        RegistrationResult(domain="other.com", success=True, price=20.0), dry_run=False
    )

    await engine.run_once()

    assert storage.has_successful_purchase("target.com") is False
    assert "budget_reject" in [event.kind for event in storage.recent_events(20)]


async def test_retries_until_success(rdap_server, storage):
    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "attempt_interval": 0,
                  "attempt_concurrency": 1, "max_attempts": 10},
        registrar={"provider": "dryrun", "options": {"fail_times": 3, "price": 5.0}},
    )
    storage.upsert_domain("target.com")

    await engine.run_once()

    item = storage.get_domain("target.com")
    assert item.state is DomainState.ACQUIRED
    assert item.attempts == 4      # 3 次失败 + 1 次成功


async def test_gives_up_after_max_attempts(rdap_server, storage):
    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "attempt_interval": 0,
                  "attempt_concurrency": 1, "max_attempts": 3},
        registrar={"provider": "dryrun", "options": {"always_fail": True}},
    )
    storage.upsert_domain("target.com")

    await engine.run_once()

    assert storage.has_successful_purchase("target.com") is False
    assert storage.get_domain("target.com").attempts == 3
    assert "acquire_failed" in [event.kind for event in storage.recent_events(20)]


async def test_fatal_error_stops_retry_loop(rdap_server, storage):
    """余额不足这类硬错误必须立刻停手，不能空转几百次。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text=(
            '<?xml version="1.0"?><namesilo><reply><code>400</code>'
            "<detail>insufficient funds</detail></reply></namesilo>"
        ))

    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "attempt_interval": 0,
                  "attempt_concurrency": 1, "max_attempts": 50,
                  "check_price_first": False},
        registrar={"provider": "namesilo", "options": {"api_key": "k"}},
    )
    engine.registrar = build_registrar(
        engine.config.registrar,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    rdap_server.set("target.com", None)
    storage.upsert_domain("target.com")

    await engine.run_once()

    assert calls["n"] == 1     # 只试了一次就停
    assert "acquire_abort" in [event.kind for event in storage.recent_events(20)]


async def test_never_buys_twice(rdap_server, storage):
    from domain_monitor.models import RegistrationResult

    rdap_server.set("target.com", None)
    engine = build_engine(rdap_server, storage,
                          purchase={"enabled": True, "dry_run": False})
    storage.upsert_domain("target.com")
    storage.record_purchase(
        RegistrationResult(domain="target.com", success=True, price=9.0), dry_run=False
    )

    await engine.run_once()

    # 已经买到过就不再下单，花费不变
    assert storage.spend_today() == 9.0


# ------------------------------------------------------------- Telegram 相关

async def test_telegram_confirmation_rejected_blocks_purchase(rdap_server, storage):
    telegram = FakeTelegram(confirm=False)
    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage, bot=telegram,
        telegram={"enabled": True, "bot_token": "t", "chat_id": "1"},
        purchase={"enabled": True, "dry_run": True, "confirm_via_telegram": True},
    )
    storage.upsert_domain("target.com")

    await engine.run_once()

    assert telegram.confirm_calls == ["target.com"]
    assert storage.has_successful_purchase("target.com") is False
    assert "confirm_rejected" in [event.kind for event in storage.recent_events(20)]


async def test_telegram_confirmation_approved_allows_purchase(rdap_server, storage):
    telegram = FakeTelegram(confirm=True)
    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage, bot=telegram,
        purchase={"enabled": True, "dry_run": True, "confirm_via_telegram": True},
    )
    storage.upsert_domain("target.com")

    await engine.run_once()

    assert telegram.confirm_calls == ["target.com"]
    assert storage.get_domain("target.com").state is DomainState.ACQUIRED


async def test_notifications_sent_on_acquisition(rdap_server, storage, monkeypatch):
    telegram = FakeTelegram()
    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage, bot=telegram,
        telegram={"enabled": True, "bot_token": "t", "chat_id": "1"},
        purchase={"enabled": True, "dry_run": True},
    )
    storage.upsert_domain("target.com")

    await engine.run_once()

    joined = "\n".join(telegram.messages)
    assert "可以注册了" in joined
    assert "抢注成功" in joined


# ------------------------------------------------------- Telegram 命令处理

async def test_cmd_add_list_remove(rdap_server, storage):
    engine = build_engine(rdap_server, storage)

    reply = await engine.cmd_add(["Example.COM", "bad domain", "example.com"])
    assert "example.com" in reply
    assert "非法域名" in reply
    assert storage.get_domain("example.com") is not None

    listing = await engine.cmd_list()
    assert "example.com" in listing

    removed = await engine.cmd_remove("example.com")
    assert "已移出监控" in removed
    assert storage.get_domain("example.com") is None


async def test_cmd_check_reports_status(rdap_server, storage):
    rdap_server.set("target.com", rdap_payload(statuses=["pending delete"]))
    engine = build_engine(rdap_server, storage)
    reply = await engine.cmd_check("TARGET.com")
    assert "待删除" in reply
    assert "未在监控列表中" in reply


async def test_cmd_check_rejects_garbage(rdap_server, storage):
    engine = build_engine(rdap_server, storage)
    assert "不是合法域名" in await engine.cmd_check("nonsense")


async def test_cmd_pause_resume(rdap_server, storage):
    engine = build_engine(rdap_server, storage)
    assert engine.paused is False
    await engine.cmd_pause(True)
    assert engine.paused is True
    await engine.cmd_pause(False)
    assert engine.paused is False


async def test_cmd_status_and_log(rdap_server, storage):
    engine = build_engine(rdap_server, storage)
    storage.upsert_domain("a.com")
    storage.add_event("test", domain="a.com", message="hello")

    status = await engine.cmd_status()
    assert "监控域名" in status
    assert "注册商" in status

    log = await engine.cmd_log(5)
    assert "hello" in log


async def test_cmd_buy_requires_purchase_enabled(rdap_server, storage):
    engine = build_engine(rdap_server, storage, purchase={"enabled": False})
    assert "purchase.enabled=false" in await engine.cmd_buy("a.com")


# ------------------------------------------------------------------ 端到端

async def test_full_drop_catch_lifecycle(rdap_server, storage):
    """完整生命周期：正常注册 → 待删除 → 释放 → 抢注成功。"""
    telegram = FakeTelegram()
    engine = build_engine(
        rdap_server, storage, bot=telegram,
        telegram={"enabled": True, "bot_token": "t", "chat_id": "1"},
        purchase={"enabled": True, "dry_run": False, "attempt_interval": 0,
                  "attempt_concurrency": 1, "max_attempts": 5},
        registrar={"provider": "dryrun", "options": {"fail_times": 2, "price": 11.0}},
        poll={"jitter": 0.0},
    )
    storage.upsert_domain("target.com", max_price=50)

    # 第 1 步：正常注册中
    rdap_server.set("target.com", rdap_payload())
    await engine.run_once()
    assert storage.get_domain("target.com").state is DomainState.REGISTERED

    # 第 2 步：进入 pendingDelete，预测出释放时间
    storage.update_domain("target.com", next_check_at=utcnow() - timedelta(seconds=1))
    rdap_server.set(
        "target.com",
        rdap_payload(statuses=["pending delete"],
                     changed=(utcnow() - timedelta(days=4)).isoformat()),
    )
    await engine.run_once()
    item = storage.get_domain("target.com")
    assert item.state is DomainState.PENDING_DELETE
    assert item.drop_at is not None

    # 第 3 步：域名被释放（RDAP 返回 404）→ 抢注
    storage.update_domain("target.com", next_check_at=utcnow() - timedelta(seconds=1))
    rdap_server.set("target.com", None)
    await engine.run_once()

    item = storage.get_domain("target.com")
    assert item.state is DomainState.ACQUIRED
    assert item.attempts == 3           # 前两次「还没释放」，第三次成功
    assert storage.spend_today() == 11.0
    assert item.enabled is False        # stop_after_success 生效，摘出监控

    kinds = [event.kind for event in storage.recent_events(50)]
    assert "state_change" in kinds
    assert "available" in kinds
    assert "acquired" in kinds

    joined = "\n".join(telegram.messages)
    assert "抢注成功" in joined and "target.com" in joined


# ------------------------------------------------------------------ 冲刺循环

class StubProbe:
    """可脚本化的 DNS 探测替身。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.usable = True

    async def probe(self, domain):
        self.calls += 1
        return self.results.pop(0) if self.results else ProbeResult.DELEGATED


async def test_sprint_task_starts_and_stops_with_phase(rdap_server, storage):
    engine = build_engine(rdap_server, storage)
    drop_at = utcnow() + timedelta(seconds=30)

    engine._sync_sprint("target.com", Phase.SPRINT, drop_at)
    assert "target.com" in engine._sprints

    engine._sync_sprint("target.com", Phase.NEAR, drop_at)
    assert "target.com" not in engine._sprints
    await engine._cancel_sprints()


async def test_sprint_buys_on_dns_nxdomain(rdap_server, storage):
    """冲刺核心路径：DNS 看到 NXDOMAIN 就直接下单，不等 RDAP。"""
    engine = build_engine(
        rdap_server, storage,
        dns={"enabled": True, "interval": 0.01},
        poll={"jitter": 0.0, "sprint_tail": 1.0},
        purchase={"enabled": True, "dry_run": True, "skip_rdap_confirm": True,
                  "attempt_interval": 0, "attempt_concurrency": 1},
    )
    engine.probe = StubProbe([ProbeResult.DELEGATED, ProbeResult.NXDOMAIN])
    storage.upsert_domain("target.com")

    await engine._sprint("target.com", utcnow())

    assert storage.get_domain("target.com").state is DomainState.ACQUIRED
    assert "drop_detected" in [event.kind for event in storage.recent_events(20)]
    # RDAP 完全没被调用 —— 冲刺时就是要省掉这一个往返
    assert rdap_server.calls == []


async def test_sprint_rdap_confirm_rejects_false_positive(rdap_server, storage):
    """skip_rdap_confirm=false 时，RDAP 说还注册着就不下单（DNS 假阳性防护）。"""
    rdap_server.set("target.com", rdap_payload())      # RDAP 说：还注册着
    engine = build_engine(
        rdap_server, storage,
        dns={"enabled": True, "interval": 0.01},
        poll={"jitter": 0.0, "sprint_tail": 0.3},
        purchase={"enabled": True, "dry_run": True, "skip_rdap_confirm": False},
    )
    engine.probe = StubProbe([ProbeResult.NXDOMAIN, ProbeResult.NXDOMAIN])
    storage.upsert_domain("target.com")

    await engine._sprint("target.com", utcnow())

    assert storage.has_successful_purchase("target.com") is False
    assert rdap_server.calls                          # 确实做了复核


# --------------------------------------------------------------- 多注册商通道

def attach_pool(engine, *registrars):
    """给引擎换上一个多通道池（setter 会重建 pool）。"""
    from domain_monitor.registrars.pool import RegistrarPool

    engine.registrar = RegistrarPool(list(registrars))
    return engine.pool


async def test_multi_registrar_buys_from_cheapest(rdap_server, storage):
    from tests.test_pool import FakeRegistrar

    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "max_price": 50,
                  "compare_prices": True, "attempt_interval": 0,
                  "attempt_concurrency": 1, "max_attempts": 4},
    )
    storage.upsert_domain("target.com")
    pricey = FakeRegistrar("pricey", price=30.0, results=[("ok", "")])
    cheap = FakeRegistrar("cheap", price=9.0, results=[("ok", "")])
    attach_pool(engine, pricey, cheap)

    await engine.run_once()

    item = storage.get_domain("target.com")
    assert item.state is DomainState.ACQUIRED
    # 便宜的那家被提到队首，所以它先出手
    assert cheap.register_calls >= 1
    assert storage.spend_today() == 9.0
    assert "price_compare" in [event.kind for event in storage.recent_events(20)]


async def test_multi_registrar_rejects_when_all_over_limit(rdap_server, storage):
    from tests.test_pool import FakeRegistrar

    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "max_price": 10,
                  "compare_prices": True},
    )
    storage.upsert_domain("target.com")
    attach_pool(engine, FakeRegistrar("a", price=30.0), FakeRegistrar("b", price=45.0))

    await engine.run_once()

    assert storage.has_successful_purchase("target.com") is False
    assert "price_reject" in [event.kind for event in storage.recent_events(20)]


async def test_one_dead_channel_does_not_stop_the_others(rdap_server, storage):
    """一家余额不足时，抢注要继续用其他通道，而不是整轮放弃。"""
    from tests.test_pool import FakeRegistrar

    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "max_price": 50,
                  "compare_prices": False, "check_price_first": False,
                  "attempt_interval": 0, "attempt_concurrency": 1, "max_attempts": 6},
    )
    storage.upsert_domain("target.com")
    broke = FakeRegistrar("broke", results=[("fatal", "insufficient funds")])
    good = FakeRegistrar("good", price=9.0,
                         results=[("retry", "not available"), ("ok", "")])
    pool = attach_pool(engine, broke, good)

    await engine.run_once()

    assert storage.get_domain("target.com").state is DomainState.ACQUIRED
    assert broke.register_calls == 1              # 只试了一次就被摘掉
    assert "broke" in pool.disabled_reasons


async def test_all_channels_down_aborts(rdap_server, storage):
    from tests.test_pool import FakeRegistrar

    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "max_price": 50,
                  "compare_prices": False, "check_price_first": False,
                  "attempt_interval": 0, "attempt_concurrency": 1, "max_attempts": 20},
    )
    storage.upsert_domain("target.com")
    attach_pool(
        engine,
        FakeRegistrar("a", results=[("fatal", "unauthorized")]),
        FakeRegistrar("b", results=[("fatal", "insufficient funds")]),
    )

    await engine.run_once()

    assert storage.has_successful_purchase("target.com") is False
    events = storage.recent_events(20)
    assert "acquire_abort" in [event.kind for event in events]
    assert any("均已停用" in event.message for event in events)


async def test_sprint_skips_price_comparison(rdap_server, storage):
    """冲刺是毫秒级竞争：不比价，直接开抢。"""
    from tests.test_pool import FakeRegistrar

    engine = build_engine(
        rdap_server, storage,
        dns={"enabled": True, "interval": 0.01},
        poll={"jitter": 0.0, "sprint_tail": 1.0},
        purchase={"enabled": True, "dry_run": False, "max_price": 50,
                  "compare_prices": True, "check_price_first": True,
                  "skip_rdap_confirm": True, "attempt_interval": 0,
                  "attempt_concurrency": 1, "max_attempts": 3},
    )
    winner = FakeRegistrar("chan", price=9.0, results=[("ok", "")])
    attach_pool(engine, winner, FakeRegistrar("other", price=8.0))
    engine.probe = StubProbe([ProbeResult.NXDOMAIN])
    storage.upsert_domain("target.com")

    await engine._sprint("target.com", utcnow())

    assert storage.get_domain("target.com").state is DomainState.ACQUIRED
    assert winner.check_calls == 0      # 一次查价都没做，省下的都是时间


async def test_registrar_setter_rebuilds_pool(rdap_server, storage):
    """engine.registrar = X 必须真的生效，不能是静默无效赋值。"""
    from tests.test_pool import FakeRegistrar

    engine = build_engine(rdap_server, storage)
    replacement = FakeRegistrar("replacement")
    engine.registrar = replacement

    assert engine.pool.primary is replacement
    assert engine.registrar is replacement


async def test_parallel_registrars_off_only_hits_cheapest(rdap_server, storage):
    """parallel_registrars=false 时不并发骚扰所有注册商，只打最便宜那家。"""
    from tests.test_pool import FakeRegistrar

    rdap_server.set("target.com", None)
    engine = build_engine(
        rdap_server, storage,
        purchase={"enabled": True, "dry_run": False, "max_price": 50,
                  "compare_prices": True, "parallel_registrars": False,
                  "attempt_interval": 0, "attempt_concurrency": 1, "max_attempts": 3},
    )
    storage.upsert_domain("target.com")
    pricey = FakeRegistrar("pricey", price=30.0, results=[("ok", "")])
    cheap = FakeRegistrar("cheap", price=9.0, results=[("ok", "")])
    attach_pool(engine, pricey, cheap)

    await engine.run_once()

    assert storage.get_domain("target.com").state is DomainState.ACQUIRED
    assert cheap.register_calls == 1
    assert pricey.register_calls == 0      # 贵的那家一次都没被打扰
