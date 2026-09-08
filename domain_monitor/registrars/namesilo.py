"""NameSilo 适配器。

API 极简（GET + XML），价格便宜、下单快，是脚本抢注最常用的通道之一。

    registrar:
      provider: namesilo
      options:
        api_key: "${NAMESILO_API_KEY}"
        portfolio: ""          # 可选：归入某个组合
        sandbox: false

注意：NameSilo 走**账户余额**扣款，抢注前请预先充值，否则会返回 400 系列错误。
"""

from __future__ import annotations

import logging
from typing import Any
from xml.etree import ElementTree

import httpx

from ..config import PurchaseConfig
from ..models import Availability, RegistrationResult
from .base import Registrar, RegistrarError

logger = logging.getLogger(__name__)

LIVE_BASE = "https://www.namesilo.com/api"
SANDBOX_BASE = "https://sandbox.namesilo.com/api"

# 300 成功；301 成功但部分 NS 无效已改用官方 NS
SUCCESS_CODES = {"300", "301"}
# 302 下单成功但注册环节出错，需要人工介入，别再重试
MANUAL_CODES = {"302"}
FATAL_CODES = {"110", "10", "108", "200", "400", "401", "402", "403", "404", "405"}


def _text(node: ElementTree.Element | None, default: str = "") -> str:
    return (node.text or default).strip() if node is not None and node.text else default


class NameSiloRegistrar(Registrar):
    name = "namesilo"
    display_name = "NameSilo"
    signup_url = "https://www.namesilo.com"
    payment = "账户余额（需预先充值）"
    required_options = (
        "api_key",
    )
    needs_contact = False
    notes = (
        "后台 → API Manager 生成 API Key",
        "续费价与注册价接近，适合长期持有",
    )
    supports_price = True

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE if self.options.get("sandbox") else LIVE_BASE

    async def _call(self, operation: str, **params: Any) -> ElementTree.Element:
        query = {
            "version": "1",
            "type": "xml",
            "key": self.require_option("api_key"),
            **{key: value for key, value in params.items() if value not in (None, "")},
        }
        try:
            response = await self.client.get(f"{self.base_url}/{operation}", params=query)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RegistrarError(f"namesilo 请求失败: {exc}") from exc

        try:
            root = ElementTree.fromstring(response.text)
        except ElementTree.ParseError as exc:
            raise RegistrarError(f"namesilo 返回体不是合法 XML: {exc}") from exc

        reply = root.find("reply")
        if reply is None:
            raise RegistrarError("namesilo 返回体缺少 reply 节点")
        return reply

    async def ping(self) -> tuple[bool, str]:
        try:
            reply = await self._call("getAccountBalance")
        except RegistrarError as exc:
            return False, str(exc)
        code = _text(reply.find("code"))
        balance = _text(reply.find("balance"))
        if code in SUCCESS_CODES:
            return True, f"namesilo 连接正常，账户余额 {balance or '未知'}"
        return False, f"namesilo 返回 code={code} {_text(reply.find('detail'))}"

    async def check(self, domain: str) -> Availability:
        try:
            reply = await self._call("checkRegisterAvailability", domains=domain)
        except RegistrarError as exc:
            return Availability(domain=domain, available=None, error=str(exc))

        available_node = reply.find("available")
        if available_node is not None:
            for item in available_node.findall("domain"):
                if (item.text or "").strip().lower() == domain:
                    price = item.get("price")
                    return Availability(
                        domain=domain,
                        available=True,
                        price=float(price) if price else None,
                        currency="USD",
                        premium=item.get("premium") in ("1", "true"),
                    )

        unavailable_node = reply.find("unavailable")
        if unavailable_node is not None:
            for item in unavailable_node.findall("domain"):
                if (item.text or "").strip().lower() == domain:
                    return Availability(domain=domain, available=False)

        return Availability(domain=domain, available=None, error=_text(reply.find("detail")))

    async def register(
        self, domain: str, purchase: PurchaseConfig, *, years: int | None = None
    ) -> RegistrationResult:
        params: dict[str, Any] = {
            "domain": domain,
            "years": self.years_for(purchase, years),
            "private": 1 if purchase.whois_privacy else 0,
            "auto_renew": 1 if purchase.auto_renew else 0,
            "portfolio": self.options.get("portfolio"),
        }
        for index, nameserver in enumerate(purchase.nameservers[:13], start=1):
            params[f"ns{index}"] = nameserver

        try:
            reply = await self._call("registerDomain", **params)
        except RegistrarError as exc:
            return self.failure(domain, str(exc))

        code = _text(reply.find("code"))
        detail = _text(reply.find("detail")) or f"code={code}"
        raw = {"code": code, "detail": detail}

        if code in SUCCESS_CODES:
            amount = _text(reply.find("total_paid")) or _text(reply.find("price"))
            return RegistrationResult(
                domain=domain,
                success=True,
                provider=self.name,
                order_id=_text(reply.find("order_amount")) or _text(reply.find("order_id")) or None,
                price=float(amount) if amount else None,
                currency="USD",
                message=detail,
                raw=raw,
            )
        if code in MANUAL_CODES:
            return self.failure(
                domain, f"下单已提交但注册失败，需人工检查: {detail}", raw=raw, retryable=False
            )
        if code in FATAL_CODES:
            return self.failure(domain, f"namesilo 致命错误 code={code}: {detail}",
                                raw=raw, retryable=False)
        return self.failure(domain, f"namesilo code={code}: {detail}", raw=raw)
