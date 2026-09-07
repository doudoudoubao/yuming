"""注册商适配层的公共接口。

每家注册商只需要实现两个动作：
* ``check``    —— 查可注册性 / 价格（可选，不支持就返回 available=None）
* ``register`` —— 真正下单

``register`` 里最关键的是 **retryable 的判定**：抢注是循环下单，
「域名还没释放」要能继续重试，而「余额不足 / 认证失败 / 域名已被别人抢走」
必须立刻停下来，否则会空转几百次甚至反复扣钱。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import PurchaseConfig, RegistrarConfig
from ..models import Availability, RegistrationResult

logger = logging.getLogger(__name__)

# 命中这些关键字说明「域名当前不可注册」——冲刺时属于正常情况，继续重试
NOT_AVAILABLE_HINTS = (
    "not available", "unavailable", "already registered", "taken",
    "not registrable", "domain is not available", "已被注册", "不可注册",
)

# 命中这些说明是配置/账户层面的硬错误——立刻停手
FATAL_HINTS = (
    "insufficient", "not enough", "balance", "unauthorized", "authentication",
    "invalid api", "api key", "forbidden", "permission", "suspended",
    "余额不足", "认证失败", "无权限",
)


class RegistrarError(Exception):
    """注册商调用异常。"""


class Registrar:
    """注册商适配器基类。"""

    name = "base"
    supports_price = False

    def __init__(self, config: RegistrarConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.options: dict[str, Any] = dict(config.options or {})
        self.contact: dict[str, Any] = dict(config.contact or {})
        self._client = client
        self._owns_client = client is None

    # ----------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout)
            self._owns_client = True

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "Registrar":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RegistrarError(f"{self.name}: 适配器未初始化，请先 await start()")
        return self._client

    # -------------------------------------------------------------------- 接口

    async def ping(self) -> tuple[bool, str]:
        """连通性 / 凭据自检，供 `test` 子命令使用。"""
        return True, f"{self.name}: 未实现自检"

    async def check(self, domain: str) -> Availability:
        """查询可注册性与价格；不支持时返回 available=None。"""
        return Availability(domain=domain, available=None)

    async def register(
        self, domain: str, purchase: PurchaseConfig, *, years: int | None = None
    ) -> RegistrationResult:
        raise NotImplementedError

    # ------------------------------------------------------------------ 工具

    def require_option(self, key: str) -> str:
        value = self.options.get(key)
        if not value:
            raise RegistrarError(
                f"{self.name}: 缺少必填配置 registrar.options.{key}"
            )
        return str(value)

    def years_for(self, purchase: PurchaseConfig, years: int | None) -> int:
        return max(1, int(years or purchase.years or 1))

    def failure(
        self,
        domain: str,
        message: str,
        *,
        raw: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> RegistrationResult:
        """统一构造失败结果，并自动判定是否值得重试。"""
        lowered = message.lower()
        if retryable is None:
            if any(hint in lowered for hint in FATAL_HINTS):
                retryable = False
            elif any(hint in lowered for hint in NOT_AVAILABLE_HINTS):
                retryable = True
            else:
                retryable = True
        return RegistrationResult(
            domain=domain,
            success=False,
            provider=self.name,
            message=message,
            retryable=retryable,
            raw=raw,
        )

    def contact_field(self, *names: str, default: str = "") -> str:
        for name in names:
            value = self.contact.get(name)
            if value:
                return str(value)
        return default

    def require_contact(self, *names: str) -> str:
        value = self.contact_field(*names)
        if not value:
            raise RegistrarError(
                f"{self.name}: 缺少注册人信息 registrar.contact.{names[0]}"
            )
        return value
