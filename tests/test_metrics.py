"""指标库测试：采集、分位、时间序列、重启回载、Prometheus 导出。"""

from __future__ import annotations

import time

import pytest

from railkit.metrics import MetricsStore, OUTCOME_TYPES, percentile


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture()
def store(tmp_path):
    clock = FakeClock()
    st = MetricsStore(db_path=tmp_path / "metrics.sqlite3", window_seconds=600.0, flush_interval=0.0, clock=clock)
    st.clock = clock
    yield st
    st.close()


class TestPercentile:
    def test_empty(self):
        assert percentile([], 0.5) == 0.0

    def test_single(self):
        assert percentile([7.0], 0.9) == 7.0

    def test_bounds(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert percentile(values, 0.0) == 1.0
        assert percentile(values, 1.0) == 5.0
        assert percentile(values, 0.5) == 3.0


class TestRecording:
    def test_records_query_and_updates_task(self, store):
        store.record_query("t1", "no_ticket", 300.0, station="北京-上海", label="北京->上海", ts=store.clock.now)
        tasks = store.tasks()
        assert len(tasks) == 1
        assert tasks[0]["task"] == "t1"
        assert tasks[0]["label"] == "北京->上海"
        assert tasks[0]["query_count"] == 1
        assert tasks[0]["outcome_counts"] == {"no_ticket": 1}
        assert tasks[0]["last_duration_ms"] == 300.0

    def test_unknown_outcome_falls_back(self, store):
        store.record_query("t1", "totally-made-up")
        assert store.tasks()[0]["last_outcome"] == "no_ticket"

    def test_summary_counts_and_rate(self, store):
        for i in range(30):
            store.record_query("t1", "no_ticket", 200.0, ts=store.clock.now - i)
        summary = store.summary()
        assert summary["queries_total"] == 30
        assert summary["task_count"] == 1
        # 窗口 600s 内有 30 次 -> 3 次/分钟
        assert summary["queries_per_minute"] == pytest.approx(3.0, abs=0.01)

    def test_latency_percentiles(self, store):
        for value in (100.0, 200.0, 300.0, 400.0, 500.0):
            store.record_query("t1", "no_ticket", value)
        lat = store.summary()["latency_ms"]
        assert lat["p50"] == 300.0
        assert lat["max"] == 500.0

    def test_outcome_tally(self, store):
        store.record_query("t1", "risk_control")
        store.record_query("t1", "risk_control")
        store.record_query("t1", "ticket_found")
        summary = store.summary()
        assert summary["outcomes"]["risk_control"] == 2
        assert summary["risk_control_total"] == 2
        assert summary["outcomes"]["ticket_found"] == 1

    def test_all_outcome_types_accepted(self, store):
        for outcome in OUTCOME_TYPES:
            store.record_query("t1", outcome)
        assert store.tasks()[0]["query_count"] == len(OUTCOME_TYPES)

    def test_window_eviction(self, store):
        future = store.clock.now + 10000
        store.record_query("t1", "no_ticket", ts=store.clock.now)
        store.record_query("t2", "no_ticket", ts=future)
        store.summary(future)
        assert store.summary(future)["queries_in_window"] == 1

    def test_last_query_ago(self, store):
        store.record_query("t1", "no_ticket", ts=store.clock.now)
        later = store.clock.now + 42
        assert store.tasks(later)[0]["last_query_ago"] == pytest.approx(42.0)


class TestBreakerSync:
    def test_update_task_fields(self, store):
        store.update_task("t1", label="L", breaker_state="open", consecutive_failures=4, next_probe_in=12.5)
        task = store.tasks()[0]
        assert task["breaker_state"] == "open"
        assert task["consecutive_failures"] == 4
        assert task["next_probe_in"] == 12.5

    def test_update_task_ignores_unknown_fields(self, store):
        store.update_task("t1", 不存在的字段=1)
        assert store.tasks()[0]["task"] == "t1"

    def test_sync_breakers(self, store):
        store.sync_breakers(
            {
                "k1": {
                    "label": "北京->上海",
                    "state": "half_open",
                    "consecutive_failures": 2,
                    "open_count": 1,
                    "next_probe_in": 3.0,
                    "soft_risk_score": 0.5,
                    "last_reason": "HTTP 429",
                }
            }
        )
        task = store.tasks()[0]
        assert task["breaker_state"] == "half_open"
        assert task["open_count"] == 1
        assert task["last_reason"] == "HTTP 429"

    def test_breaker_events_recorded_and_listed(self, store):
        store.record_breaker("k1", "risk_control", state="open", wait_seconds=30.0, reason="过于频繁")
        events = store.recent_breaker_events()
        assert events[0]["task"] == "k1"
        assert events[0]["state"] == "open"
        assert events[0]["wait_seconds"] == 30.0

    def test_open_tasks_counted(self, store):
        store.update_task("a", breaker_state="open")
        store.update_task("b", breaker_state="closed")
        assert store.summary()["open_tasks"] == 1
        store.update_task("c", breaker_state="backoff")
        assert store.summary()["open_tasks"] == 2


class TestSeries:
    def test_fixed_length_buckets(self, store):
        for i in range(20):
            store.record_query("t1", "no_ticket", 100.0, ts=store.clock.now - i * 30)
        series = store.series(bucket_seconds=60, buckets=10, now=store.clock.now)
        assert len(series["queries"]) == 10
        assert len(series["labels"]) == 10
        assert len(series["latency_ms"]) == 10
        assert sum(series["queries"]) == 20

    def test_empty_buckets_are_zero(self, store):
        series = store.series(bucket_seconds=60, buckets=5, now=store.clock.now)
        assert series["queries"] == [0, 0, 0, 0, 0]

    def test_risk_and_ticket_split(self, store):
        store.record_query("t1", "risk_control", ts=store.clock.now)
        store.record_query("t1", "ticket_found", ts=store.clock.now)
        store.record_query("t1", "server_error", ts=store.clock.now)
        series = store.series(bucket_seconds=60, buckets=3, now=store.clock.now)
        assert sum(series["risk_control"]) == 1
        assert sum(series["tickets"]) == 1
        assert sum(series["failures"]) == 1

    def test_breaker_opens_counted_by_bucket(self, store):
        store.record_breaker("k", "risk_control", state="open", ts=store.clock.now)
        series = store.series(bucket_seconds=60, buckets=3, now=store.clock.now)
        assert sum(series["breaker_opens"]) == 1

    def test_bucket_bounds_are_respected(self, store):
        store.record_query("t1", "no_ticket", ts=store.clock.now - 10000)
        series = store.series(bucket_seconds=60, buckets=5, now=store.clock.now)
        assert sum(series["queries"]) == 0


class TestPersistence:
    def test_reload_after_restart(self, tmp_path):
        db = tmp_path / "m.sqlite3"
        first = MetricsStore(db_path=db, window_seconds=3600.0, flush_interval=0.0)
        for i in range(5):
            first.record_query("t1", "no_ticket", 250.0, label="北京->上海")
        first.close()

        second = MetricsStore(db_path=db, window_seconds=3600.0, flush_interval=0.0)
        try:
            assert len(second.tasks()) == 1
            assert second.tasks()[0]["query_count"] == 5
            assert second.summary()["queries_total"] == 5
            assert sum(second.series(bucket_seconds=600, buckets=6)["queries"]) == 5
        finally:
            second.close()

    def test_restart_loads_breaker_events(self, tmp_path):
        db = tmp_path / "m.sqlite3"
        first = MetricsStore(db_path=db, window_seconds=3600.0, flush_interval=0.0)
        first.record_breaker("k1", "risk_control", state="open", wait_seconds=30.0)
        first.close()
        second = MetricsStore(db_path=db, window_seconds=3600.0, flush_interval=0.0)
        try:
            assert second.recent_breaker_events()[0]["state"] == "open"
        finally:
            second.close()

    def test_no_db_path_works_in_memory(self):
        store = MetricsStore(db_path=None)
        store.record_query("t", "no_ticket")
        assert store.summary()["queries_total"] == 1
        assert store.summary()["sqlite_path"] is None
        store.close()

    def test_close_is_idempotent(self, tmp_path):
        store = MetricsStore(db_path=tmp_path / "m.sqlite3")
        store.close()
        store.close()


class TestPrometheus:
    def test_exposition_format(self, store):
        store.record_query("t1", "risk_control", 100.0)
        store.record_query("t1", "success", 200.0)
        store.update_task("t1", breaker_state="open")
        text = store.to_prometheus()
        assert "ticket_query_total{" in text
        assert 'task_circuit_breaker_open{task="t1"} 1' in text
        assert "ticket_risk_control_total 1" in text
        assert "ticket_success_total 1" in text
        assert text.endswith("\n")

    def test_label_escaping(self, store):
        store.update_task('we"ird\\name', breaker_state="open")
        text = store.to_prometheus()
        assert 'task="we\\"ird\\\\name"' in text
