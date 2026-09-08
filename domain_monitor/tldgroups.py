"""预设后缀合集。

写 ``vps.{@two}`` 比一个个列 ``vps.{io,co,ai,me,...}`` 省事得多。

⚠️ 这些只是**书写便利**，不是「保证能注册」的清单：
* 各注册商支持的后缀范围不一样，有的根本不卖某些国别后缀
* 部分国别后缀有当地实体 / 居民要求（.de .fr .it .es .eu .us 等）
* .cn 需要实名认证
* 价格差异极大，两位后缀里不少一年上百美元

下单前用 ``domain_monitor price`` 确认你的注册商卖不卖、多少钱。
"""

from __future__ import annotations

# 最常用的一批，日常够用
_CLASSIC = ["com", "net", "org"]

# 两位后缀里真正被广泛使用、且通常可自由注册的
_TWO = [
    "io", "co", "ai", "me", "cc", "tv",
    "ly", "to", "sh", "gg", "im", "is",
    "la", "vc",
]

# 更全的两位后缀，包含一些小众岛国域名（便宜，但注册商支持参差）
_TWO_MORE = _TWO + [
    "ag", "bz", "cx", "gd", "gl", "gs", "ki", "mn",
    "ms", "mu", "nu", "pw", "sc", "so", "st", "sx",
    "tc", "vg", "ws",
]

# 欧洲国别后缀。⚠️ 这一组多数有当地实体或居民要求，注册前务必确认
_EUROPE = [
    "de", "fr", "it", "es", "nl", "se",
    "eu", "ch", "at", "dk", "be", "pl", "cz",
]

# 国内
_CHINA = ["cn", "com.cn", "net.cn"]

# 科技 / 创业项目常用
_STARTUP = ["io", "ai", "dev", "app", "tech", "xyz", "co", "sh"]

BUILTIN_TLD_GROUPS: dict[str, list[str]] = {
    "classic": _CLASSIC,
    "popular": _CLASSIC + ["io", "co", "ai", "xyz", "app", "dev"],
    "two": _TWO,
    "two-more": _TWO_MORE,
    "europe": _EUROPE,
    "china": _CHINA,
    "startup": _STARTUP,
    # 常用别名
    "2": _TWO,
    "短": _TWO,
    "常用": _CLASSIC + ["io", "co", "ai", "xyz", "app", "dev"],
}

# 需要提醒用户注意限制的组
RESTRICTED_NOTES: dict[str, str] = {
    "europe": "多数需要当地实体或居民身份，注册商未必受理",
    "china": ".cn 需要实名认证",
    "two-more": "含小众岛国后缀，部分注册商不支持",
}


def group_names() -> list[str]:
    """所有可用的组名（别名排在后面）。"""
    primary = ["classic", "popular", "two", "two-more", "startup", "europe", "china"]
    aliases = sorted(set(BUILTIN_TLD_GROUPS) - set(primary))
    return primary + aliases


def merge_groups(custom: dict[str, list[str]] | None) -> dict[str, list[str]]:
    """把用户自定义的组并进内置组；同名时用户的覆盖内置。"""
    merged = {name: list(values) for name, values in BUILTIN_TLD_GROUPS.items()}
    for name, values in (custom or {}).items():
        key = str(name).strip().lower()
        if not key:
            continue
        merged[key] = [str(item).strip().lower().lstrip(".") for item in values if str(item).strip()]
    return merged
