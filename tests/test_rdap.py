from datetime import datetime, timezone

import httpx
import pytest

from domain_monitor.config import RdapConfig
from domain_monitor.models import DomainState
from domain_monitor.rdap import RdapClient, parse_rdap
from tests.conftest import FUTURE, rdap_payload


def test_parse_registered_domain():
    status = parse_rdap("target.com", rdap_payload())
    assert status.state == DomainState.REGISTERED
    assert status.registrar == "Example Registrar"
    assert status.expires_at is not None and status.expires_at > datetime.now(timezone.utc)
    assert status.nameservers == ["ns1.example.com"]


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["pending delete"], DomainState.PENDING_DELETE),
        (["pendingDelete"], DomainState.PENDING_DELETE),          # WHOIS 风格驼峰
        (["redemption period"], DomainState.REDEMPTION),
        (["redemptionPeriod"], DomainState.REDEMPTION),
        (["auto renew period"], DomainState.EXPIRED),
        (["client transfer prohibited"], DomainState.REGISTERED),
        # 同时有多个状态时，取生命周期最靠后的那个
        (["client delete prohibited", "pending delete"], DomainState.PENDING_DELETE),
    ],
)
def test_status_classification(statuses, expected):
    status = parse_rdap("target.com", rdap_payload(statuses=statuses))
    assert status.state == expected


def test_expired_by_date_without_status():
    status = parse_rdap(
        "target.com", rdap_payload(statuses=[], expiration="2000-01-01T00:00:00Z")
    )
    assert status.state == DomainState.EXPIRED


def test_registrar_falls_back_to_public_id():
    payload = rdap_payload()
    payload["entities"] = [{"roles": ["registrar"], "publicIds": [{"identifier": "292"}]}]
    assert parse_rdap("target.com", payload).registrar == "IANA#292"


def test_parse_rejects_non_object():
    with pytest.raises(ValueError):
        parse_rdap("target.com", ["not", "an", "object"])


async def test_lookup_404_means_available(rdap_server, rdap_client):
    rdap_server.set("free.com", None)
    client = RdapClient(RdapConfig(rps_per_host=1000), client=rdap_client)
    status = await client.lookup("free.com")
    assert status.state == DomainState.AVAILABLE
    assert await client.is_available("free.com") is True


async def test_lookup_errors_are_not_available():
    """关键安全性质：查询失败绝不能被当成「可注册」。"""
    def handler(request):
        if "dns.json" in str(request.url):
            return httpx.Response(200, json={"services": [[["com"], ["https://r/"]]]})
        return httpx.Response(500)

    client = RdapClient(
        RdapConfig(rps_per_host=1000, max_retries=0),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    status = await client.lookup("boom.com")
    assert status.state == DomainState.ERROR
    assert status.state != DomainState.AVAILABLE
    assert await client.is_available("boom.com") is None


async def test_rate_limit_triggers_backoff():
    calls = {"n": 0}

    def handler(request):
        if "dns.json" in str(request.url):
            return httpx.Response(200, json={"services": [[["com"], ["https://r/"]]]})
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = RdapClient(
        RdapConfig(rps_per_host=1000, max_retries=2),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    status = await client.lookup("busy.com")
    assert status.state == DomainState.ERROR
    assert "429" in (status.error or "")
    assert calls["n"] == 3  # 初次 + 2 次重试


async def test_bootstrap_longest_suffix_match():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "services": [
                    [["uk"], ["https://rdap.uk/"]],
                    [["co.uk"], ["https://rdap.couk/"]],
                ]
            },
        )

    client = RdapClient(
        RdapConfig(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await client.server_for("a.example.co.uk") == "https://rdap.couk/"


async def test_overrides_win_over_bootstrap(rdap_client):
    client = RdapClient(
        RdapConfig(overrides={"com": "https://my-rdap.example/"}), client=rdap_client
    )
    assert await client.server_for("x.com") == "https://my-rdap.example/"


async def test_fallback_service_when_bootstrap_unavailable():
    client = RdapClient(
        RdapConfig(fallback_service="https://rdap.org/"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
    )
    assert await client.server_for("weird.unknowntld") == "https://rdap.org/"


async def test_bootstrap_cached_to_disk(tmp_path, rdap_server):
    fetches = {"n": 0}

    def handler(request):
        if "dns.json" in str(request.url):
            fetches["n"] += 1
        return rdap_server.handler(request)

    cache = tmp_path / "boot.json"
    transport = httpx.MockTransport(handler)
    first = RdapClient(
        RdapConfig(rps_per_host=1000), cache_path=cache,
        client=httpx.AsyncClient(transport=transport),
    )
    await first.server_for("a.com")
    assert cache.exists()

    # 新实例应该直接吃磁盘缓存，不再打网络
    second = RdapClient(
        RdapConfig(rps_per_host=1000), cache_path=cache,
        client=httpx.AsyncClient(transport=transport),
    )
    assert await second.server_for("a.com") == "https://rdap.verisign.com/com/v1/"
    assert fetches["n"] == 1


async def test_fallback_direct_404_is_not_available():
    """兜底入口自己不认识这个后缀时的 404，只说明「它查不到」。

    不知道权威服务器在哪却断言「可注册」，是最危险的一种误报。
    """
    def handler(request):
        if "dns.json" in str(request.url):
            return httpx.Response(200, json={"services": [[["com"], ["https://v/"]]]})
        return httpx.Response(404)

    client = RdapClient(
        RdapConfig(rps_per_host=1000, max_retries=0, fallback_service="https://rdap.org/"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    status = await client.lookup("x.unknowntld")

    assert status.state is DomainState.ERROR
    assert status.state is not DomainState.AVAILABLE
    assert "无法确认" in (status.error or "")


async def test_fallback_404_after_redirect_is_trusted():
    """兜底入口跳转到真实 RDAP 服务器后返回的 404 是可信的。"""
    def handler(request):
        url = str(request.url)
        if "dns.json" in url:
            return httpx.Response(200, json={"services": [[["com"], ["https://v/"]]]})
        if "rdap.org" in url:
            return httpx.Response(302, headers={"Location": "https://real.example/domain/x.co"})
        return httpx.Response(404)

    client = RdapClient(
        RdapConfig(rps_per_host=1000, max_retries=0, fallback_service="https://rdap.org/"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
    )
    assert (await client.lookup("x.unknowntld")).state is DomainState.AVAILABLE


async def test_bootstrap_matched_404_still_means_available(rdap_server, rdap_client):
    """正常路径不受影响：权威服务器说 404 就是可注册。"""
    rdap_server.set("free.com", None)
    client = RdapClient(RdapConfig(rps_per_host=1000), client=rdap_client)
    assert (await client.lookup("free.com")).state is DomainState.AVAILABLE


async def test_resolve_server_reports_fallback_usage(rdap_client):
    client = RdapClient(RdapConfig(fallback_service="https://rdap.org/"), client=rdap_client)
    server, via_fallback = await client.resolve_server("a.com")
    assert via_fallback is False and "verisign" in server
    server, via_fallback = await client.resolve_server("a.unknowntld")
    assert via_fallback is True and server == "https://rdap.org/"
