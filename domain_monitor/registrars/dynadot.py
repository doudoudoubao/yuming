"""Dynadot 适配器。

Dynadot 的 API 响应快、按次下单，抢注圈子里用得很多。

    registrar:
      provider: dynadot
      options:
        api_key: "${DYNADOT_API_KEY}"
        currency: usd

同样是**账户余额**扣款，记得先充值。API Key 需要在后台开启并绑定 IP 白名单。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import PurchaseConfig
from ..models import Availability, RegistrationResult
from .base import Registrar, RegistrarError

logger = logging.getLogger(__name__)

API_URL = "https://api.dynadot.com/api3.json"


def _header(payload: dict[str, Any], *names: str) -> dict[str, Any]:
    """Dynadot 不同接口的 header 节点名不一样，挨个试。"""
    for name in names:
        node = payload.get(name)
        if isinstance(node, dict):
            header = node.get(f"{name.replace('Response', '')}Header")
            if isinstance(header, dict):
                return header
            for key, value in node.items():
                if key.endswith("Header") and isinstance(value, dict):
                    return value
    return {}


def _response_code(header: dict[str, Any]) -> str:
    for key in ("ResponseCode", "SuccessCode", "Code"):
        if key in header:
            return str(header[key])
    return ""


class DynadotRegistrar(Registrar):
    name = "dynadot"
    supports_price = True

    async def _call(self, command: str, **params: Any) -> dict[str, Any]:
        query = {
            "key": self.require_option("api_key"),
            "command": command,
            **{key: value for key, value in params.items() if value not in (None, "")},
        }
        try:
            response = await self.client.get(API_URL, params=query)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise RegistrarError(f"dynadot 请求失败: {exc}") from exc
        except ValueError as exc:
            raise RegistrarError(f"dynadot 返回体不是合法 JSON: {exc}") from exc

    async def ping(self) -> tuple[bool, str]:
        try:
            payload = await self._call("account_info")
        except RegistrarError as exc:
            return False, str(exc)
        header = _header(payload, "AccountInfoResponse", "Response")
        if _response_code(header) == "0":
            return True, "dynadot 连接正常"
        return False, f"dynadot 返回 {header.get('Status')} {header.get('Error', '')}".strip()

    async def check(self, domain: str) -> Availability:
        try:
            payload = await self._call(
                "search",
                domain0=domain,
                show_price=1,
                currency=str(self.options.get("currency", "usd")),
            )
        except RegistrarError as exc:
            return Availability(domain=domain, available=None, error=str(exc))

        node = payload.get("SearchResponse") or {}
        results = node.get("SearchResults") or []
        if isinstance(results, dict):
            results = [results]
        for item in results:
            if str(item.get("DomainName", "")).lower() != domain:
                continue
            available = str(item.get("Available", "")).lower() == "yes"
            price_text = str(item.get("Price", "") or item.get("PriceList", "")).strip()
            price = None
            for token in price_text.replace(",", " ").split():
                try:
                    price = float(token.lstrip("$"))
                    break
                except ValueError:
                    continue
            return Availability(
                domain=domain,
                available=available,
                price=price,
                currency=str(self.options.get("currency", "usd")).upper(),
                premium=str(item.get("IsPremium", "")).lower() == "yes",
            )
        return Availability(domain=domain, available=None, error="dynadot 未返回该域名的结果")

    async def register(
        self, domain: str, purchase: PurchaseConfig, *, years: int | None = None
    ) -> RegistrationResult:
        params: dict[str, Any] = {
            "domain": domain,
            "duration": self.years_for(purchase, years),
            "currency": str(self.options.get("currency", "usd")),
        }
        if purchase.whois_privacy:
            params["privacy"] = 1
        if purchase.nameservers:
            for index, nameserver in enumerate(purchase.nameservers[:13]):
                params[f"ns{index}"] = nameserver
        if self.options.get("registrant_contact_id"):
            params["registrant_contact"] = self.options["registrant_contact_id"]

        try:
            payload = await self._call("register", **params)
        except RegistrarError as exc:
            return self.failure(domain, str(exc))

        node = payload.get("RegisterResponse") or {}
        header = _header(payload, "RegisterResponse", "Response") or node
        code = _response_code(header)
        status = str(header.get("Status", "")).lower()
        error = str(header.get("Error", "") or node.get("Error", ""))

        if code == "0" and status in ("success", "ok", ""):
            price = node.get("RegisterResult", {}).get("Price") if isinstance(
                node.get("RegisterResult"), dict
            ) else None
            return RegistrationResult(
                domain=domain,
                success=True,
                provider=self.name,
                order_id=str(node.get("OrderId") or header.get("OrderId") or "") or None,
                price=float(price) if price else None,
                currency=str(self.options.get("currency", "usd")).upper(),
                message=error or "注册成功",
                raw=payload,
            )
        return self.failure(domain, error or f"dynadot code={code} status={status}", raw=payload)
