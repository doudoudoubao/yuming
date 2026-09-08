from datetime import datetime, timedelta, timezone

import pytest

from domain_monitor.utils import (
    Backoff,
    TokenBucket,
    apply_jitter,
    escape_html,
    expand_env,
    fit_lines,
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


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("example.com。", "example.com"),   # 中文句号是合法标签分隔符，IDNA 会转成点
        ("example.com.", "example.com"),
        ("中文。com", "xn--fiq228c.com"),
        ("例え.テスト", "xn--r8jz45g.xn--zckzah"),
    ],
)
def test_normalize_strips_trailing_dot_after_idna(raw, expected):
    """去尾点必须在 IDNA 之后，否则中文句号会留下 'example.com.' 这种残留。"""
    assert normalize_domain(raw) == expected
    assert is_valid_domain(normalize_domain(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "例子.中国",      # 中文后缀
        "公司.公司",
        "shop.在线",
        "сайт.рф",       # 西里尔后缀
        "例え.テスト",     # 日文后缀
    ],
)
def test_internationalized_tlds_are_valid(raw):
    """国际化后缀 punycode 后带数字（.中国 -> xn--fiqs8s），不能被后缀正则挡掉。"""
    assert is_valid_domain(normalize_domain(raw))


@pytest.mark.parametrize("raw", ["no-tld", "-bad.com", "bad-.com", "x.c0m", "a.", ""])
def test_invalid_domains_still_rejected_after_idn_support(raw):
    assert not is_valid_domain(normalize_domain(raw))


@pytest.mark.parametrize(
    "stored,shown",
    [
        ("xn--0zwm56d.com", "测试.com"),
        ("xn--eqrt2gmt2b.cn", "短域名.cn"),
        ("xn--fsqu00a.xn--fiqs8s", "例子.中国"),
        ("example.com", "example.com"),       # 非 IDN 原样返回
        ("", ""),
    ],
)
def test_display_domain_restores_unicode(stored, shown):
    """界面上该显示中文，punycode 只是内部存储形式。"""
    from domain_monitor.utils import display_domain

    assert display_domain(stored) == shown


def test_display_domain_never_raises_on_garbage():
    """展示层绝不能因为解码失败而崩掉。"""
    from domain_monitor.utils import display_domain

    for bad in ("xn--bad!!", "xn--", "xn--@@@.com"):
        assert display_domain(bad) == bad


def test_display_domain_roundtrips_with_normalize():
    from domain_monitor.utils import display_domain, normalize_domain

    for original in ("测试.com", "例子.中国", "短域名.cn"):
        assert display_domain(normalize_domain(original)) == original


class TestFitLines:
    """把列表塞进一条 Telegram 消息：宁可少列，也不能被拦腰截断。"""

    def test_everything_fits(self):
        text = fit_lines(["头"], ["a", "b"], 100, "… 还有 {n} 条")

        assert text == "头\na\nb"
        assert "还有" not in text

    def test_drops_items_before_exceeding_limit(self):
        text = fit_lines([], ["x" * 10] * 10, 60, "… 还有 {n} 条")

        assert len(text) <= 60
        assert text.endswith("… 还有 6 条")

    def test_reserves_room_for_the_tail(self):
        """预留不足的话，会加到最后一条才发现「还有 N 条」放不下。"""
        for limit in range(20, 200):
            assert len(fit_lines(["头"], ["y" * 7] * 20, limit, "… 还有 {n} 条")) <= limit

    def test_max_items_caps_the_count_too(self):
        """短条目不会超长，但列 200 条一样是刷屏。"""
        text = fit_lines([], [str(index) for index in range(200)], 4000,
                         "… 还有 {n} 条", max_items=30)

        assert text.count("\n") == 30  # 30 条 + 尾巴
        assert text.endswith("… 还有 170 条")

    def test_no_items(self):
        assert fit_lines(["头"], [], 100, "… 还有 {n} 条") == "头"
