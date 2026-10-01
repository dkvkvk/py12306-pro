"""查询循环集成测试：用假 Job/假响应验证熔断与抖动真的生效。

这里不导入 py12306 本体（它需要 requests-html 等重依赖），而是用鸭子类型的假对象，
既能验证 integration 的逻辑，也能在 CI 里快速跑。
"""

from __future__ import annotations

import time

import pytest

from railkit.integration import IntegrationConfig, JobRiskAdapter, QueryLoopIntegration
from railkit.metrics import MetricsStore
from railkit.notifier import MemoryAdapter, NotifyHub
from railkit.risk import FailureCategory, RiskConfig


class FakeResponse:
    def __init__(self, status_code=200, text='{"data": {"result": ["a|b|c"]}}', reason="OK", headers=None):
        self.status_code = status_code
        self.text = text
        self.reason = reason
        self.headers = headers or {}
        self.elapsed = type("E", (), {"total_seconds": lambda self: 0.3})()


class FakeJob:
    def __init__(self, name="G1", left="北京", arrive="上海", date="2026-10-01"):
        self.job_name = name
        self.left_station = left
        self.arrive_station = arrive
        self.left_station_code = "BJP"
        self.arrive_station_code = "SHH"
        self.left_date = date
        self.INDEX_TICKET_NUM = 11
        self.INDEX_ORDER_TEXT = 1
        self.INDEX_TRAIN_NUMBER = 3


def make_integration(tmp_path, **risk_kwargs):
    store = MetricsStore(db_path=tmp_path / "m.sqlite3", window_seconds=3600.0, flush_interval=0.0)
    hub = NotifyHub([MemoryAdapter()], sleep=lambda _s: None, retry_base=0.0)
    # jitter_ratio 默认关闭，测试里要看确定值；想验证抖动时显式传入
    risk_kwargs.setdefault("jitter_ratio", 0.0)
    config = IntegrationConfig(
        risk=RiskConfig(**risk_kwargs),
        query_interval=4.0,
        max_station_pairs=3,
    )
    integration = QueryLoopIntegration(config=config, store=store, hub=hub)
    return integration, store, hub


class TestOutcomeClassification:
    def test_success_200_marks_no_ticket_and_breaker_success(self, tmp_path):
        integration, store, _hub = make_integration(tmp_path)
        job = FakeJob()
        outcome = integration.after_response(job, FakeResponse(200), 250.0)
        assert outcome == "no_ticket"
        assert store.tasks()[0]["last_outcome"] == "no_ticket"

    def test_swallowed_exception_is_timeout(self, tmp_path):
        """上游 Request.request() 会把异常吞成空响应，状态码 0。"""
        integration, store, _ = make_integration(tmp_path)
        outcome = integration.after_response(FakeJob(), FakeResponse(0, "", "Response Empty Error"), 5000.0)
        assert outcome == "timeout"
        assert store.tasks()[0]["outcome_counts"]["timeout"] == 1

    def test_risk_control_body_on_200(self, tmp_path):
        integration, store, hub = make_integration(tmp_path)
        response = FakeResponse(200, '{"result_message": "您的访问过于频繁，请稍后再试"}')
        outcome = integration.after_response(FakeJob(), response, 300.0)
        assert outcome == "risk_control"
        assert store.tasks()[0]["breaker_state"] == "open"
        assert any(m.event == "RISK_CONTROL" for m in hub.adapters[0].messages)

    def test_rate_limit_403(self, tmp_path):
        integration, store, _ = make_integration(tmp_path)
        assert integration.after_response(FakeJob(), FakeResponse(403, "forbidden"), 100.0) == "rate_limit"
        assert store.tasks()[0]["breaker_state"] == "open"

    def test_server_error_5xx(self, tmp_path):
        integration, store, _ = make_integration(tmp_path)
        assert integration.after_response(FakeJob(), FakeResponse(503, "", "Service Unavailable"), 100.0) == "server_error"

    def test_auth_error_302(self, tmp_path):
        integration, store, hub = make_integration(tmp_path)
        outcome = integration.after_response(FakeJob(), FakeResponse(302, "", "Found"), 80.0)
        assert outcome == "auth_error"
        # 登录态失效要告警，但不应该熔断
        assert store.tasks()[0]["breaker_state"] == "closed"
        assert any(m.event == "LOGIN_EXPIRED" for m in hub.adapters[0].messages)

    def test_retry_after_header_is_honoured(self, tmp_path):
        integration, store, _ = make_integration(tmp_path)
        integration.after_response(FakeJob(), FakeResponse(429, "", "Too Many", {"Retry-After": "240"}), 100.0)
        assert store.tasks()[0]["breaker_state"] == "open"
        # Retry-After 只设下限；熔断等待还会带抖动，所以断言区间而不是精确值
        assert 235.0 <= store.tasks()[0]["next_probe_in"] <= 300.0

    def test_ticket_found_recorded(self, tmp_path):
        integration, store, _ = make_integration(tmp_path)
        integration.on_ticket_found(FakeJob(), "G1234")
        assert store.tasks()[0]["outcome_counts"]["ticket_found"] == 1

    def test_order_success_recorded(self, tmp_path):
        integration, store, _hub = make_integration(tmp_path)
        integration.on_order_success(FakeJob())
        assert store.tasks()[0]["outcome_counts"]["success"] == 1


class TestCircuitBreakerDrivenByJob:
    def test_consecutive_failures_lead_to_backoff(self, tmp_path):
        integration, store, _ = make_integration(tmp_path, failure_threshold=3, failure_backoff_base=3.0)
        job = FakeJob()
        for _ in range(3):
            integration.after_response(job, FakeResponse(503, "", "err"), 100.0)
        task = store.tasks()[0]
        assert task["breaker_state"] == "backoff"
        assert task["consecutive_failures"] == 3

    def test_delay_is_blocked_while_open_and_reported_as_circuit_open(self, tmp_path):
        integration, store, _ = make_integration(tmp_path, breaker_base=30.0)
        job = FakeJob()
        integration.after_response(job, FakeResponse(200, "您的访问过于频繁"), 100.0)

        delay = integration.before_query(job)
        assert 29.0 <= delay <= 36.0  # base 30s 起步 + 最多 20% 抖动
        assert store.tasks()[0]["outcome_counts"]["circuit_open"] == 1

    def test_delay_is_generated_after_cooldown(self, tmp_path):
        integration, store, _ = make_integration(tmp_path, breaker_base=1.0)
        job = FakeJob()
        integration.after_response(job, FakeResponse(200, "您的访问过于频繁"), 100.0)
        time.sleep(1.05)
        delay = integration.before_query(job)
        assert 0.5 <= delay <= 8.0  # 探针放行，走自适应抖动

    def test_delay_within_configured_jitter_band(self, tmp_path):
        integration, _store, _ = make_integration(tmp_path, jitter_ratio=0.3)
        job = FakeJob()
        delays = [integration.before_query(job) for _ in range(50)]
        assert all(2.8 <= d <= 5.2 for d in delays), (min(delays), max(delays))
        assert len(set(round(d, 3) for d in delays)) > 1  # 必须有抖动，不能恒定

    def test_keys_are_independent(self, tmp_path):
        integration, _store, _ = make_integration(tmp_path, breaker_base=30.0)
        job_a = FakeJob(name="A")
        job_b = FakeJob(name="B")
        integration.after_response(job_a, FakeResponse(200, "您的访问过于频繁"), 100.0)
        # A 被熔断，B 不受影响
        assert integration.before_query(job_a) >= 29.0      # A 在熔断冷却里
        assert integration.before_query(job_b) < 10.0        # B 不受影响

    def test_max_station_pairs_enforced(self, tmp_path):
        integration, _store, _ = make_integration(tmp_path)
        for i in range(3):
            integration.before_query(FakeJob(name="job-%d" % i, date="2026-10-0%d" % (i + 1)))
        with pytest.raises(ValueError):
            integration.before_query(FakeJob(name="job-4", date="2026-10-04"))

    def test_disabled_integration_passes_through(self, tmp_path):
        integration, store, _ = make_integration(tmp_path)
        integration.enabled = False
        assert integration.before_query(FakeJob()) == 0.0
        assert integration.after_response(FakeJob(), FakeResponse(200)) == ""
        assert store.tasks() == []


class TestPreSaleWindow:
    def test_detects_window_before_full_hour(self, tmp_path):
        integration, _store, _ = make_integration(tmp_path)
        adapter = JobRiskAdapter(
            FakeJob(), registry=integration.registry, store=integration.store, config=integration.config
        )
        # 09:55 -> 距整点 5 分钟，处于 60 分钟激进窗口内
        nine_fifty_five = time.mktime((2026, 10, 1, 9, 55, 0, 0, 0, -1))
        assert adapter.is_pre_sale(nine_fifty_five) is True
        # 09:05 -> 距整点 55 分钟，仍在内（窗口 60 分钟）
        assert adapter.is_pre_sale(time.mktime((2026, 10, 1, 9, 5, 0, 0, 0, -1))) is True

    def test_window_can_be_disabled(self, tmp_path):
        integration, _store, _ = make_integration(tmp_path)
        integration.config.pre_sale_window_minutes = 0
        adapter = JobRiskAdapter(
            FakeJob(), registry=integration.registry, store=integration.store, config=integration.config
        )
        assert adapter.is_pre_sale(time.mktime((2026, 10, 1, 9, 59, 0, 0, 0, -1))) is False


class TestRebinding:
    def test_key_changes_when_station_or_date_changes(self, tmp_path):
        integration, store, _ = make_integration(tmp_path)
        job = FakeJob()
        first = integration.before_query(job)
        assert first > 0
        key_before = list(integration.registry.keys())[0]

        job.left_date = "2026-10-02"
        integration.before_query(job)
        keys = list(integration.registry.keys())
        assert len(keys) == 2
        assert key_before in keys
        assert "2026-10-02" in keys[1]

    def test_forget_removes_adapter(self, tmp_path):
        integration, _store, _ = make_integration(tmp_path)
        job = FakeJob()
        integration.before_query(job)
        assert len(integration._adapters) == 1
        integration.forget(job)
        assert integration._adapters == {}


class TestNotifications:
    def test_login_expired_and_captcha_helpers(self, tmp_path):
        integration, _store, hub = make_integration(tmp_path)
        integration.record_login_expired("tester")
        integration.record_captcha_failed("tester", "打码平台 502")
        events = [m.event for m in hub.adapters[0].messages]
        assert "LOGIN_EXPIRED" in events
        assert "CAPTCHA_FAILED" in events

    def test_secrets_in_reason_are_redacted(self, tmp_path):
        integration, store, _ = make_integration(tmp_path)
        integration.after_response(
            FakeJob(), FakeResponse(200, "cookie RAIL_DEVICEID=abcdefg1234567890 now"), 10.0
        )
        reason = store.tasks()[0]["last_reason"]
        assert "abcdefg1234567890" not in reason
