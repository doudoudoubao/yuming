import httpx
import pytest

from domain_monitor.config import PurchaseConfig, RegistrarConfig
from domain_monitor.registrars import PROVIDERS, available_providers, build_registrar
from domain_monitor.registrars.aliyun import sign_params
from domain_monitor.registrars.base import RegistrarError


def make(provider, handler=None, **options):
    config = RegistrarConfig(provider=provider, options=options)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    return build_registrar(config, client=client)


def test_all_providers_constructible():
    for name in available_providers():
        assert build_registrar(RegistrarConfig(provider=name)).name == name


def test_unknown_provider_rejected():
    with pytest.raises(RegistrarError, match="未知的注册商"):
        build_registrar(RegistrarConfig(provider="nope"))


# ------------------------------------------------------------------ 重试语义

@pytest.mark.parametrize(
    "message,retryable",
    [
        ("domain is not available", True),       # 还没释放 —— 继续冲
        ("Domain already registered", True),
        ("insufficient funds in account", False),  # 余额不足 —— 立刻停
        ("Unauthorized: bad api key", False),
        ("余额不足", False),
        ("some unexpected error", True),
    ],
)
def test_failure_retry_classification(message, retryable):
    registrar = make("dryrun")
    assert registrar.failure("a.com", message).retryable is retryable


async def test_dryrun_fails_then_succeeds():
    async with make("dryrun", fail_times=2, price=12.5) as registrar:
        first = await registrar.register("a.com", PurchaseConfig())
        second = await registrar.register("a.com", PurchaseConfig())
        third = await registrar.register("a.com", PurchaseConfig())
    assert (first.success, second.success, third.success) == (False, False, True)
    assert third.price == 12.5
    assert first.retryable is True


# -------------------------------------------------------------------- NameSilo

NAMESILO_OK = """<?xml version="1.0"?><namesilo><reply>
<code>300</code><detail>success</detail><total_paid>8.99</total_paid></reply></namesilo>"""

NAMESILO_TAKEN = """<?xml version="1.0"?><namesilo><reply>
<code>261</code><detail>Domain is not available for registration</detail></reply></namesilo>"""

NAMESILO_BROKE = """<?xml version="1.0"?><namesilo><reply>
<code>400</code><detail>insufficient funds</detail></reply></namesilo>"""

NAMESILO_CHECK = """<?xml version="1.0"?><namesilo><reply><code>300</code>
<available><domain price="9.99" premium="0">free.com</domain></available>
<unavailable><domain>taken.com</domain></unavailable></reply></namesilo>"""


async def test_namesilo_success():
    async with make("namesilo", lambda r: httpx.Response(200, text=NAMESILO_OK),
                    api_key="k") as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is True
    assert result.price == 8.99


async def test_namesilo_taken_is_retryable():
    async with make("namesilo", lambda r: httpx.Response(200, text=NAMESILO_TAKEN),
                    api_key="k") as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is False
    assert result.retryable is True


async def test_namesilo_insufficient_funds_is_fatal():
    async with make("namesilo", lambda r: httpx.Response(200, text=NAMESILO_BROKE),
                    api_key="k") as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is False
    assert result.retryable is False


async def test_namesilo_check():
    handler = lambda r: httpx.Response(200, text=NAMESILO_CHECK)
    async with make("namesilo", handler, api_key="k") as registrar:
        free = await registrar.check("free.com")
        taken = await registrar.check("taken.com")
    assert (free.available, free.price) == (True, 9.99)
    assert taken.available is False


async def test_namesilo_requires_api_key():
    async with make("namesilo", lambda r: httpx.Response(200, text=NAMESILO_OK)) as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is False
    assert "api_key" in result.message


async def test_namesilo_sends_expected_params():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, text=NAMESILO_OK)

    async with make("namesilo", handler, api_key="k") as registrar:
        await registrar.register(
            "a.com",
            PurchaseConfig(whois_privacy=True, auto_renew=False,
                           nameservers=["ns1.x.com", "ns2.x.com"]),
            years=3,
        )
    assert seen["domain"] == "a.com"
    assert seen["years"] == "3"
    assert seen["private"] == "1"
    assert seen["auto_renew"] == "0"
    assert seen["ns1"] == "ns1.x.com"
    assert seen["ns2"] == "ns2.x.com"


# --------------------------------------------------------------------- Dynadot

async def test_dynadot_success():
    payload = {"RegisterResponse": {"ResponseCode": "0", "Status": "success"}}
    async with make("dynadot", lambda r: httpx.Response(200, json=payload),
                    api_key="k") as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is True


async def test_dynadot_failure():
    payload = {"RegisterResponse": {"ResponseCode": "-1", "Status": "error",
                                    "Error": "domain not available"}}
    async with make("dynadot", lambda r: httpx.Response(200, json=payload),
                    api_key="k") as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is False
    assert result.retryable is True


async def test_dynadot_check_parses_price():
    payload = {
        "SearchResponse": {
            "SearchResults": [
                {"DomainName": "a.com", "Available": "yes", "Price": "$10.99 in USD"}
            ]
        }
    }
    async with make("dynadot", lambda r: httpx.Response(200, json=payload),
                    api_key="k") as registrar:
        result = await registrar.check("a.com")
    assert result.available is True
    assert result.price == 10.99


# --------------------------------------------------------------------- GoDaddy

CONTACT = {
    "first_name": "San", "last_name": "Zhang", "email": "a@b.com",
    "phone": "+86.13800138000", "address1": "road 1", "city": "Shanghai",
    "state": "SH", "postal_code": "200000", "country": "CN",
}


def godaddy(handler, **options):
    config = RegistrarConfig(provider="godaddy", options=options, contact=CONTACT)
    return build_registrar(config, client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))


async def test_godaddy_price_is_micro_units():
    payload = {"available": True, "price": 11990000, "currency": "USD"}
    async with godaddy(lambda r: httpx.Response(200, json=payload),
                       api_key="k", api_secret="s") as registrar:
        result = await registrar.check("a.com")
    assert result.available is True
    assert result.price == pytest.approx(11.99)


async def test_godaddy_purchase_sends_four_contacts():
    seen = {}

    def handler(request):
        if "agreements" in str(request.url):
            return httpx.Response(200, json=[{"agreementKey": "DNRA"}])
        seen.update(request.read() and __import__("json").loads(request.read()))
        return httpx.Response(200, json={"orderId": 99, "total": 11990000})

    async with godaddy(handler, api_key="k", api_secret="s") as registrar:
        result = await registrar.register("a.com", PurchaseConfig())

    assert result.success is True
    assert result.order_id == "99"
    assert result.price == pytest.approx(11.99)
    for key in ("contactAdmin", "contactBilling", "contactRegistrant", "contactTech"):
        assert seen[key]["nameFirst"] == "San"
    assert seen["consent"]["agreementKeys"] == ["DNRA"]


async def test_godaddy_missing_contact_is_fatal():
    config = RegistrarConfig(provider="godaddy", options={"api_key": "k", "api_secret": "s"},
                             contact={})
    registrar = build_registrar(config, client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    async with registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is False
    assert result.retryable is False


async def test_godaddy_auth_error_is_fatal():
    def handler(request):
        if "agreements" in str(request.url):
            return httpx.Response(200, json=[{"agreementKey": "DNRA"}])
        return httpx.Response(401, json={"message": "bad key"})

    async with godaddy(handler, api_key="k", api_secret="s") as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is False
    assert result.retryable is False


# ------------------------------------------------------------------- Namecheap

NC_OK = """<?xml version="1.0" encoding="utf-8"?>
<ApiResponse Status="OK" xmlns="http://api.namecheap.com/xml.response">
<CommandResponse><DomainCreateResult Domain="a.com" Registered="true"
 ChargedAmount="10.98" OrderID="12345"/></CommandResponse></ApiResponse>"""

NC_ERROR = """<?xml version="1.0" encoding="utf-8"?>
<ApiResponse Status="ERROR" xmlns="http://api.namecheap.com/xml.response">
<Errors><Error Number="1011102">API Key is invalid</Error></Errors></ApiResponse>"""


def namecheap(handler, **options):
    options.setdefault("api_user", "u")
    options.setdefault("api_key", "k")
    options.setdefault("client_ip", "1.2.3.4")
    config = RegistrarConfig(provider="namecheap", options=options, contact=CONTACT)
    return build_registrar(config, client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))


async def test_namecheap_success_and_contact_expansion():
    seen = {}

    def handler(request):
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, text=NC_OK)

    async with namecheap(handler) as registrar:
        result = await registrar.register("a.com", PurchaseConfig())

    assert result.success is True
    assert result.order_id == "12345"
    assert result.price == 10.98
    # 四类联系人都要带上
    for prefix in ("Registrant", "Tech", "Admin", "AuxBilling"):
        assert seen[f"{prefix}FirstName"] == "San"
        assert seen[f"{prefix}Country"] == "CN"


async def test_namecheap_bad_key_is_fatal():
    async with namecheap(lambda r: httpx.Response(200, text=NC_ERROR)) as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is False
    assert result.retryable is False


# ---------------------------------------------------------------------- 阿里云

def test_aliyun_signature_matches_official_example():
    """用阿里云文档里的官方测试向量校验 HMAC-SHA1 签名实现。"""
    params = {
        "Action": "DescribeRegions",
        "Format": "XML",
        "Version": "2014-05-26",
        "AccessKeyId": "testid",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": "3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf",
        "Timestamp": "2016-02-23T12:46:24Z",
    }
    assert sign_params(params, "testsecret") == "OLeaidS1JvxuMvnyHOwuJ+uX5qY="


async def test_aliyun_register_returns_task_no():
    async with make("aliyun", lambda r: httpx.Response(200, json={"TaskNo": "T-1"}),
                    access_key_id="a", access_key_secret="b",
                    registrant_profile_id="7") as registrar:
        result = await registrar.register("a.cn", PurchaseConfig())
    assert result.success is True
    assert result.order_id == "T-1"


async def test_aliyun_requires_profile_id():
    async with make("aliyun", lambda r: httpx.Response(200, json={}),
                    access_key_id="a", access_key_secret="b") as registrar:
        result = await registrar.register("a.cn", PurchaseConfig())
    assert result.success is False
    assert result.retryable is False
    assert "registrant_profile_id" in result.message


async def test_aliyun_signature_error_is_fatal():
    payload = {"Code": "SignatureDoesNotMatch", "Message": "bad signature"}
    async with make("aliyun", lambda r: httpx.Response(400, json=payload),
                    access_key_id="a", access_key_secret="b",
                    registrant_profile_id="7") as registrar:
        result = await registrar.register("a.cn", PurchaseConfig())
    assert result.success is False
    assert result.retryable is False


# ------------------------------------------------------------------------ exec

async def test_exec_success_with_json_output(tmp_path):
    script = tmp_path / "buy.sh"
    script.write_text('#!/bin/sh\necho \'{"success":true,"order_id":"X1","price":7.5}\'\n')
    script.chmod(0o755)
    async with make("exec", command=[str(script)]) as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is True
    assert result.order_id == "X1"
    assert result.price == 7.5


async def test_exec_exit_code_2_is_fatal(tmp_path):
    script = tmp_path / "buy.sh"
    script.write_text("#!/bin/sh\necho 'hard failure' >&2\nexit 2\n")
    script.chmod(0o755)
    async with make("exec", command=[str(script)]) as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is False
    assert result.retryable is False


async def test_exec_receives_domain_and_env(tmp_path):
    script = tmp_path / "buy.sh"
    script.write_text('#!/bin/sh\necho "arg=$1 env=$DM_DOMAIN years=$DM_YEARS"\nexit 1\n')
    script.chmod(0o755)
    async with make("exec", command=[str(script)]) as registrar:
        result = await registrar.register("a.com", PurchaseConfig(), years=2)
    assert "arg=a.com" in result.message
    assert "env=a.com" in result.message
    assert "years=2" in result.message


async def test_exec_timeout(tmp_path):
    script = tmp_path / "slow.sh"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    async with make("exec", command=[str(script)], timeout=0.3) as registrar:
        result = await registrar.register("a.com", PurchaseConfig())
    assert result.success is False
    assert "超时" in result.message


def test_every_provider_declares_how_to_set_it_up():
    """每个适配器都要能回答「我需要提供什么」，否则用户只能翻源码。"""
    for name, provider in PROVIDERS.items():
        assert provider.display_name and provider.display_name != "未命名", name
        assert provider.payment and provider.payment != "未知", name


def test_real_providers_declare_required_credentials():
    """真实注册商必须声明必填项，否则用户配了一半才发现少东西。"""
    for name in ("namesilo", "dynadot", "godaddy", "namecheap", "aliyun"):
        provider = PROVIDERS[name]
        assert provider.required_options, f"{name} 没声明必填项"
        assert provider.signup_url.startswith("https://"), f"{name} 没给开户地址"


def test_declared_options_match_what_the_code_reads():
    """声明的必填项必须真的是代码里 require_option 读的那些键。

    两边对不上的话，用户照着填完仍然会报「缺少配置」。
    """
    import inspect
    import re

    from domain_monitor.registrars import PROVIDERS

    for name in ("namesilo", "dynadot", "godaddy", "namecheap", "aliyun"):
        provider = PROVIDERS[name]
        source = inspect.getsource(inspect.getmodule(provider))
        actually_required = set(re.findall(r'require_option\(\s*["\'](\w+)["\']', source))
        declared = set(provider.required_options)
        assert declared == actually_required, (
            f"{name} 声明的必填项 {sorted(declared)} "
            f"与代码实际读取的 {sorted(actually_required)} 不一致"
        )


def test_contact_requirement_matches_the_code():
    """声明「需要联系人资料」的，代码里必须真的读了 contact。"""
    import inspect

    for name, provider in PROVIDERS.items():
        source = inspect.getsource(inspect.getmodule(provider))
        uses_contact = "require_contact(" in source
        assert provider.needs_contact == uses_contact, (
            f"{name} 的 needs_contact={provider.needs_contact} 与代码不符"
        )


@pytest.mark.parametrize(
    "message",
    [
        "namesilo: 缺少必填配置 registrar.options.api_key",
        "godaddy: 缺少注册人信息 registrar.contact.first_name",
        "aliyun: 缺少必填配置 registrar.options.access_key_secret",
    ],
)
def test_missing_config_is_never_retried(message):
    """配置缺失重试多少次都是同样的结果，只会白白错过抢注窗口。"""
    registrar = make("dryrun")
    assert registrar.failure("a.com", message).retryable is False


async def test_empty_credential_stops_after_one_attempt():
    """凭据为空时应该一次就停，而不是把 max_attempts 全部空转掉。"""
    from domain_monitor.registrars.pool import RegistrarPool

    registrar = build_registrar(
        RegistrarConfig(provider="namesilo", options={"api_key": ""})
    )
    pool = RegistrarPool([registrar])

    winner, results = await pool.race_register(
        "a.com", PurchaseConfig(), years=1, concurrency=1
    )

    assert winner is None
    assert results and results[0].retryable is False
    assert not pool.active            # 通道已被停用，不会再打
