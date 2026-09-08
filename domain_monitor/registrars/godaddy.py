"""GoDaddy 适配器。

    registrar:
      provider: godaddy
      options:
        api_key: "${GODADDY_KEY}"
        api_secret: "${GODADDY_SECRET}"
        ote: false            # true 用 OTE 沙箱环境
      contact:
        first_name: San
        last_name: Zhang
        email: you@example.com
        phone: "+86.13800138000"
        address1: "XX 路 1 号"
        city: Shanghai
        state: Shanghai
        postal_code: "200000"
        country: CN

注意：GoDaddy 对生产环境 API 有账户门槛（需持有一定数量域名），
申请前先在 OTE 环境把流程跑通。价格字段是**微单位**（1000000 = 1 美元）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import PurchaseConfig
from ..models import Availability, RegistrationResult
from ..utils import iso, tld_of, utcnow
from .base import Registrar, RegistrarError

logger = logging.getLogger(__name__)

LIVE_BASE = "https://api.godaddy.com"
OTE_BASE = "https://api.ote-godaddy.com"
MICRO = 1_000_000.0


class GoDaddyRegistrar(Registrar):
    name = "godaddy"
    display_name = "GoDaddy"
    signup_url = "https://developer.godaddy.com"
    payment = "账户绑定的支付方式"
    required_options = (
        "api_key",
        "api_secret",
    )
    needs_contact = True
    notes = (
        "生产环境 API 有账户门槛（需持有一定数量域名）",
        "先用 ote: true 在沙箱把流程跑通",
    )
    supports_price = True

    @property
    def base_url(self) -> str:
        return OTE_BASE if self.options.get("ote") else LIVE_BASE

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"sso-key {self.require_option('api_key')}:{self.require_option('api_secret')}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.client.request(
                method, f"{self.base_url}{path}", headers=self.headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise RegistrarError(f"godaddy 请求失败: {exc}") from exc

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"HTTP {response.status_code}: {response.text[:200]}"
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("code") or ""
            fields = payload.get("fields") or []
            if fields:
                details = "; ".join(
                    f"{item.get('path')}: {item.get('message')}"
                    for item in fields
                    if isinstance(item, dict)
                )
                message = f"{message} ({details})" if message else details
            if message:
                return f"HTTP {response.status_code}: {message}"
        return f"HTTP {response.status_code}: {response.text[:200]}"

    async def ping(self) -> tuple[bool, str]:
        try:
            response = await self._request("GET", "/v1/domains", params={"limit": 1})
        except RegistrarError as exc:
            return False, str(exc)
        if response.status_code == 200:
            env = "OTE 沙箱" if self.options.get("ote") else "生产环境"
            return True, f"godaddy 连接正常（{env}）"
        return False, f"godaddy 自检失败 {self._error_message(response)}"

    async def check(self, domain: str) -> Availability:
        try:
            response = await self._request(
                "GET",
                "/v1/domains/available",
                params={"domain": domain, "checkType": "FAST", "forTransfer": "false"},
            )
        except RegistrarError as exc:
            return Availability(domain=domain, available=None, error=str(exc))

        if response.status_code != 200:
            # 404/422 通常代表「不可注册」而不是错误
            if response.status_code in (404, 422):
                return Availability(domain=domain, available=False)
            return Availability(domain=domain, available=None,
                                error=self._error_message(response))
        try:
            payload = response.json()
        except ValueError as exc:
            return Availability(domain=domain, available=None, error=str(exc))

        price = payload.get("price")
        return Availability(
            domain=domain,
            available=bool(payload.get("available")),
            price=float(price) / MICRO if isinstance(price, (int, float)) else None,
            currency=str(payload.get("currency", "USD")),
            raw=payload,
        )

    async def _agreement_keys(self, domain: str, privacy: bool) -> list[str]:
        """下单必须带上当前生效的协议 key，动态拉取，失败就用通用默认值。"""
        try:
            response = await self._request(
                "GET",
                "/v1/domains/agreements",
                params={
                    "tlds": tld_of(domain),
                    "privacy": "true" if privacy else "false",
                },
            )
            if response.status_code == 200:
                payload = response.json()
                keys = [
                    str(item["agreementKey"])
                    for item in payload
                    if isinstance(item, dict) and item.get("agreementKey")
                ]
                if keys:
                    return keys
        except (RegistrarError, ValueError) as exc:
            logger.warning("godaddy 拉取协议 key 失败: %s，使用默认 DNRA", exc)
        return ["DNRA"]

    def _contact(self) -> dict[str, Any]:
        return {
            "nameFirst": self.require_contact("first_name"),
            "nameLast": self.require_contact("last_name"),
            "email": self.require_contact("email"),
            "phone": self.require_contact("phone"),
            "addressMailing": {
                "address1": self.require_contact("address1", "address"),
                "city": self.require_contact("city"),
                "state": self.contact_field("state", "province", default=""),
                "postalCode": self.require_contact("postal_code", "zip"),
                "country": self.require_contact("country"),
            },
            **(
                {"organization": self.contact_field("organization")}
                if self.contact_field("organization")
                else {}
            ),
        }

    async def register(
        self, domain: str, purchase: PurchaseConfig, *, years: int | None = None
    ) -> RegistrationResult:
        try:
            contact = self._contact()
            agreement_keys = await self._agreement_keys(domain, purchase.whois_privacy)
        except RegistrarError as exc:
            return self.failure(domain, str(exc), retryable=False)

        body: dict[str, Any] = {
            "domain": domain,
            "period": self.years_for(purchase, years),
            "privacy": bool(purchase.whois_privacy),
            "renewAuto": bool(purchase.auto_renew),
            "consent": {
                "agreedAt": iso(utcnow()),
                "agreedBy": str(self.options.get("agreed_by", "domain-monitor")),
                "agreementKeys": agreement_keys,
            },
            "contactAdmin": contact,
            "contactBilling": contact,
            "contactRegistrant": contact,
            "contactTech": contact,
        }
        if purchase.nameservers:
            body["nameServers"] = list(purchase.nameservers)

        try:
            response = await self._request("POST", "/v1/domains/purchase", json=body)
        except RegistrarError as exc:
            return self.failure(domain, str(exc))

        if response.status_code in (200, 201, 202):
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            total = payload.get("total")
            return RegistrationResult(
                domain=domain,
                success=True,
                provider=self.name,
                order_id=str(payload.get("orderId", "")) or None,
                price=float(total) / MICRO if isinstance(total, (int, float)) else None,
                currency=str(payload.get("currency", "USD")),
                message="注册成功",
                raw=payload,
            )

        message = self._error_message(response)
        # 401/403 是凭据问题，400/422 多半是资料格式问题——都别再重试了
        fatal = response.status_code in (401, 403, 400, 422)
        return self.failure(domain, message, retryable=not fatal)
