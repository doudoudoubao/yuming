"""通用工具：时间处理、限速、环境变量展开等。"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def utcnow() -> datetime:
    """当前 UTC 时间（带时区信息）。"""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """把 naive datetime 视为 UTC，aware datetime 统一转成 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    """尽力解析 RDAP / WHOIS 里各种花样的时间字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    # ISO-8601，Python 3.11 之前不认识结尾的 Z
    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        return to_utc(datetime.fromisoformat(candidate))
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%Y.%m.%d",
        "%Y/%m/%d",
    ):
        try:
            return to_utc(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return None


def iso(dt: datetime | None) -> str | None:
    """序列化成 ISO 字符串，None 原样返回。"""
    return None if dt is None else to_utc(dt).isoformat()


def human_delta(seconds: float) -> str:
    """把秒数格式化成 `3天4小时` 这种人类可读形式。"""
    seconds = int(seconds)
    if seconds < 0:
        return "-" + human_delta(-seconds)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{sec}秒" if sec else f"{minutes}分"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"
    days, hours = divmod(hours, 24)
    return f"{days}天{hours}小时" if hours else f"{days}天"


def human_until(dt: datetime | None, *, now: datetime | None = None) -> str:
    """距离某个时间点还有多久。"""
    if dt is None:
        return "未知"
    delta = (to_utc(dt) - (now or utcnow())).total_seconds()
    return f"还有 {human_delta(delta)}" if delta >= 0 else f"已过 {human_delta(-delta)}"


def apply_jitter(value: float, fraction: float) -> float:
    """给间隔加上 ±fraction 的抖动，避免所有域名同一秒打请求。"""
    if fraction <= 0:
        return value
    return max(0.0, value * (1.0 + random.uniform(-fraction, fraction)))


def normalize_domain(name: str) -> str:
    """规范化域名：去空格、去协议头、去末尾点、转小写、IDN 转 punycode。"""
    text = (name or "").strip().lower()
    text = re.sub(r"^[a-z]+://", "", text)
    text = text.split("/", 1)[0]
    text = text.split("?", 1)[0]
    if not text:
        return ""
    try:
        text = text.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        pass
    # 去尾点必须放在 IDNA 之后：中文句号「。」等都是合法的标签分隔符，
    # IDNA 会把它们转成 ASCII 的点，先 strip 就会留下 "example.com." 这种残留。
    return text.rstrip(".")


# 后缀既可能是纯字母（com/io），也可能是 punycode 化的国际化后缀
# （.中国 -> xn--fiqs8s，含数字和连字符），后者不能漏掉。
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$"
)


def is_valid_domain(name: str) -> bool:
    """粗略校验域名是否合法（已 punycode 化）。"""
    return bool(_DOMAIN_RE.match(name or ""))


def display_domain(name: str) -> str:
    """把 punycode 还原成人看得懂的形式，仅用于展示。

    内部一律存 ``xn--0zwm56d.com``，但界面上该显示「测试.com」。
    还原失败就原样返回——展示层绝不能因为解码失败而报错。
    """
    text = (name or "").strip()
    if "xn--" not in text.lower():
        return text
    try:
        return text.encode("ascii").decode("idna")
    except (UnicodeError, UnicodeDecodeError, ValueError):
        return text


def domain_labels(name: str) -> list[str]:
    return [label for label in normalize_domain(name).split(".") if label]


def tld_of(name: str) -> str:
    """取最后一级后缀，例如 example.co.uk -> uk。"""
    labels = domain_labels(name)
    return labels[-1] if labels else ""


def suffixes_of(name: str) -> list[str]:
    """由长到短返回所有后缀，供 RDAP bootstrap 做最长匹配。"""
    labels = domain_labels(name)
    return [".".join(labels[index:]) for index in range(1, len(labels))] or [name]


def load_dotenv(path: str | os.PathLike[str]) -> int:
    """把 .env 里的变量读进 os.environ，返回实际设置的条数。

    已经存在的环境变量优先——真正的环境变量应该压过文件里的值。
    只认最朴素的 ``KEY=VALUE``，够用且不引第三方依赖。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return 0

    count = 0
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        return 0

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        # 去掉成对的引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value
        count += 1
    return count


class PatternError(ValueError):
    """域名模式写得不对。"""


_BRACE_RE = re.compile(r"\{([^{}]*)\}")
DEFAULT_PATTERN_LIMIT = 200


def expand_pattern(
    text: str,
    *,
    limit: int = DEFAULT_PATTERN_LIMIT,
    groups: dict[str, list[str]] | None = None,
) -> list[str]:
    """展开花括号写法，一个前缀盯多个后缀。

    ``mydream.{com,net,io}`` → ``mydream.com`` / ``mydream.net`` / ``mydream.io``
    ``{a,b}.{com,cn}``       → 四个组合
    ``vps.{@two}``           → 展开成预设的两位后缀合集
    ``vps.{@two,com,net}``   → 合集和具体后缀可以混写

    没有花括号就原样返回单元素列表，所以可以无脑套在任何接受域名的地方。
    展开结果做去重且保持书写顺序；超过 ``limit`` 直接报错，
    免得一个手滑的模式生成几千个域名把 RDAP 打爆。
    """
    text = (text or "").strip()
    if not text:
        return []
    if "{" not in text and "}" not in text:
        return [text]
    if text.count("{") != text.count("}"):
        raise PatternError(f"花括号没配对: {text}")

    groups = groups or {}

    results = [text]
    while True:
        match = _BRACE_RE.search(results[0])
        if match is None:
            break
        options: list[str] = []
        for raw in match.group(1).split(","):
            item = raw.strip()
            if not item:
                continue
            if item.startswith("@"):
                key = item[1:].strip().lower()
                if key not in groups:
                    available = "、".join(f"@{name}" for name in sorted(groups)) or "（无）"
                    raise PatternError(
                        f"没有名为 @{key} 的后缀合集。可用的有：{available}"
                    )
                options.extend(groups[key])
            else:
                options.append(item)
        # 合集之间可能有重叠（@two 和 @startup 都含 io），去重保序
        seen_option: set[str] = set()
        options = [
            item for item in options
            if not (item in seen_option or seen_option.add(item))
        ]
        if not options:
            raise PatternError(f"花括号里是空的: {text}")

        expanded: list[str] = []
        for candidate in results:
            spot = _BRACE_RE.search(candidate)
            if spot is None:
                expanded.append(candidate)
                continue
            head, tail = candidate[: spot.start()], candidate[spot.end():]
            for option in options:
                expanded.append(f"{head}{option}{tail}")
            if len(expanded) > limit:
                raise PatternError(
                    f"模式 {text} 展开后超过 {limit} 个域名，请拆小一点"
                )
        results = expanded

    # 去重但保持书写顺序
    seen: set[str] = set()
    ordered: list[str] = []
    for item in results:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def expand_patterns(
    items: Iterable[str],
    *,
    limit: int = DEFAULT_PATTERN_LIMIT,
    groups: dict[str, list[str]] | None = None,
) -> list[str]:
    """批量展开，结果整体去重。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        for name in expand_pattern(item, limit=limit, groups=groups):
            if name not in seen:
                seen.add(name)
                ordered.append(name)
    return ordered


def expand_env(value: Any, *, strict: bool = False) -> Any:
    """递归展开配置里的 ``${VAR}`` / ``${VAR:-默认值}``。"""

    def _replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        env = os.environ.get(name)
        if env is not None:
            return env
        if default is not None:
            return default
        if strict:
            raise KeyError(f"环境变量 {name} 未设置")
        return ""

    if isinstance(value, str):
        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {key: expand_env(item, strict=strict) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item, strict=strict) for item in value]
    return value


def escape_html(text: Any) -> str:
    """Telegram HTML parse_mode 需要转义的三个字符。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def display_width(text: str) -> int:
    """终端里的显示宽度：中日韩字符占两格。"""
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in str(text)
    )


def pad(text: Any, width: int) -> str:
    """按显示宽度左对齐补空格，中文表格才不会错位。"""
    text = str(text)
    return text + " " * max(0, width - display_width(text))


class TokenBucket:
    """异步令牌桶限速器。

    RDAP 服务器普遍有速率限制，打太猛会被 429 甚至临时封禁，
    所以每个 host 一个桶，按配置的 rps 匀速放行。
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = max(rate, 0.001)
        self.capacity = capacity if capacity is not None else max(1.0, self.rate)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.rate
            await asyncio.sleep(wait)


class Backoff:
    """指数退避，命中 429 / 5xx 时给单个 host 降速。"""

    def __init__(self, base: float = 2.0, factor: float = 2.0, maximum: float = 900.0) -> None:
        self.base = base
        self.factor = factor
        self.maximum = maximum
        self.failures = 0
        self.blocked_until = 0.0

    def penalize(self, retry_after: float | None = None) -> float:
        self.failures += 1
        # 注意用 is not None：Retry-After: 0 是合法的「立即重试」，不能当成未提供
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(self.maximum, self.base * (self.factor ** (self.failures - 1)))
        delay = min(delay, self.maximum)
        self.blocked_until = time.monotonic() + delay
        return delay

    def reset(self) -> None:
        self.failures = 0
        self.blocked_until = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.blocked_until - time.monotonic())

    async def wait(self) -> None:
        remaining = self.remaining
        if remaining > 0:
            await asyncio.sleep(remaining)


def next_window_occurrence(
    reference: datetime, start_hm: str, end_hm: str
) -> tuple[datetime, datetime]:
    """把 ``HH:MM`` 的删除窗口映射到 reference 当天的绝对时间区间（UTC）。"""
    start_h, start_m = (int(part) for part in start_hm.split(":", 1))
    end_h, end_m = (int(part) for part in end_hm.split(":", 1))
    day = to_utc(reference).replace(hour=0, minute=0, second=0, microsecond=0)
    start = day + timedelta(hours=start_h, minutes=start_m)
    end = day + timedelta(hours=end_h, minutes=end_m)
    if end <= start:  # 跨零点的窗口
        end += timedelta(days=1)
    return start, end
