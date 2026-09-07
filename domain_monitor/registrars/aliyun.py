"""阿里云（万网）域名适配器。

国内用户最常用的通道，支持 .cn 等国别域名。

    registrar:
      provider: aliyun
      options:
        access_key_id: "${ALIYUN_AK}"
        access_key_secret: "${ALIYUN_SK}"
        registrant_profile_id: "123456"   # 后台「信息模板」的 ID（必须已实名认证）
        region: cn-hangzhou

工作方式：``SaveSingleTaskForCreatingOrderActivate`` 会创建一个注册任务，
阿里云从**账户余额**扣款完成下单，所以事前必须充值 + 准备好实名信息模板。
接口返回的是 TaskNo，可以再用 QueryTaskDetailList 查明细。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from ..config import PurchaseConfig
from ..models import Availability, RegistrationResult
from ..utils import utcnow
from .base import Registrar, RegistrarError

logger = logging.getLogger(__name__)

ENDPOINT = "https://domain.aliyuncs.com"
API_VERSION = "2018-01-29"


def _percent_encode(value: str) -> str:
    """阿里云 RPC 签名要求的编码规则（RFC3986 + 三个特例替换）。"""
    return (
        quote(str(value), safe="")
        .replace("+", "%20")
        .replace("*", "%2A")
        .replace("%7E", "~")
    )


def sign_params(params: dict[str, Any], secret: str, method: str = "GET") -> str:
    """按 signature v1（HMAC-SHA1）计算签名。"""
    canonical = "&".join(
        f"{_percent_encode(key)}={_percent_encode(params[key])}"
        for key in sorted(params)
    )
    string_to_sign = f"{method}&{_percent_encode('/')}&{_percent_encode(canonical)}"
    digest = hmac.new(
        f"{secret}&".encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


class AliyunRegistrar(Registrar):
    name = "aliyun"
    supports_price = True

    async def _call(self, action: str, **params: Any) -> dict[str, Any]:
        secret = self.require_option("access_key_secret")
        query: dict[str, Any] = {
            "Action": action,
            "Format": "JSON",
            "Version": API_VERSION,
            "AccessKeyId": self.require_option("access_key_id"),
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex,
            "Timestamp": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            **{key: value for key, value in params.items() if value not in (None, "")},
        }
        query["Signature"] = sign_params(query, secret)

        try:
            response = await self.client.get(ENDPOINT, params=query)
        except httpx.HTTPError as exc:
            raise RegistrarError(f"aliyun 请求失败: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise RegistrarError(f"aliyun 返回体不是合法 JSON: {exc}") from exc

        if response.status_code >= 400 or payload.get("Code"):
            raise RegistrarError(
                f"aliyun {payload.get('Code', response.status_code)}: "
                f"{payload.get('Message', response.text[:200])}"
            )
        return payload

    async def ping(self) -> tuple[bool, str]:
        try:
            await self._call("QueryDomainList", PageNum=1, PageSize=1)
        except RegistrarError as exc:
            return False, str(exc)
        return True, "aliyun 连接正常"

    async def check(self, domain: str) -> Availability:
        try:
            payload = await self._call(
                "CheckDomain", DomainName=domain, FeeCommand="create", FeePeriod=1
            )
        except RegistrarError as exc:
            return Availability(domain=domain, available=None, error=str(exc))

        avail = str(payload.get("Avail", "")).strip()
        price = payload.get("Price")
        return Availability(
            domain=domain,
            available=avail == "1" if avail in ("0", "1") else None,
            price=float(price) if isinstance(price, (int, float)) else None,
            currency="CNY",
            premium=str(payload.get("Premium", "")).lower() in ("true", "1"),
            raw=payload,
            error=str(payload.get("Reason", "")) or None,
        )

    async def register(
        self, domain: str, purchase: PurchaseConfig, *, years: int | None = None
    ) -> RegistrationResult:
        try:
            profile_id = self.require_option("registrant_profile_id")
        except RegistrarError as exc:
            return self.failure(domain, str(exc), retryable=False)

        try:
            payload = await self._call(
                "SaveSingleTaskForCreatingOrderActivate",
                DomainName=domain,
                SubscriptionDuration=self.years_for(purchase, years),
                RegistrantProfileId=profile_id,
                EnableDomainProxy="true" if purchase.whois_privacy else "false",
                PermitPremiumActivation="false",
            )
        except RegistrarError as exc:
            message = str(exc)
            lowered = message.lower()
            fatal = any(
                hint in lowered
                for hint in (
                    "signaturedoesnotmatch", "invalidaccesskeyid", "forbidden",
                    "insufficient", "balance", "realname", "profile",
                )
            )
            return self.failure(domain, message, retryable=not fatal)

        task_no = payload.get("TaskNo")
        if not task_no:
            return self.failure(domain, f"aliyun 未返回 TaskNo: {payload}", raw=payload)

        return RegistrationResult(
            domain=domain,
            success=True,
            provider=self.name,
            order_id=str(task_no),
            currency="CNY",
            message=f"已提交注册任务 TaskNo={task_no}（阿里云异步执行，请到控制台确认）",
            raw=payload,
        )
