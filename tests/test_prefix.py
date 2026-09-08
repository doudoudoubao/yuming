"""前缀监控：花括号模式展开、prefixes 配置段、同组抢到即停。"""

import pytest

from domain_monitor.config import ConfigError, load_config
from domain_monitor.storage import Storage
from domain_monitor.utils import PatternError, expand_pattern, expand_patterns


# ------------------------------------------------------------------ 模式展开

@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("mydream.{com,net,io}", ["mydream.com", "mydream.net", "mydream.io"]),
        ("{short,tiny}.com", ["short.com", "tiny.com"]),
        ("{a,b}.{com,cn}", ["a.com", "a.cn", "b.com", "b.cn"]),
        ("example.com", ["example.com"]),                 # 没花括号原样返回
        ("my{a,b}dream.com", ["myadream.com", "mybdream.com"]),
        ("a.{com, net , io}", ["a.com", "a.net", "a.io"]),  # 容忍空格
        ("", []),
    ],
)
def test_expand_pattern(pattern, expected):
    assert expand_pattern(pattern) == expected


def test_expansion_dedupes_but_keeps_order():
    assert expand_pattern("a.{com,com,net}") == ["a.com", "a.net"]
    assert expand_patterns(["a.{com,net}", "a.com", "b.io"]) == [
        "a.com", "a.net", "b.io"
    ]


@pytest.mark.parametrize("bad", ["a.{com", "a.com}", "a.{}", "a.{,}"])
def test_malformed_patterns_rejected(bad):
    with pytest.raises(PatternError):
        expand_pattern(bad)


def test_expansion_limit_guards_against_blowup():
    """一个手滑的模式不能生成几千个域名把 RDAP 打爆。"""
    with pytest.raises(PatternError, match="超过"):
        expand_pattern("{a,b,c,d,e}.{a,b,c,d,e}.{a,b,c,d,e}", limit=50)


# --------------------------------------------------------------- 配置：domains

def test_domains_accept_patterns():
    config = load_config(data={"domains": ["mydream.{com,net,io}"]})
    assert [item.name for item in config.domains] == [
        "mydream.com", "mydream.net", "mydream.io"
    ]
    # 同一条模式展开出来的归为一组
    assert len({item.group for item in config.domains}) == 1


def test_plain_domain_has_no_group():
    config = load_config(data={"domains": ["single.com"]})
    assert config.domains[0].group is None


def test_pattern_with_invalid_result_rejected():
    with pytest.raises(ConfigError, match="不是合法域名"):
        load_config(data={"domains": ["a.{com,不合法!}"]})


def test_malformed_pattern_in_config_rejected():
    with pytest.raises(ConfigError, match="花括号"):
        load_config(data={"domains": ["a.{com"]})


# -------------------------------------------------------------- 配置：prefixes

def test_prefixes_expand_to_domains():
    config = load_config(
        data={"prefixes": [{"name": "short", "tlds": ["com", "cn", "io"],
                            "max_price": 80}]}
    )
    assert [item.name for item in config.domains] == [
        "short.com", "short.cn", "short.io"
    ]
    assert all(item.group == "prefix:short" for item in config.domains)
    assert all(item.max_price == 80 for item in config.domains)
    # 默认「抢到一个就够了」
    assert all(item.stop_after_first for item in config.domains)


def test_prefixes_accept_multiple_names():
    config = load_config(
        data={"prefixes": [{"name": ["a", "b"], "tlds": ["com"]}]}
    )
    assert [item.name for item in config.domains] == ["a.com", "b.com"]
    # 不同前缀是不同的组——a.com 抢到了不该影响 b.com
    assert config.domains[0].group != config.domains[1].group


def test_prefixes_strip_leading_dot_on_tlds():
    config = load_config(data={"prefixes": [{"name": "x", "tlds": [".com", "io"]}]})
    assert [item.name for item in config.domains] == ["x.com", "x.io"]


def test_prefixes_require_name_and_tlds():
    with pytest.raises(ConfigError, match="缺少 name"):
        load_config(data={"prefixes": [{"tlds": ["com"]}]})
    with pytest.raises(ConfigError, match="缺少 tlds"):
        load_config(data={"prefixes": [{"name": "x"}]})


def test_prefixes_must_be_a_list():
    with pytest.raises(ConfigError, match="prefixes 必须是列表"):
        load_config(data={"prefixes": {"name": "x", "tlds": ["com"]}})


def test_prefixes_respect_expansion_limit():
    with pytest.raises(ConfigError, match="超过上限"):
        load_config(
            data={
                "pattern_limit": 5,
                "prefixes": [{"name": ["a", "b", "c"], "tlds": ["com", "net", "io"]}],
            }
        )


def test_domains_and_prefixes_merge_without_duplicates():
    config = load_config(
        data={
            "domains": ["short.com"],
            "prefixes": [{"name": "short", "tlds": ["com", "io"]}],
        }
    )
    names = [item.name for item in config.domains]
    assert names.count("short.com") == 1
    assert "short.io" in names


def test_idn_prefix_works():
    config = load_config(data={"prefixes": [{"name": "测试", "tlds": ["com", "中国"]}]})
    assert [item.name for item in config.domains] == [
        "xn--0zwm56d.com", "xn--0zwm56d.xn--fiqs8s"
    ]


# ------------------------------------------------------------------ 存储：分组

def test_group_siblings():
    store = Storage(":memory:")
    for tld in ("com", "net", "io"):
        store.upsert_domain(f"a.{tld}", group="prefix:a", stop_after_first=True)
    store.upsert_domain("unrelated.com")

    assert {item.domain for item in store.group_siblings("a.com")} == {"a.net", "a.io"}
    assert store.group_siblings("unrelated.com") == []
    store.close()


def test_group_siblings_excludes_disabled_and_acquired():
    from domain_monitor.models import DomainState

    store = Storage(":memory:")
    for tld in ("com", "net", "io"):
        store.upsert_domain(f"a.{tld}", group="prefix:a")
    store.set_enabled("a.net", False)
    store.update_domain("a.io", state=DomainState.ACQUIRED)

    assert store.group_siblings("a.com") == []
    store.close()
