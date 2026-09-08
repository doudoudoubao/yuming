"""Namecheap 适配器。

    registrar:
      provider: namecheap
      options:
        api_user: "${NC_API_USER}"
        api_key: "${NC_API_KEY}"
        username: "${NC_API_USER}"    # 一般与 api_user 相同
        client_ip: "1.2.3.4"          # 必须是已在后台白名单里的出口 IP
        sandbox: false
      contact: { ... 同 godaddy ... }

Namecheap 要求：账户余额 ≥ 50 美元或有过消费记录、API 开关打开、
并且**调用方 IP 必须加入白名单**——client_ip 填错是最常见的失败原因。
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

LIVE_URL = "https://api.namecheap.com/xml.response"
SANDBOX_URL = "https://api.sandbox.namecheap.com/xml.response"
NS = {"nc": "http://api.namecheap.com/xml.response"}

# 四类联系人用的是同一份资料，Namecheap 要求逐个前缀重复提交
CONTACT_PREFIXES = ("Registrant", "Tech", "Admin", "AuxBilling")


class NamecheapRegistrar(Registrar):
    name = "namecheap"
    display_name = "Namecheap"
    signup_url = "https://ap.www.namecheap.com/settings/tools/apiaccess/"
    payment = "账户余额（需预先充值）"
    required_options = (
        "api_user",
        "api_key",
        "client_ip",
    )
    needs_contact = True
    notes = (
        "必须把服务器出口 IP 加进后台白名单，client_ip 填错是最常见的失败原因",
        "查出口 IP：curl ifconfig.me",
        "开通 API 需账户有过消费或余额达标",
    )
    supports_price = True

    @property
    def base_url(self) -> str:
        return SANDBOX_URL if self.options.get("sandbox") else LIVE_URL

    def _global_params(self, command: str) -> dict[str, str]:
        api_user = self.require_option("api_user")
        return {
            "ApiUser": api_user,
            "ApiKey": self.require_option("api_key"),
            "UserName": str(self.options.get("username") or api_user),
            "ClientIp": self.require_option("client_ip"),
            "Command": command,
        }

    async def _call(self, command: str, **params: Any) -> ElementTree.Element:
        query = {
            **self._global_params(command),
            **{key: str(value) for key, value in params.items() if value not in (None, "")},
        }
        try:
            # 联系人字段很多，用 POST 避免超长 URL
            response = await self.client.post(self.base_url, data=query)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RegistrarError(f"namecheap 请求失败: {exc}") from exc

        try:
            root = ElementTree.fromstring(response.text)
        except ElementTree.ParseError as exc:
            raise RegistrarError(f"namecheap 返回体不是合法 XML: {exc}") from exc

        if root.get("Status", "").upper() == "ERROR":
            errors = [
                (node.text or "").strip()
                for node in root.iter()
                if node.tag.endswith("}Error") or node.tag == "Error"
            ]
            raise RegistrarError("; ".join(item for item in errors if item) or "namecheap 未知错误")
        return root

    @staticmethod
    def _find(root: ElementTree.Element, tag: str) -> ElementTree.Element | None:
        node = root.find(f".//nc:{tag}", NS)
        return node if node is not None else root.find(f".//{tag}")

    async def ping(self) -> tuple[bool, str]:
        try:
            await self._call("namecheap.domains.getList", PageSize="10")
        except RegistrarError as exc:
            return False, str(exc)
        env = "沙箱" if self.options.get("sandbox") else "生产环境"
        return True, f"namecheap 连接正常（{env}，ClientIp={self.options.get('client_ip')}）"

    async def check(self, domain: str) -> Availability:
        try:
            root = await self._call("namecheap.domains.check", DomainList=domain)
        except RegistrarError as exc:
            return Availability(domain=domain, available=None, error=str(exc))

        node = self._find(root, "DomainCheckResult")
        if node is None:
            return Availability(domain=domain, available=None, error="namecheap 未返回结果")
        price = node.get("PremiumRegistrationPrice")
        return Availability(
            domain=domain,
            available=node.get("Available", "").lower() == "true",
            price=float(price) if price and price != "0" else None,
            premium=node.get("IsPremiumName", "").lower() == "true",
        )

    def _contact_params(self) -> dict[str, str]:
        base = {
            "FirstName": self.require_contact("first_name"),
            "LastName": self.require_contact("last_name"),
            "Address1": self.require_contact("address1", "address"),
            "City": self.require_contact("city"),
            "StateProvince": self.contact_field("state", "province", default="N/A"),
            "PostalCode": self.require_contact("postal_code", "zip"),
            "Country": self.require_contact("country"),
            "Phone": self.require_contact("phone"),
            "EmailAddress": self.require_contact("email"),
        }
        if self.contact_field("organization"):
            base["OrganizationName"] = self.contact_field("organization")
        if self.contact_field("address2"):
            base["Address2"] = self.contact_field("address2")

        params: dict[str, str] = {}
        for prefix in CONTACT_PREFIXES:
            for key, value in base.items():
                params[f"{prefix}{key}"] = value
        return params

    async def register(
        self, domain: str, purchase: PurchaseConfig, *, years: int | None = None
    ) -> RegistrationResult:
        try:
            params: dict[str, Any] = {
                "DomainName": domain,
                "Years": self.years_for(purchase, years),
                **self._contact_params(),
            }
        except RegistrarError as exc:
            return self.failure(domain, str(exc), retryable=False)

        if purchase.whois_privacy:
            params["AddFreeWhoisguard"] = "yes"
            params["WGEnabled"] = "yes"
        if purchase.nameservers:
            params["Nameservers"] = ",".join(purchase.nameservers)

        try:
            root = await self._call("namecheap.domains.create", **params)
        except RegistrarError as exc:
            message = str(exc)
            # 「域名不可注册」在冲刺中是常态，可以继续重试
            fatal = any(
                hint in message.lower()
                for hint in ("ip", "authenticat", "insufficient", "funds", "api key", "not enabled")
            )
            return self.failure(domain, message, retryable=not fatal)

        node = self._find(root, "DomainCreateResult")
        if node is None:
            return self.failure(domain, "namecheap 未返回 DomainCreateResult")
        if node.get("Registered", "").lower() != "true":
            return self.failure(domain, f"namecheap 注册未成功: {node.attrib}")

        charged = node.get("ChargedAmount")
        return RegistrationResult(
            domain=domain,
            success=True,
            provider=self.name,
            order_id=node.get("OrderID") or node.get("TransactionID"),
            price=float(charged) if charged else None,
            currency="USD",
            message="注册成功",
            raw=dict(node.attrib),
        )
