from datetime import datetime, timedelta, timezone

import pytest

from domain_monitor.utils import (
    Backoff,
    TokenBucket,
    apply_jitter,
    escape_html,
    expand_env,
    human_delta,
    is_valid_domain,
    next_window_occurrence,
    normalize_domain,
    parse_datetime,
    suffixes_of,
    tld_of,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Example.COM", "example.com"),
        ("  https://Foo.bar.io/path?x=1 ", "foo.bar.io"),
        ("trailing.dot.com.", "trailing.dot.com"),
        ("中文.com", "xn--fiq228c.com"),
        ("", ""),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "name,valid",
    [
        ("example.com", True),
        ("a.b.c.co.uk", True),
        ("no-tld", False),
        ("-bad.com", False),
        ("bad-.com", False),
        ("x.c0m", False),
        ("", False),
    ],
)
def test_is_valid_domain(name, valid):
    assert is_valid_domain(name) is valid


def test_suffixes_longest_first():
    assert suffixes_of("a.example.co.uk") == ["example.co.uk", "co.uk", "uk"]
    assert tld_of("a.example.co.uk") == "uk"


@pytest.mark.parametrize(
    "raw",
    ["2026-08-13T04:00:00Z", "2026-08-13 04:00:00", "2026-08-13T04:00:00+00:00"],
)
def test_parse_datetime_variants(raw):
    parsed = parse_datetime(raw)
    assert parsed == datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)


def test_parse_datetime_rejects_garbage():
    assert parse_datetime("不是时间") is None
    assert parse_datetime(None) is None


def test_parse_datetime_naive_is_utc():
    assert parse_datetime("2026-08-13").tzinfo == timezone.utc


def test_expand_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret")
    result = expand_env({"a": "${MY_TOKEN}", "b": ["${MISSING:-fallback}"], "c": 5})
    assert result == {"a": "secret", "b": ["fallback"], "c": 5}


def test_expand_env_missing_becomes_empty(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    assert expand_env("${NOPE}") == ""


def test_escape_html():
    assert escape_html("<b>&x</b>") == "&lt;b&gt;&amp;x&lt;/b&gt;"


def test_human_delta():
    assert human_delta(30) == "30秒"
    assert human_delta(90) == "1分30秒"
    assert human_delta(3600) == "1小时"
    assert human_delta(93784) == "1天2小时"


def test_apply_jitter_within_bounds():
    for _ in range(200):
        value = apply_jitter(100, 0.2)
        assert 80 <= value <= 120
    assert apply_jitter(100, 0) == 100


def test_next_window_occurrence():
    reference = datetime(2026, 3, 4, 6, 0, tzinfo=timezone.utc)
    start, end = next_window_occurrence(reference, "17:30", "20:30")
    assert start == datetime(2026, 3, 4, 17, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 4, 20, 30, tzinfo=timezone.utc)


def test_next_window_crossing_midnight():
    reference = datetime(2026, 3, 4, tzinfo=timezone.utc)
    start, end = next_window_occurrence(reference, "23:00", "01:00")
    assert end - start == timedelta(hours=2)


@pytest.mark.asyncio
async def test_token_bucket_limits_rate():
    import time

    bucket = TokenBucket(rate=50, capacity=1)
    started = time.monotonic()
    for _ in range(4):
        await bucket.acquire()
    # 容量 1、速率 50/s：4 次至少要 3 个间隔 ≈ 60ms
    assert time.monotonic() - started >= 0.05


def test_backoff_grows_and_resets():
    backoff = Backoff(base=1.0, factor=2.0, maximum=10.0)
    assert backoff.penalize() == 1.0
    assert backoff.penalize() == 2.0
    assert backoff.penalize() == 4.0
    assert backoff.penalize(retry_after=0.5) == 0.5
    backoff.reset()
    assert backoff.failures == 0
    assert backoff.remaining == 0


def test_backoff_respects_maximum():
    backoff = Backoff(base=100.0, factor=10.0, maximum=5.0)
    assert backoff.penalize() == 5.0


def test_backoff_honours_zero_retry_after():
    """Retry-After: 0 表示「立即重试」，不能被当成「未提供」而走指数退避。"""
    backoff = Backoff(base=60.0)
    assert backoff.penalize(retry_after=0) == 0
    assert backoff.remaining == 0


def test_display_width_counts_cjk_as_two():
    from domain_monitor.utils import display_width, pad

    assert display_width("abc") == 3
    assert display_width("待删除") == 6
    assert display_width("已过期(宽限期)") == 14  # 6 个中文(各占 2) + 2 个半角括号
    # 中英混排的表格列必须对齐到同一显示宽度
    assert display_width(pad("待删除", 12)) == 12
    assert display_width(pad("expired", 12)) == 12
    assert pad("toolongvalue", 4) == "toolongvalue"   # 超宽不截断
