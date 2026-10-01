"""风控熔断测试：分类识别、退避、熔断、探针复归、告警。

全部使用假时钟，不 sleep、不联网。
"""

from __future__ import annotations

import pytest

from core.risk import (
    BreakerRegistry,
    BreakerState,
    FailureCategory,
    RiskBreaker,
    RiskConfig,
    classify_exception,
    classify_response,
)
from core.timing import DeterministicJitter, new_stream


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, event, message="", extra=None) -> None:
        self.calls.append({"event": event, "message": message, "extra": extra or {}})

    @property
    def events(self):
        return [c["event"] for c in self.calls]


# --- 分类 -----------------------------------------------------------------


class TestClassifyResponse:
    def test_normal_200_is_ok(self):
        result = classify_response(200, {}, '{"data":{"result":[]}}')
        assert result.ok and result.category is None

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_server_errors(self, code):
        assert classify_response(code, {}, "").category == FailureCategory.SERVER

    def test_rate_limit_403_without_signature(self):
        assert classify_response(403, {}, "forbidden").category == FailureCategory.RATE_LIMIT

    def test_rate_limit_429(self):
        assert classify_response(429, {}, "").category == FailureCategory.RATE_LIMIT

    def test_risk_signature_in_200_body(self):
        """12306 常见：HTTP 200 + result_message 里才是真正的风控提示。"""
        result = classify_response(200, {}, '{"result_message":"您的访问过于频繁，请稍后再试"}')
        assert result.category == FailureCategory.RISK_CONTROL
        assert "访问过于频繁" in result.reason

    def test_risk_signature_beats_rate_limit_on_403(self):
        result = classify_response(403, {}, "抱歉，当前排队人数超过限制")
        assert result.category == FailureCategory.RISK_CONTROL

    def test_risk_control_header(self):
        result = classify_response(200, {"X-Risk-Control": "1"}, "ok")
        assert result.category == FailureCategory.RISK_CONTROL

    def test_auth_codes(self):
        assert classify_response(401, {}, "").category == FailureCategory.AUTH
        assert classify_response(302, {}, "").category == FailureCategory.AUTH

    def test_retry_after_is_parsed(self):
        result = classify_response(429, {"Retry-After": "45"}, "")
        assert result.retry_after == 45.0

    def test_retry_after_header_case_insensitive(self):
        assert classify_response(429, {"retry-after": "9"}, "").retry_after == 9.0

    def test_body_scan_is_bounded(self):
        """超长响应体只扫前 N 字节，避免拖慢主循环。"""
        body = "x" * 100000 + "您的访问过于频繁"
        assert classify_response(200, {}, body, max_body_scan=1024).ok

    def test_other_4xx_is_server_category(self):
        assert classify_response(400, {}, "").category == FailureCategory.SERVER


class TestClassifyException:
    def test_timeout(self, fake_errors):
        assert classify_exception(fake_errors["timeout"]).category == FailureCategory.TRANSPORT

    def test_connection_error(self, fake_errors):
        assert classify_exception(fake_errors["connection"]).category == FailureCategory.TRANSPORT

    def test_ssl_error(self, fake_errors):
        assert classify_exception(fake_errors["ssl"]).category == FailureCategory.TRANSPORT

    def test_unknown_error_still_transport(self, fake_errors):
        detail = classify_exception(fake_errors["weird"])
        assert detail.category == FailureCategory.TRANSPORT
        assert "ValueError" in detail.reason

    def test_real_requests_exceptions(self):
        requests = pytest.importorskip("requests")
        assert classify_exception(requests.Timeout()).category == FailureCategory.TRANSPORT
        assert classify_exception(requests.ConnectionError()).category == FailureCategory.TRANSPORT
        from requests.exceptions import SSLError

        assert classify_exception(SSLError()).category == FailureCategory.TRANSPORT


# --- 配置 -----------------------------------------------------------------


class TestRiskConfig:
    def test_defaults_match_spec(self):
        config = RiskConfig()
        assert config.failure_threshold == 3
        assert config.breaker_base == 30.0
        assert config.breaker_cap == 1800.0

    @pytest.mark.parametrize("kwargs", [
        {"failure_threshold": 0},
        {"require_successes": 0},
        {"half_open_probes": 0},
        {"jitter_ratio": 1.0},
        {"jitter_ratio": -0.1},
        {"breaker_cap": 10.0, "breaker_base": 30.0},
        {"failure_backoff_base": 0},
    ])
    def test_invalid_config_rejected(self, kwargs):
        with pytest.raises(ValueError):
            RiskConfig(**kwargs)

    def test_freeze_aggressive_defaults_on(self):
        assert RiskConfig().freeze_aggressive_when_open is True


# --- 退避 -----------------------------------------------------------------


class TestBackoff:
    def _breaker(self, clock, **kwargs):
        params = {"failure_threshold": 3, "jitter_ratio": 0.0}
        params.update(kwargs)
        return RiskBreaker(RiskConfig(**params), key="t", clock=clock, stream=DeterministicJitter("t"))

    def test_under_threshold_stays_closed(self, clock):
        breaker = self._breaker(clock)
        for _ in range(2):
            decision = breaker.report_failure(FailureCategory.TRANSPORT, "timeout", now=clock.now)
            assert decision.state == BreakerState.CLOSED
            assert breaker.before_query(now=clock.now).allow is True
        assert breaker.snapshot(clock.now).consecutive_failures == 2

    def test_threshold_reached_blocks_queries(self, clock):
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.report_failure(FailureCategory.TRANSPORT, "timeout", now=clock.now)
        decision = breaker.before_query(now=clock.now)
        assert decision.state == BreakerState.BACKOFF
        assert decision.allow is False
        assert decision.wait_seconds == pytest.approx(3.0)

    def test_backoff_grows_exponentially(self, clock):
        breaker = self._breaker(clock)
        waits = []
        for _ in range(5):
            breaker.report_failure(FailureCategory.SERVER, "HTTP 500", now=clock.now)
            waits.append(breaker.before_query(now=clock.now).wait_seconds)
        # 前两次未到阈值（不阻塞），第 3 次起按 factor 指数增长
        assert waits == [0.0, 0.0, 3.0, 6.0, 12.0]
        assert waits == sorted(waits)
        assert breaker.snapshot(clock.now).consecutive_failures == 5

    def test_backoff_expires_and_recovers(self, clock):
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.report_failure(FailureCategory.TRANSPORT, "timeout", now=clock.now)
        clock.advance(3.0)
        decision = breaker.before_query(now=clock.now)
        assert decision.allow is True
        assert breaker.state == BreakerState.CLOSED

    def test_success_resets_failure_counter(self, clock):
        breaker = self._breaker(clock)
        breaker.report_failure(FailureCategory.TRANSPORT, "t", now=clock.now)
        breaker.report_failure(FailureCategory.TRANSPORT, "t", now=clock.now)
        breaker.report_success(now=clock.now)
        assert breaker.snapshot(clock.now).consecutive_failures == 0
        breaker.report_failure(FailureCategory.TRANSPORT, "t", now=clock.now)
        assert breaker.state == BreakerState.CLOSED

    def test_backoff_is_capped(self, clock):
        breaker = self._breaker(clock, failure_backoff_cap=10.0)
        for _ in range(20):
            breaker.report_failure(FailureCategory.TRANSPORT, "t", now=clock.now)
            clock.advance(0.01)
        assert breaker.before_query(now=clock.now).wait_seconds <= 10.0

    def test_notifies_on_backoff(self, clock):
        notifier = RecordingNotifier()
        breaker = RiskBreaker(RiskConfig(failure_threshold=2, jitter_ratio=0.0), key="k", clock=clock, notifier=notifier)
        for _ in range(2):
            breaker.report_failure(FailureCategory.TRANSPORT, "timeout", now=clock.now)
        assert notifier.events == ["backoff"]


# --- 熔断 -----------------------------------------------------------------


class TestBreaker:
    def _breaker(self, clock, notifier=None, **kwargs):
        params = {"jitter_ratio": 0.0}
        params.update(kwargs)
        return RiskBreaker(
            RiskConfig(**params),
            key="task-1",
            label="北京-上海",
            notifier=notifier,
            clock=clock,
            stream=DeterministicJitter("task-1"),
        )

    def test_risk_control_trips_immediately(self, clock):
        """规格书 5.2：命中风控特征 -> 熔断，停止该任务查询并告警。"""
        notifier = RecordingNotifier()
        breaker = self._breaker(clock, notifier=notifier)
        decision = breaker.report_failure(FailureCategory.RISK_CONTROL, "您的访问过于频繁", now=clock.now)
        assert decision.state == BreakerState.OPEN
        assert decision.wait_seconds == pytest.approx(30.0)
        assert notifier.events == ["RISK_CONTROL"]
        assert notifier.calls[0]["extra"]["key"] == "task-1"

    def test_open_blocks_until_cooldown(self, clock):
        breaker = self._breaker(clock)
        breaker.report_failure(FailureCategory.RATE_LIMIT, "HTTP 429", now=clock.now)
        assert breaker.before_query(now=clock.now).allow is False
        clock.advance(29.0)
        assert breaker.before_query(now=clock.now).allow is False
        clock.advance(1.1)
        decision = breaker.before_query(now=clock.now)
        assert decision.allow is True and decision.probe is True
        assert decision.state == BreakerState.HALF_OPEN

    def test_half_open_allows_single_probe(self, clock):
        breaker = self._breaker(clock, half_open_probes=1)
        breaker.report_failure(FailureCategory.RISK_CONTROL, "risk", now=clock.now)
        clock.advance(30.1)
        assert breaker.before_query(now=clock.now).allow is True
        second = breaker.before_query(now=clock.now)
        assert second.allow is False
        assert "探针在飞" in second.reason

    def test_probe_success_closes_after_required_successes(self, clock):
        breaker = self._breaker(clock, require_successes=2)
        breaker.report_failure(FailureCategory.RISK_CONTROL, "risk", now=clock.now)
        clock.advance(30.1)
        breaker.before_query(now=clock.now)
        breaker.report_success(now=clock.now)
        assert breaker.state == BreakerState.HALF_OPEN
        clock.advance(1.0)
        breaker.before_query(now=clock.now)
        breaker.report_success(now=clock.now)
        assert breaker.state == BreakerState.CLOSED
        assert breaker.snapshot(clock.now).trip_count == 0

    def test_probe_failure_opens_again_with_longer_wait(self, clock):
        notifier = RecordingNotifier()
        breaker = self._breaker(clock, notifier=notifier)
        breaker.report_failure(FailureCategory.RISK_CONTROL, "risk", now=clock.now)
        first_wait = breaker.before_query(now=clock.now).wait_seconds

        clock.advance(first_wait + 0.1)
        assert breaker.before_query(now=clock.now).probe is True
        breaker.report_failure(FailureCategory.SERVER, "HTTP 500", now=clock.now)

        second_wait = breaker.before_query(now=clock.now).wait_seconds
        assert second_wait == pytest.approx(first_wait * 2, rel=0.01)
        assert notifier.events == ["RISK_CONTROL", "RISK_CONTROL"]

    def test_breaker_wait_escalates_30s_to_30min(self, clock):
        """规格书 5.2 的 30s -> 5min -> 30min 阶梯（探针持续失败）。"""
        breaker = self._breaker(clock, breaker_multiplier=2.0)
        waits = []
        for _ in range(7):
            breaker.report_failure(FailureCategory.RISK_CONTROL, "risk", now=clock.now)
            waits.append(round(breaker.before_query(now=clock.now).wait_seconds))
            clock.advance(waits[-1] + 0.1)
            breaker.before_query(now=clock.now)  # 取走探针
        assert waits == [30, 60, 120, 240, 480, 960, 1800]

    def test_retry_after_header_is_respected(self, clock):
        breaker = self._breaker(clock)
        decision = breaker.report_failure(FailureCategory.RATE_LIMIT, "429", retry_after=600.0, now=clock.now)
        assert decision.wait_seconds == pytest.approx(600.0)

    def test_retry_after_respects_max_wait(self, clock):
        breaker = self._breaker(clock, max_wait_seconds=300.0)
        decision = breaker.report_failure(FailureCategory.RATE_LIMIT, "429", retry_after=99999.0, now=clock.now)
        assert decision.wait_seconds == pytest.approx(300.0)

    def test_soft_risk_signals_accumulate_then_trip(self, clock):
        """软信号（验证码触发频率异常升高）需要累计到阈值才熔断。"""
        breaker = self._breaker(clock, soft_risk_threshold=3.0)
        first = breaker.report_failure(FailureCategory.RISK_CONTROL, "captcha spike", explicit=False, now=clock.now)
        assert first.state == BreakerState.CLOSED
        breaker.report_failure(FailureCategory.RISK_CONTROL, "captcha spike", explicit=False, now=clock.now)
        assert breaker.state == BreakerState.CLOSED
        third = breaker.report_failure(FailureCategory.RISK_CONTROL, "captcha spike", explicit=False, now=clock.now)
        assert third.state == BreakerState.OPEN

    def test_soft_risk_score_decays(self, clock):
        breaker = self._breaker(clock, soft_risk_threshold=3.0, soft_risk_window=60.0)
        breaker.report_failure(FailureCategory.RISK_CONTROL, "x", explicit=False, now=clock.now)
        assert breaker.snapshot(clock.now).soft_risk_score > 0
        clock.advance(600.0)
        assert breaker.snapshot(clock.now).soft_risk_score == 0.0

    def test_auth_failure_does_not_trip(self, clock):
        """登录态过期要靠重新登录解决，撞墙没用，所以不计入熔断。"""
        notifier = RecordingNotifier()
        breaker = self._breaker(clock, notifier=notifier)
        for _ in range(10):
            breaker.report_failure(FailureCategory.AUTH, "HTTP 302", now=clock.now)
        assert breaker.state == BreakerState.CLOSED
        assert notifier.events == ["login_expired"] * 10

    def test_captcha_failure_does_not_trip(self, clock):
        breaker = self._breaker(clock)
        for _ in range(10):
            breaker.report_failure(FailureCategory.CAPTCHA, "打码失败", now=clock.now)
        assert breaker.state == BreakerState.CLOSED

    def test_open_blocks_aggressive_pre_sale_mode(self, clock):
        """熔断后不允许再走「开售前激进」路径。"""
        breaker = self._breaker(clock)
        breaker.report_failure(FailureCategory.RISK_CONTROL, "risk", now=clock.now)
        decision = breaker.before_query(pre_sale=True, now=clock.now)
        assert decision.allow is False

    def test_freeze_aggressive_flag_readable(self, clock):
        assert self._breaker(clock).freeze_aggressive_when_open() is True

    def test_reset_clears_state(self, clock):
        breaker = self._breaker(clock)
        breaker.report_failure(FailureCategory.RISK_CONTROL, "risk", now=clock.now)
        breaker.reset(now=clock.now)
        assert breaker.state == BreakerState.CLOSED
        assert breaker.before_query(now=clock.now).allow is True

    def test_snapshot_feeds_web_ui(self, clock):
        breaker = self._breaker(clock)
        breaker.report_failure(FailureCategory.SERVER, "HTTP 500", now=clock.now)
        clock.advance(2.5)
        snap = breaker.snapshot(clock.now).as_dict()
        for field in (
            "key", "state", "consecutive_failures", "open_count", "next_probe_in",
            "last_category", "last_reason", "last_result_ago", "counters",
        ):
            assert field in snap
        assert snap["last_result_ago"] == pytest.approx(2.5)
        assert snap["counters"]["server"] == 1

    def test_snapshot_redacts_secrets_in_reason(self, clock):
        breaker = self._breaker(clock)
        breaker.report_failure(FailureCategory.RISK_CONTROL, "cookie=RAIL_DEVICEID=abcdef1234567890abcd")
        assert "abcdef1234567890abcd" not in breaker.snapshot(clock.now).last_reason

    def test_notifier_failure_never_breaks_breaker(self, clock):
        def broken_notifier(event, message="", extra=None):
            raise RuntimeError("notifier exploded")

        breaker = RiskBreaker(RiskConfig(), key="k", clock=clock, notifier=broken_notifier)
        breaker.report_failure(FailureCategory.RISK_CONTROL, "risk", now=clock.now)
        assert breaker.state == BreakerState.OPEN


# --- 组合与抖动 -----------------------------------------------------------


class TestRegistryAndJitter:
    def test_registry_caps_station_pairs(self):
        """规格书 5.6：多车站组合要有上限，否则查询量爆炸。"""
        registry = BreakerRegistry(RiskConfig(), max_keys=5)
        for i in range(5):
            registry.get(f"pair-{i}")
        with pytest.raises(ValueError) as exc:
            registry.get("pair-5")
        assert "上限" in str(exc.value)

    def test_registry_rejects_bad_max(self):
        with pytest.raises(ValueError):
            BreakerRegistry(RiskConfig(), max_keys=0)

    def test_registry_returns_same_instance(self):
        registry = BreakerRegistry(RiskConfig(), max_keys=2)
        assert registry.get("a") is registry.get("a")

    def test_registry_all_open(self, clock):
        registry = BreakerRegistry(RiskConfig(), max_keys=2)
        a = registry.get("a")
        b = registry.get("b")
        assert registry.all_open() is False
        for breaker in (a, b):
            breaker.report_failure(FailureCategory.RISK_CONTROL, "risk", now=clock.now)
        for key, breaker in (("a", a), ("b", b)):
            breaker.clock = clock
        assert registry.all_open() is True

    def test_registry_snapshots_cover_every_key(self):
        registry = BreakerRegistry(RiskConfig(), max_keys=3)
        registry.get("a")
        registry.get("b")
        assert set(registry.snapshots()) == {"a", "b"}

    def test_independent_jitter_per_combo(self):
        """不同组合的退避不能同步（否则等于没有抖动）。"""
        config = RiskConfig(jitter_ratio=0.3)
        keys = ["t1|d1|A-B", "t1|d1|A-C", "t1|d2|A-B"]
        waits = []
        for key in keys:
            breaker = RiskBreaker(config, key=key, stream=new_stream(key))
            waits.append(breaker.report_failure(FailureCategory.RISK_CONTROL, "risk").wait_seconds)
        assert len(set(round(w, 3) for w in waits)) == len(waits)

    def test_same_combo_is_reproducible(self):
        config = RiskConfig(jitter_ratio=0.3)
        first = RiskBreaker(config, key="stable", stream=DeterministicJitter("stable"))
        second = RiskBreaker(config, key="stable", stream=DeterministicJitter("stable"))
        assert first.report_failure(FailureCategory.RISK_CONTROL, "risk").wait_seconds == \
               second.report_failure(FailureCategory.RISK_CONTROL, "risk").wait_seconds
