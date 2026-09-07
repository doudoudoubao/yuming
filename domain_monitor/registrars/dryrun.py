"""演练用的假注册商。

默认 provider，永远不会真的花钱。可以通过 options 模拟各种场景，
方便在接真实 API 之前把整条抢注链路跑通：

    registrar:
      provider: dryrun
      options:
        price: 9.99          # 假装的价格
        fail_times: 3        # 前 3 次下单失败（模拟域名还没释放）
        always_fail: false   # 一直失败
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from ..config import PurchaseConfig
from ..models import Availability, RegistrationResult
from .base import Registrar

logger = logging.getLogger(__name__)


class DryRunRegistrar(Registrar):
    name = "dryrun"
    supports_price = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._attempts: dict[str, int] = {}

    async def ping(self) -> tuple[bool, str]:
        return True, "dryrun: 演练模式，不会产生任何真实下单"

    async def check(self, domain: str) -> Availability:
        return Availability(
            domain=domain,
            available=bool(self.options.get("available", True)),
            price=float(self.options.get("price", 9.99)),
            currency=str(self.options.get("currency", "USD")),
        )

    async def register(
        self, domain: str, purchase: PurchaseConfig, *, years: int | None = None
    ) -> RegistrationResult:
        latency = float(self.options.get("latency", 0.0))
        if latency:
            await asyncio.sleep(latency)

        count = self._attempts.get(domain, 0) + 1
        self._attempts[domain] = count

        if self.options.get("always_fail"):
            return self.failure(domain, "演练：注册商返回 domain not available", retryable=True)
        if count <= int(self.options.get("fail_times", 0)):
            return self.failure(
                domain,
                f"演练：第 {count} 次尝试，域名尚未释放(not available)",
                retryable=True,
            )

        price = float(self.options.get("price", 9.99))
        logger.info("[dryrun] 假装注册成功 %s（%d 年，%.2f）", domain, self.years_for(purchase, years), price)
        return RegistrationResult(
            domain=domain,
            success=True,
            provider=self.name,
            order_id=f"dryrun-{uuid.uuid4().hex[:12]}",
            price=price,
            currency=str(self.options.get("currency", "USD")),
            message="演练模式下单成功（未产生真实交易）",
        )
