"""多注册商通道池。

两件事：

1. **比价** —— 并发问所有已配置的注册商要价格，挑最便宜的下单。
   数据来自注册商自己的 API，是你账户的**真实成交价**（含等级折扣、
   促销、溢价域名加价），比第三方比价站的挂牌价准。

2. **并发抢** —— 冲刺时同时向多家下单，先成功的算赢。
   同一个域名在注册局只能被注册一次，所以多通道并发**不会重复扣款**，
   但成功率显著提高——专业抢注商就是靠握着几十上百个注册商通道取胜的。

池子还会在一轮抢注里记住哪家已经废了（余额不足、认证失败），
把它摘掉继续用其他通道，而不是让一家的硬错误拖死整轮。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Sequence

from ..config import PurchaseConfig
from ..models import Availability, RegistrationResult
from .base import Registrar

logger = logging.getLogger(__name__)


class RegistrarPool:
    """一组注册商通道。单个注册商也走这条路径，只是池子里只有一个成员。"""

    def __init__(self, registrars: Iterable[Registrar]) -> None:
        self._registrars: list[Registrar] = list(registrars)
        if not self._registrars:
            raise ValueError("注册商池不能为空")
        self._assign_labels()
        self._disabled: dict[str, str] = {}

    def _assign_labels(self) -> None:
        """同一家配置多个账号时给个可区分的标签。"""
        seen: dict[str, int] = {}
        for registrar in self._registrars:
            count = seen.get(registrar.name, 0)
            seen[registrar.name] = count + 1
            registrar.label = registrar.name if count == 0 else f"{registrar.name}#{count + 1}"

    # ------------------------------------------------------------------ 基本属性

    def __len__(self) -> int:
        return len(self._registrars)

    def __iter__(self):
        return iter(self._registrars)

    @property
    def primary(self) -> Registrar:
        """主通道，用于展示和只需要一家时的场景。"""
        return self._registrars[0]

    @property
    def all(self) -> list[Registrar]:
        return list(self._registrars)

    @property
    def active(self) -> list[Registrar]:
        """还能用的通道（排除本轮已判定为硬错误的）。"""
        return [item for item in self._registrars if item.label not in self._disabled]

    @property
    def labels(self) -> list[str]:
        return [item.label for item in self._registrars]

    def disable(self, label: str, reason: str) -> None:
        if label not in self._disabled:
            logger.error("注册商通道 %s 已停用：%s", label, reason)
            self._disabled[label] = reason

    def reset(self) -> None:
        """新一轮抢注开始，恢复所有通道。"""
        self._disabled.clear()

    def prioritize(self, label: str) -> None:
        """把某个通道提到队首（比价选出最便宜的一家之后调用）。"""
        for index, item in enumerate(self._registrars):
            if item.label == label:
                self._registrars.insert(0, self._registrars.pop(index))
                return

    @property
    def disabled_reasons(self) -> dict[str, str]:
        return dict(self._disabled)

    # -------------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        await asyncio.gather(*(item.start() for item in self._registrars))

    async def close(self) -> None:
        await asyncio.gather(
            *(item.close() for item in self._registrars), return_exceptions=True
        )

    async def ping_all(self) -> list[tuple[Registrar, bool, str]]:
        results = await asyncio.gather(
            *(item.ping() for item in self._registrars), return_exceptions=True
        )
        output: list[tuple[Registrar, bool, str]] = []
        for registrar, result in zip(self._registrars, results):
            if isinstance(result, BaseException):
                output.append((registrar, False, f"自检异常: {result}"))
            else:
                output.append((registrar, result[0], result[1]))
        return output

    # ---------------------------------------------------------------------- 比价

    async def compare(self, domain: str) -> list[tuple[Registrar, Availability]]:
        """并发向所有支持查价的通道要价，按价格从低到高排序。

        查不到价 / 报错的排在最后，但仍然返回——它们依然可以用来下单。
        """
        targets = [item for item in self.active if item.supports_price]
        others = [item for item in self.active if not item.supports_price]

        results = await asyncio.gather(
            *(item.check(domain) for item in targets), return_exceptions=True
        )

        pairs: list[tuple[Registrar, Availability]] = []
        for registrar, result in zip(targets, results):
            if isinstance(result, BaseException):
                pairs.append(
                    (registrar, Availability(domain=domain, error=f"查价异常: {result}"))
                )
            else:
                pairs.append((registrar, result))

        for registrar in others:
            pairs.append(
                (registrar, Availability(domain=domain, error="该注册商不支持查价"))
            )

        # 有价格的按价格升序；没价格的（未知/报错）统一排到后面
        pairs.sort(key=lambda pair: (pair[1].price is None, pair[1].price or 0.0))
        return pairs

    async def cheapest(
        self, domain: str, *, max_price: float | None = None
    ) -> tuple[Registrar, Availability] | None:
        """挑一个「可注册且价格在上限内」的最便宜通道。

        全部查不到价时返回 None，调用方应当回退到主通道直接下单——
        查不到价不等于买不了。
        """
        for registrar, availability in await self.compare(domain):
            if availability.price is None:
                continue
            if availability.available is False:
                continue
            if max_price is not None and availability.price > max_price:
                continue
            return registrar, availability
        return None

    # ---------------------------------------------------------------------- 下单

    def plan_shots(
        self, concurrency: int, limit: int | None = None, *, parallel: bool = True
    ) -> list[Registrar]:
        """规划一轮要打哪些通道。

        单通道时按 concurrency 重复打同一家（保持单注册商时的原有行为）；
        多通道时轮转分配，并保证每家每轮至少打到一次。

        ``parallel=False`` 则只打队首那一家（比价后队首就是最便宜的），
        它被硬错误摘掉之后才轮到下一家——严格按价格顺序，不并发骚扰所有注册商。
        ``limit`` 用于收口剩余可用次数，避免超出 max_attempts。
        """
        channels = self.active
        if not channels:
            return []
        if not parallel:
            channels = channels[:1]
        if len(channels) == 1:
            shots = channels * max(1, concurrency)
        else:
            total = max(concurrency, len(channels))
            shots = [channels[index % len(channels)] for index in range(total)]
        if limit is not None:
            shots = shots[: max(1, limit)]
        return shots

    async def race_register(
        self,
        domain: str,
        purchase: PurchaseConfig,
        *,
        years: int,
        concurrency: int,
        limit: int | None = None,
        parallel: bool = True,
    ) -> tuple[RegistrationResult | None, list[RegistrationResult]]:
        """并发下单，返回 (成功的结果或 None, 本轮全部结果)。

        某家返回不可重试的硬错误时，只把**那一家**停用，其余通道继续。
        """
        shots = self.plan_shots(concurrency, limit, parallel=parallel)
        if not shots:
            return None, []

        results = await asyncio.gather(
            *(registrar.register(domain, purchase, years=years) for registrar in shots),
            return_exceptions=True,
        )

        collected: list[RegistrationResult] = []
        winner: RegistrationResult | None = None
        for registrar, result in zip(shots, results):
            if isinstance(result, BaseException):
                logger.error("%s 通过 %s 下单异常: %s", domain, registrar.label, result)
                continue
            result.provider = registrar.label
            collected.append(result)
            if result.success and winner is None:
                winner = result
            elif not result.success and not result.retryable:
                self.disable(registrar.label, result.message)

        return winner, collected


def build_pool(
    configs: Sequence[Any], *, client: Any = None, factory: Any = None
) -> RegistrarPool:
    """按配置列表构造通道池。"""
    from . import build_registrar

    make = factory or build_registrar
    return RegistrarPool([make(item, client=client) for item in configs])
