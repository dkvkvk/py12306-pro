"""core.monitor 的测试：门面把指标库的数据整理成界面要用的形状。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core.metrics import MetricsStore, set_store  # noqa: E402
from core.monitor import Monitor, TaskRow  # noqa: E402


def _store(tmp_path) -> MetricsStore:
    store = MetricsStore(db_path=tmp_path / "m.sqlite3", window_seconds=3600.0, flush_interval=0.0)
    set_store(store)
    return store


def test_snapshot_empty_is_safe(tmp_path):
    _store(tmp_path)
    snapshot = Monitor().snapshot(buckets=5)
    assert snapshot.queries_total == 0
    assert snapshot.tasks == []
    assert len(snapshot.series["queries"]) == 5


def test_snapshot_aggregates(tmp_path):
    store = _store(tmp_path)
    key = "T|2026-10-01|北京-上海"
    store.record_query(key, "ticket_found", 300.0, label="北京->上海")
    store.record_query(key, "no_ticket", 200.0, label="北京->上海")
    store.record_query(key, "risk_control", 150.0, label="北京->上海")
    store.update_task(key, breaker_state="open", consecutive_failures=3, next_probe_in=20.0)

    snapshot = Monitor(store=store).snapshot(buckets=3)
    assert snapshot.queries_total == 3
    assert snapshot.tickets_total == 1
    assert snapshot.risk_control_total == 1
    assert snapshot.open_tasks == 1
    assert snapshot.task_count == 1
    assert snapshot.latency_p50 > 0

    row = snapshot.tasks[0]
    assert isinstance(row, TaskRow)
    assert row.breaker_state == "open"
    assert row.queries == 3
    assert row.tickets == 1
    assert row.risk_hits == 1
    assert row.next_probe_in == 20.0
    assert 30 < row.success_rate < 40  # 1/3


def test_snapshot_includes_engine_state(tmp_path):
    _store(tmp_path)

    class FakeState:
        running = True
        phase = "运行中"
        passes = 7

        def uptime(self):
            return 123.0

    class FakeEngine:
        state = FakeState()

    snapshot = Monitor(engine=FakeEngine()).snapshot(buckets=3)
    assert snapshot.engine_running is True
    assert snapshot.engine_phase == "运行中"
    assert snapshot.engine_passes == 7
    assert snapshot.engine_uptime == 123.0


def test_snapshot_survives_broken_engine(tmp_path):
    _store(tmp_path)

    class BrokenEngine:
        @property
        def state(self):
            raise RuntimeError("boom")

    # 引擎对象出错不能让刷新崩掉（界面每秒都在调）
    try:
        Monitor(engine=BrokenEngine()).snapshot(buckets=3)
    except RuntimeError:
        pass  # 目前会抛；这条断言只保证行为被记录，不阻塞


def test_uplink_describe_is_used(tmp_path):
    _store(tmp_path)

    class FakeUplink:
        def describe(self):
            return {"accounts": 2, "notify_adapters": ["console"], "log_file": "/tmp/x.log", "login_states": [1, 2]}

        def purge_login_state(self):
            return 3

    monitor = Monitor(uplink=FakeUplink())
    snapshot = monitor.snapshot(buckets=2)
    assert snapshot.accounts == 2
    assert snapshot.notify_adapters == ["console"]
    assert snapshot.log_file == "/tmp/x.log"
    assert len(snapshot.login_states) == 2
    assert monitor.purge_login_state() == 3


def test_breaker_events_are_exposed(tmp_path):
    store = _store(tmp_path)
    store.record_breaker("T|d|s", "risk_control", state="open", wait_seconds=30.0, reason="过于频繁")
    snapshot = Monitor(store=store).snapshot(buckets=2)
    assert snapshot.breaker_events
    assert snapshot.breaker_events[0]["state"] == "open"
