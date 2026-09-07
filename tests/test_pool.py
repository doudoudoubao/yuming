"""多注册商通道池：比价、并发抢、单通道熔断。"""

import httpx
import pytest

from domain_monitor.config import PurchaseConfig, RegistrarConfig
from domain_monitor.models import Availability, RegistrationResult
from domain_monitor.registrars import build_registrar
from domain_monitor.registrars.base import Registrar
from domain_monitor.registrars.pool import RegistrarPool


class FakeRegistrar(Registrar):
    """价格和下单结果都可脚本化的假注册商。"""

    supports_price = True

    def __init__(self, name, *, price=None, available=True, results=None, error=None):
        super().__init__(RegistrarConfig(provider=name))
        self.name = name
        self.label = name
        self._price = price
        self._available = available
        self._results = list(results or [])
        self._error = error
        self.check_calls = 0
        self.register_calls = 0

    async def start(self): return None
    async def close(self): return None

    async def check(self, domain):
        self.check_calls += 1
        if self._error:
            return Availability(domain=domain, error=self._error)
        return Availability(
            domain=domain, available=self._available, price=self._price, currency="USD"
        )

    async def register(self, domain, purchase, *, years=None):
        self.register_calls += 1
        if self._results:
            outcome = self._results.pop(0)
        else:
            outcome = ("retry", "not available")
        kind, message = outcome
        if kind == "ok":
            return RegistrationResult(domain=domain, success=True, provider=self.name,
                                      order_id=f"{self.name}-1", price=self._price)
        return self.failure(domain, message, retryable=(kind == "retry"))


def pool_of(*registrars):
    return RegistrarPool(list(registrars))


# ---------------------------------------------------------------------- 基础

def test_empty_pool_rejected():
    with pytest.raises(ValueError, match="不能为空"):
        RegistrarPool([])


def test_duplicate_providers_get_distinct_labels():
    pool = RegistrarPool([
        build_registrar(RegistrarConfig(provider="dryrun")),
        build_registrar(RegistrarConfig(provider="dryrun")),
        build_registrar(RegistrarConfig(provider="namesilo")),
    ])
    assert pool.labels == ["dryrun", "dryrun#2", "namesilo"]


def test_single_channel_repeats_for_concurrency():
    pool = pool_of(FakeRegistrar("a"))
    assert [item.label for item in pool.plan_shots(3)] == ["a", "a", "a"]


def test_multi_channel_covers_every_registrar():
    pool = pool_of(FakeRegistrar("a"), FakeRegistrar("b"), FakeRegistrar("c"))
    # 即使 concurrency 只有 1，每家每轮也至少打到一次
    assert sorted(item.label for item in pool.plan_shots(1)) == ["a", "b", "c"]


def test_plan_shots_respects_limit():
    pool = pool_of(FakeRegistrar("a"), FakeRegistrar("b"), FakeRegistrar("c"))
    assert len(pool.plan_shots(3, limit=2)) == 2


def test_disable_and_reset():
    pool = pool_of(FakeRegistrar("a"), FakeRegistrar("b"))
    pool.disable("a", "余额不足")
    assert [item.label for item in pool.active] == ["b"]
    assert pool.disabled_reasons == {"a": "余额不足"}
    pool.reset()
    assert len(pool.active) == 2


def test_prioritize_moves_channel_to_front():
    pool = pool_of(FakeRegistrar("a"), FakeRegistrar("b"), FakeRegistrar("c"))
    pool.prioritize("c")
    assert pool.labels == ["c", "a", "b"]
    assert pool.primary.label == "c"


def test_prioritize_unknown_label_is_noop():
    pool = pool_of(FakeRegistrar("a"), FakeRegistrar("b"))
    pool.prioritize("nope")
    assert pool.labels == ["a", "b"]


# ---------------------------------------------------------------------- 比价

async def test_compare_sorts_by_price():
    pool = pool_of(
        FakeRegistrar("expensive", price=13.5),
        FakeRegistrar("cheap", price=8.88),
        FakeRegistrar("mid", price=10.2),
    )
    quotes = await pool.compare("a.com")
    assert [item.label for item, _ in quotes] == ["cheap", "mid", "expensive"]


async def test_compare_puts_unpriced_last():
    pool = pool_of(
        FakeRegistrar("broken", error="API 挂了"),
        FakeRegistrar("cheap", price=9.0),
    )
    quotes = await pool.compare("a.com")
    assert quotes[0][0].label == "cheap"
    assert quotes[-1][0].label == "broken"
    assert quotes[-1][1].error == "API 挂了"


async def test_compare_includes_price_less_registrars():
    priceless = FakeRegistrar("exec")
    priceless.supports_price = False
    pool = pool_of(FakeRegistrar("cheap", price=9.0), priceless)

    quotes = await pool.compare("a.com")

    assert priceless.check_calls == 0          # 不支持查价的不去打扰它
    assert {item.label for item, _ in quotes} == {"cheap", "exec"}


async def test_cheapest_respects_max_price():
    pool = pool_of(FakeRegistrar("cheap", price=8.0), FakeRegistrar("pricey", price=99.0))
    assert (await pool.cheapest("a.com", max_price=50))[0].label == "cheap"
    assert await pool.cheapest("a.com", max_price=5) is None


async def test_cheapest_skips_unavailable():
    pool = pool_of(
        FakeRegistrar("cheap_taken", price=5.0, available=False),
        FakeRegistrar("pricier_free", price=9.0, available=True),
    )
    assert (await pool.cheapest("a.com", max_price=50))[0].label == "pricier_free"


async def test_cheapest_returns_none_when_no_prices():
    pool = pool_of(FakeRegistrar("x", error="boom"))
    assert await pool.cheapest("a.com", max_price=50) is None


# -------------------------------------------------------------------- 并发抢

async def test_race_returns_first_success():
    pool = pool_of(
        FakeRegistrar("slow", results=[("retry", "not available")]),
        FakeRegistrar("winner", price=9.0, results=[("ok", "")]),
    )
    winner, results = await pool.race_register("a.com", PurchaseConfig(), years=1, concurrency=2)

    assert winner is not None and winner.success
    assert winner.provider == "winner"        # provider 被改写成通道标签
    assert len(results) == 2


async def test_race_disables_only_the_failing_channel():
    """一家余额不足不该拖死整轮——把它摘掉，其余通道继续。"""
    pool = pool_of(
        FakeRegistrar("broke", results=[("fatal", "insufficient funds")]),
        FakeRegistrar("fine", results=[("retry", "not available")]),
    )
    winner, _ = await pool.race_register("a.com", PurchaseConfig(), years=1, concurrency=2)

    assert winner is None
    assert [item.label for item in pool.active] == ["fine"]
    assert "insufficient" in pool.disabled_reasons["broke"]


async def test_race_survives_adapter_exception():
    class Exploding(FakeRegistrar):
        async def register(self, domain, purchase, *, years=None):
            raise RuntimeError("适配器炸了")

    pool = pool_of(Exploding("boom"), FakeRegistrar("ok", results=[("ok", "")]))
    winner, results = await pool.race_register("a.com", PurchaseConfig(), years=1, concurrency=2)

    assert winner is not None and winner.success
    assert len(results) == 1      # 异常的那家不计入结果


async def test_race_with_no_active_channels():
    pool = pool_of(FakeRegistrar("a"))
    pool.disable("a", "挂了")
    winner, results = await pool.race_register("a.com", PurchaseConfig(), years=1, concurrency=1)
    assert winner is None and results == []


async def test_ping_all_reports_each_channel():
    class Boom(FakeRegistrar):
        async def ping(self):
            raise RuntimeError("连不上")

    pool = pool_of(FakeRegistrar("good"), Boom("bad"))
    results = await pool.ping_all()

    assert len(results) == 2
    assert results[1][1] is False
    assert "连不上" in results[1][2]


async def test_sequential_mode_uses_only_the_front_channel():
    """parallel=False：严格按价格顺序，只打队首那一家。"""
    pool = pool_of(FakeRegistrar("cheap"), FakeRegistrar("pricey"))
    shots = pool.plan_shots(3, parallel=False)
    assert {item.label for item in shots} == {"cheap"}


async def test_sequential_mode_falls_through_to_next_channel():
    """队首被硬错误摘掉后，才轮到下一家。"""
    pool = pool_of(
        FakeRegistrar("cheap", results=[("fatal", "insufficient funds")]),
        FakeRegistrar("backup", results=[("ok", "")]),
    )
    winner, _ = await pool.race_register(
        "a.com", PurchaseConfig(), years=1, concurrency=1, parallel=False
    )
    assert winner is None                       # 第一轮只打了 cheap，它废了
    assert [item.label for item in pool.active] == ["backup"]

    winner, _ = await pool.race_register(
        "a.com", PurchaseConfig(), years=1, concurrency=1, parallel=False
    )
    assert winner is not None and winner.provider == "backup"
