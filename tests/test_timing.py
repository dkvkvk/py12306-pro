"""时间策略纯函数测试：抖动、退避、跨零点区间。"""

from __future__ import annotations

import pytest

from core.config import in_period, parse_periods, to_minutes
from core.timing import (
    DeterministicJitter,
    QueryTimingConfig,
    SystemJitter,
    clamp_delay,
    exponential_backoff,
    jittered,
    new_stream,
    next_query_delay,
    parse_retry_after,
    pre_sale_base,
)


class TestJitter:
    def test_jittered_stays_within_band(self):
        stream = SystemJitter(seed=1)
        for _ in range(200):
            value = jittered(4.0, 0.3, stream)
            assert 2.8 <= value <= 5.2

    def test_jitter_band_is_exact_at_ratio(self):
        assert jittered(10.0, 0.0, SystemJitter(seed=2)) == 10.0

    def test_zero_base_gives_positive_delay(self):
        stream = SystemJitter(seed=3)
        for _ in range(50):
            assert jittered(0.0, 0.5, stream) >= 0.0

    def test_deterministic_stream_is_reproducible(self):
        a = DeterministicJitter("task-1|2026-10-01|北京-上海")
        b = DeterministicJitter("task-1|2026-10-01|北京-上海")
        assert [a.uniform(0, 1) for _ in range(5)] == [b.uniform(0, 1) for _ in range(5)]

    def test_different_keys_do_not_synchronize(self):
        """规格书 5.1：每个任务/日期/车站组合必须独立抖动，不能同步。"""
        keys = ["t1|d1|A-B", "t1|d1|A-C", "t2|d1|A-B", "t1|d2|A-B"]
        first = [DeterministicJitter(k).uniform(0, 10) for k in keys]
        assert len(set(round(v, 4) for v in first)) == len(keys)

    def test_new_stream_falls_back_to_system(self):
        assert isinstance(new_stream("k"), DeterministicJitter)
        assert isinstance(new_stream(""), SystemJitter)


class TestNextQueryDelay:
    def test_normal_mode_uses_base_with_jitter(self):
        config = QueryTimingConfig(normal_base_seconds=4.0, jitter_ratio=0.3)
        values = [next_query_delay(config, False, new_stream(f"k{i}")) for i in range(200)]
        assert all(2.8 <= v <= 5.2 for v in values)

    def test_pre_sale_is_more_aggressive_than_normal(self):
        config = QueryTimingConfig(normal_base_seconds=4.0, pre_sale_base_seconds=1.2, window_base_seconds=5.0, jitter_ratio=0.3)
        pre = [next_query_delay(config, True, new_stream(f"p{i}")) for i in range(200)]
        assert max(pre) <= 5.0
        assert min(pre) >= 1.2

    def test_pre_sale_never_goes_below_configured_floor(self):
        config = QueryTimingConfig(pre_sale_base_seconds=1.5, window_base_seconds=5.0)
        for i in range(100):
            assert pre_sale_base(config, new_stream(f"q{i}")) >= 1.5

    def test_override_interval_is_respected(self):
        config = QueryTimingConfig(jitter_ratio=0.3)
        values = [next_query_delay(config, False, new_stream(f"o{i}"), override=3.0) for i in range(100)]
        assert all(2.1 <= v <= 3.9 for v in values)

    def test_sold_out_busy_uses_window_interval(self):
        config = QueryTimingConfig(window_base_seconds=6.0, jitter_ratio=0.0)
        assert next_query_delay(config, False, new_stream("x"), sold_out_busy=True) == 6.0


class TestExponentialBackoff:
    def test_doubles_until_cap(self):
        values = [exponential_backoff(i, 3.0, 2.0, 120.0, jitter_ratio=0.0) for i in range(1, 8)]
        assert values == [3.0, 6.0, 12.0, 24.0, 48.0, 96.0, 120.0]

    def test_breaker_ladder_reaches_thirty_minutes(self):
        """规格书 5.2：熔断后 30s -> 5min -> 30min 指数级延长。"""
        multiplier = (1800.0 / 30.0) ** 0.2
        values = [exponential_backoff(i, 30.0, multiplier, 1800.0, jitter_ratio=0.0) for i in range(1, 7)]
        assert values[0] == pytest.approx(30.0)
        assert values[5] == pytest.approx(1800.0)
        assert values == sorted(values)

    def test_jitter_only_shortens(self):
        values = [exponential_backoff(4, 3.0, 2.0, 1000.0, jitter_ratio=0.25, stream=SystemJitter(i)) for i in range(50)]
        assert all(24.0 * 0.75 <= v <= 24.0 for v in values)

    def test_attempt_below_one_is_clamped(self):
        assert exponential_backoff(0, 3.0, 2.0, 100.0, jitter_ratio=0.0) == 3.0


class TestMisc:
    def test_clamp_delay(self):
        assert clamp_delay(1.0, 5.0) == 5.0
        assert clamp_delay(50.0, 5.0, 10.0) == 10.0

    def test_parse_retry_after(self):
        assert parse_retry_after("12") == 12.0
        assert parse_retry_after(" 7.5 ") == 7.5
        assert parse_retry_after("-1") is None
        assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
        assert parse_retry_after(None) is None

    def test_to_minutes_validation(self):
        assert to_minutes("00:00") == 0
        assert to_minutes("23:59") == 1439
        with pytest.raises(ValueError):
            to_minutes("24:00")
        with pytest.raises(ValueError):
            to_minutes("8:00")


class TestPeriod:
    def test_normal_window(self):
        assert in_period("09:30", "08:00", "10:00")
        assert not in_period("11:00", "08:00", "10:00")
        assert in_period("08:00", "08:00", "10:00")

    def test_cross_midnight(self):
        """规格书 5.4 的核心 bug：22:00-06:00 这种跨零点区间。"""
        assert in_period("23:30", "22:00", "06:00")
        assert in_period("00:30", "22:00", "06:00")
        assert in_period("06:00", "22:00", "06:00")
        assert not in_period("12:00", "22:00", "06:00")
        assert not in_period("21:59", "22:00", "06:00")

    def test_full_day(self):
        assert in_period("13:00", "00:00", "23:59")

    def test_parse_periods_multi(self):
        assert parse_periods("22:00-06:00,08:00-10:00", []) == ((1320, 360), (480, 600))

    def test_parse_periods_single_point(self):
        assert parse_periods("08:00", []) == ((480, 480),)

    def test_parse_periods_empty_means_no_limit(self):
        assert parse_periods("", []) == ()
        assert parse_periods("*", []) == ()

    def test_parse_periods_reports_bad_input(self):
        problems = []
        parse_periods("25:00-06:00", problems)
        assert problems and "25:00-06:00" in problems[0]
