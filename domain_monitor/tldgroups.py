"""预设后缀合集。

写 ``vps.{@all}`` 比一个个列后缀省事得多。

设计上只分三档，不再搞 two / two-more 这种「超集套子集」的分层——
那种分法你还得先记住哪个是哪个：

* ``@all``  —— 想全盯就用它，无限制后缀一网打尽
* ``@two``  —— 只要两位的
* ``@gtld`` —— 只要非两位的通用后缀

有注册限制的（欧洲国别要当地实体、.cn 要实名）**不放进 @all**，
单独成组并标注原因——把它们混进来只会让你加一堆永远注册不了的域名。

⚠️ 合集只是**书写便利**，不保证能注册：各注册商卖的后缀范围不同，
价格差异也极大。下单前用 ``domain_monitor price`` 确认。
"""

from __future__ import annotations

# 两位后缀里通常可自由注册的。国别后缀但没有当地实体要求。
TWO_LETTER = [
    # 最常用的
    "io", "co", "ai", "me", "cc", "tv", "sh", "gg",
    # 常见于短链 / 品牌
    "ly", "to", "im", "is", "la", "vc", "ws", "nu",
    # 小众岛国域名，便宜但注册商支持参差
    "ag", "bz", "cx", "gd", "gl", "gs", "ki", "mn",
    "ms", "mu", "pw", "sc", "so", "st", "sx", "tc", "vg",
]

# 非两位的通用后缀（gTLD）。com/net/org 打头，后面是常见新 gTLD。
GTLD = [
    "com", "net", "org", "info", "biz",
    "xyz", "app", "dev", "tech", "online", "site", "store",
    "shop", "cloud", "top", "icu", "vip", "pro", "club",
    "live", "fun", "space", "one", "link", "wiki",
]

# ⚠️ 以下两组有注册限制，不并入 @all
EUROPE = [
    "de", "fr", "it", "es", "nl", "se",
    "eu", "ch", "at", "dk", "be", "pl", "cz",
]
CHINA = ["cn", "com.cn", "net.cn"]


def _dedupe(*lists: list[str]) -> list[str]:
    """合并去重且保持顺序。"""
    seen: set[str] = set()
    merged: list[str] = []
    for items in lists:
        for item in items:
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


BUILTIN_TLD_GROUPS: dict[str, list[str]] = {
    # 一个合集打天下
    "all": _dedupe(GTLD, TWO_LETTER),
    "two": TWO_LETTER,
    "gtld": GTLD,
    # 有限制的单列
    "europe": EUROPE,
    "china": CHINA,
    # 中文别名
    "全部": _dedupe(GTLD, TWO_LETTER),
    "两位": TWO_LETTER,
}

# 兼容早期写法。这些组保留**原本的内容**，不指向合并后的大组——
# 否则老配置里的 {@classic} 会从 3 个后缀悄悄涨到 25 个，
# 在 auto_buy_default=true 时等于凭空多出二十几个待抢域名。
_LEGACY_GROUPS: dict[str, list[str]] = {
    "classic": ["com", "net", "org"],
    "popular": ["com", "net", "org", "io", "co", "ai", "xyz", "app", "dev"],
    "startup": ["io", "ai", "dev", "app", "tech", "xyz", "co", "sh"],
    "常用": ["com", "net", "org", "io", "co", "ai", "xyz", "app", "dev"],
    # 这三个是用户明确要求合并的，指向合并后的两位后缀全集
    "two-more": TWO_LETTER,
    "2": TWO_LETTER,
    "短": TWO_LETTER,
}
BUILTIN_TLD_GROUPS.update({k: list(v) for k, v in _LEGACY_GROUPS.items()})

# 需要提醒用户注意限制的组
RESTRICTED_NOTES: dict[str, str] = {
    "europe": "多数要求当地实体或居民身份，注册商未必受理",
    "china": ".cn 需要实名认证",
}

# 展示顺序：主力组在前，别名不单独列出来占地方
PRIMARY_GROUPS = ["all", "two", "gtld", "europe", "china"]


def group_names(*, include_aliases: bool = False) -> list[str]:
    """可用的组名。默认只给主力组，别名不占版面。"""
    if not include_aliases:
        return list(PRIMARY_GROUPS)
    aliases = sorted(set(BUILTIN_TLD_GROUPS) - set(PRIMARY_GROUPS))
    return PRIMARY_GROUPS + aliases


def merge_groups(custom: dict[str, list[str]] | None) -> dict[str, list[str]]:
    """把用户自定义的组并进内置组；同名时用户的覆盖内置。"""
    merged = {name: list(values) for name, values in BUILTIN_TLD_GROUPS.items()}
    for name, values in (custom or {}).items():
        key = str(name).strip().lower()
        if not key:
            continue
        merged[key] = [
            str(item).strip().lower().lstrip(".") for item in values if str(item).strip()
        ]
    return merged
