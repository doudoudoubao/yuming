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


# ------------------------------------------------------------------ 后缀合集

def test_builtin_groups_expand():
    from domain_monitor.tldgroups import BUILTIN_TLD_GROUPS, merge_groups

    groups = merge_groups(None)
    result = expand_pattern("vps.{@two}", groups=groups)
    assert len(result) == len(BUILTIN_TLD_GROUPS["two"])
    assert "vps.io" in result and "vps.ai" in result


def test_all_group_covers_everything_unrestricted():
    """@all 是「一个合集打天下」，常见后缀都得在里面。"""
    from domain_monitor.tldgroups import merge_groups

    result = expand_pattern("vps.{@all}", groups=merge_groups(None))
    for expected in ("vps.com", "vps.net", "vps.org", "vps.io",
                     "vps.ai", "vps.co", "vps.xyz", "vps.app"):
        assert expected in result


def test_two_letter_group_is_all_two_letter():
    """@two 顾名思义，里面必须都是两位后缀。"""
    from domain_monitor.tldgroups import BUILTIN_TLD_GROUPS

    for tld in BUILTIN_TLD_GROUPS["two"]:
        assert len(tld) == 2, f"{tld} 不是两位"


def test_gtld_group_has_no_two_letter():
    """@gtld 是「非两位」的那一档，混进两位后缀就说明分组乱了。"""
    from domain_monitor.tldgroups import BUILTIN_TLD_GROUPS

    for tld in BUILTIN_TLD_GROUPS["gtld"]:
        assert len(tld) > 2, f"{tld} 是两位，不该在 @gtld 里"


def test_all_is_exactly_two_plus_gtld():
    """@all 就是两档的并集——不多不少，避免又出现「超集套子集」的分层。"""
    from domain_monitor.tldgroups import BUILTIN_TLD_GROUPS as groups

    assert set(groups["all"]) == set(groups["two"]) | set(groups["gtld"])
    assert len(groups["all"]) == len(set(groups["all"]))     # 无重复


def test_restricted_tlds_stay_out_of_all():
    """有注册限制的后缀不能混进 @all，否则会加一堆永远注册不了的域名。"""
    from domain_monitor.tldgroups import BUILTIN_TLD_GROUPS as groups

    for key in ("europe", "china"):
        assert not (set(groups[key]) & set(groups["all"])), f"@{key} 混进了 @all"


def test_legacy_group_names_still_work():
    """早期写法（@two-more / @classic / @常用）不该因为合并而失效。"""
    from domain_monitor.tldgroups import BUILTIN_TLD_GROUPS as groups

    assert groups["two-more"] == groups["two"]
    assert groups["classic"] == groups["gtld"]
    assert groups["常用"] == groups["gtld"]
    assert groups["全部"] == groups["all"]


def test_groups_and_literals_can_mix():
    from domain_monitor.tldgroups import merge_groups

    result = expand_pattern("vps.{com,net,@two}", groups=merge_groups(None))
    assert result[:2] == ["vps.com", "vps.net"]
    assert "vps.io" in result and "vps.ai" in result


def test_overlapping_groups_dedupe():
    from domain_monitor.tldgroups import merge_groups

    # @two 和 @all 大量重叠，不该重复
    result = expand_pattern("x.{@two,@all}", groups=merge_groups(None))
    assert len(result) == len(set(result))


def test_unknown_group_gives_helpful_error():
    from domain_monitor.tldgroups import merge_groups

    with pytest.raises(PatternError, match="没有名为 @nosuch"):
        expand_pattern("x.{@nosuch}", groups=merge_groups(None))


def test_group_expansion_respects_limit():
    from domain_monitor.tldgroups import merge_groups

    with pytest.raises(PatternError, match="超过"):
        expand_pattern("x.{@two-more}", groups=merge_groups(None), limit=10)


def test_custom_groups_override_builtins():
    config = load_config(
        data={"tld_groups": {"two": ["io", "co"]}, "domains": ["x.{@two}"]}
    )
    assert [item.name for item in config.domains] == ["x.io", "x.co"]


def test_custom_group_alongside_builtins():
    config = load_config(
        data={"tld_groups": {"我的": ["com", "io"]}, "domains": ["x.{@我的}", "y.{@classic}"]}
    )
    names = [item.name for item in config.domains]
    assert names[:2] == ["x.com", "x.io"]
    assert "y.org" in names


def test_prefixes_tlds_accept_groups():
    config = load_config(
        data={"tld_groups": {"mini": ["com", "net"]},
              "prefixes": [{"name": "host", "tlds": ["@mini", "io"]}]}
    )
    assert [item.name for item in config.domains] == [
        "host.com", "host.net", "host.io"
    ]


def test_prefixes_unknown_group_rejected():
    with pytest.raises(ConfigError, match="不存在的后缀合集"):
        load_config(data={"prefixes": [{"name": "x", "tlds": ["@nope"]}]})


def test_prefixes_group_dedupes_with_literals():
    config = load_config(data={"prefixes": [{"name": "x", "tlds": ["@gtld", "com"]}]})
    names = [item.name for item in config.domains]
    assert names.count("x.com") == 1


def test_every_group_entry_forms_a_valid_domain():
    """任何一个合集配上普通前缀都得能组成合法域名。"""
    from domain_monitor.tldgroups import merge_groups
    from domain_monitor.utils import is_valid_domain, normalize_domain

    for name, tlds in merge_groups(None).items():
        for tld in tlds:
            candidate = normalize_domain(f"test.{tld}")
            assert is_valid_domain(candidate), f"@{name} 里的 {tld} 组不出合法域名"


def test_restricted_groups_are_flagged():
    """有注册限制的组必须带说明，否则用户会白花时间。"""
    from domain_monitor.tldgroups import BUILTIN_TLD_GROUPS, RESTRICTED_NOTES

    for key in ("europe", "china"):
        assert key in BUILTIN_TLD_GROUPS
        assert key in RESTRICTED_NOTES and RESTRICTED_NOTES[key]
