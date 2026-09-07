"""测试夹具：全部离线，用 httpx.MockTransport 顶掉所有网络调用。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Callable

import httpx
import pytest

from domain_monitor.config import load_config
from domain_monitor.storage import Storage
from domain_monitor.utils import iso, utcnow

BOOTSTRAP = {
    "version": "1.0",
    "services": [
        [["com", "net"], ["https://rdap.verisign.com/com/v1/"]],
        [["io"], ["https://rdap.nic.io/"]],
    ],
}


# 到期时间必须相对「现在」计算，否则夹具会随时间推移而失效：
# 一个写死的过去日期会让默认域名被正确判成「已过期」，把测试搞挂。
FUTURE = iso(utcnow() + timedelta(days=200))
PAST = iso(utcnow() - timedelta(days=60))


def rdap_payload(
    *,
    statuses: list[str] | None = None,
    expiration: str | None = FUTURE,
    changed: str | None = None,
    registrar: str = "Example Registrar",
) -> dict[str, Any]:
    events = []
    if expiration:
        events.append({"eventAction": "expiration", "eventDate": expiration})
    if changed:
        events.append({"eventAction": "last changed", "eventDate": changed})
    return {
        "objectClassName": "domain",
        "ldhName": "target.com",
        "status": statuses if statuses is not None else ["client transfer prohibited"],
        "events": events,
        "entities": [
            {
                "roles": ["registrar"],
                "vcardArray": ["vcard", [["version", {}, "text", "4.0"],
                                         ["fn", {}, "text", registrar]]],
            }
        ],
        "nameservers": [{"ldhName": "ns1.example.com"}],
    }


class FakeRdapServer:
    """可以在测试中途改变域名状态的假 RDAP 服务器。"""

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any] | None] = {}
        self.calls: list[str] = []

    def set(self, domain: str, payload: dict[str, Any] | None) -> None:
        """payload=None 表示该域名未注册（404）。"""
        self.responses[domain] = payload

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "dns.json" in url:
            return httpx.Response(200, json=BOOTSTRAP)
        domain = url.rsplit("/domain/", 1)[-1].lower()
        self.calls.append(domain)
        if domain not in self.responses:
            return httpx.Response(404)
        payload = self.responses[domain]
        if payload is None:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)


@pytest.fixture
def rdap_server() -> FakeRdapServer:
    return FakeRdapServer()


@pytest.fixture
def rdap_client(rdap_server: FakeRdapServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(rdap_server.handler))


@pytest.fixture
def storage() -> Storage:
    store = Storage(":memory:")
    yield store
    store.close()


@pytest.fixture
def make_config() -> Callable[..., Any]:
    def _make(**overrides: Any) -> Any:
        base: dict[str, Any] = {
            "rdap": {"rps_per_host": 1000, "bootstrap_ttl": 3600},
            "dns": {"enabled": False},
            "telegram": {"enabled": False},
            "poll": {"jitter": 0.0, "concurrency": 4},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key].update(value)
            else:
                base[key] = value
        return load_config(data=base)

    return _make


class FakeTelegram:
    """记录所有发出去的消息，便于断言通知内容。"""

    def __init__(self, *, confirm: bool = True) -> None:
        self.messages: list[str] = []
        self.confirm = confirm
        self.confirm_calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            self.messages.append(body.get("text", ""))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "testbot"}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    async def confirm_purchase(self, domain: str, price, currency, timeout) -> bool:
        self.confirm_calls.append(domain)
        return self.confirm

    def stop(self) -> None:
        return None

    async def run(self) -> None:
        return None
