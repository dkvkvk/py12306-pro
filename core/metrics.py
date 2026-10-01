"""运行指标采集：面板的数据底座。

设计取舍：
- 纯标准库（sqlite3 + threading），不引入 TSDB。单机抢票场景下 SQLite 足够，
  且「不需要额外服务」比「能存十亿点」重要得多。
- 内存里保留滚动窗口（供面板高频读取），按批落盘 SQLite（供重启后仍有历史）。
- 所有采样都是「一次性写入 + 无锁读」：采集点不能拖慢查询主循环。

采集口径（与 spec 第 8 节的指标对齐）：
    ticket_query_total{station,date}         -> events 表 outcome 计数
    ticket_query_duration_seconds            -> 延迟分位
    ticket_success_total                     -> outcome=success
    ticket_risk_control_total                -> outcome=risk_control
    task_circuit_breaker_open                -> breaker 状态快照
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

#: 查询结果分类
OUTCOME_TYPES = (
    "no_ticket",       # 查到了，但没票
    "ticket_found",    # 查到有票
    "success",         # 下单成功
    "server_error",    # 5xx
    "timeout",         # 超时 / 连接失败
    "risk_control",    # 命中风控
    "rate_limit",      # 403/429
    "auth_error",      # 登录态失效
    "captcha_failed",  # 打码失败
    "circuit_open",    # 被熔断拦下（本地主动不发请求）
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task TEXT NOT NULL,
    station TEXT NOT NULL DEFAULT '',
    travel_date TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    duration_ms REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_query_events_ts ON query_events(ts);
CREATE INDEX IF NOT EXISTS idx_query_events_task ON query_events(task, ts);

CREATE TABLE IF NOT EXISTS breaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task TEXT NOT NULL,
    category TEXT NOT NULL,
    previous_state TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    wait_seconds REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_breaker_events_ts ON breaker_events(ts);

CREATE TABLE IF NOT EXISTS task_snapshots (
    task TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);
"""


def percentile(values: List[float], ratio: float) -> float:
    """最近邻分位，够用且不引入 numpy。values 会保持原序。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if ratio <= 0:
        return ordered[0]
    if ratio >= 1:
        return ordered[-1]
    index = int(round(ratio * (len(ordered) - 1)))
    return ordered[max(0, min(index, len(ordered) - 1))]


@dataclass
class QueryEvent:
    ts: float
    task: str
    outcome: str
    duration_ms: float = 0.0
    station: str = ""
    travel_date: str = ""


@dataclass
class BreakerEvent:
    ts: float
    task: str
    category: str
    previous_state: str = ""
    state: str = ""
    wait_seconds: float = 0.0
    reason: str = ""


@dataclass
class TaskState:
    """面板左侧任务列表用的实时状态。"""

    task: str
    label: str = ""
    station: str = ""
    travel_date: str = ""
    last_query_ago: Optional[float] = None
    last_outcome: str = ""
    last_duration_ms: float = 0.0
    query_count: int = 0
    outcome_counts: Dict[str, int] = field(default_factory=dict)
    breaker_state: str = "closed"
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    open_count: int = 0
    next_probe_in: float = 0.0
    soft_risk_score: float = 0.0
    last_reason: str = ""
    updated_at: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "label": self.label,
            "station": self.station,
            "travel_date": self.travel_date,
            "last_query_ago": self.last_query_ago,
            "last_outcome": self.last_outcome,
            "last_duration_ms": round(self.last_duration_ms, 1),
            "query_count": self.query_count,
            "outcome_counts": dict(self.outcome_counts),
            "breaker_state": self.breaker_state,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "open_count": self.open_count,
            "next_probe_in": round(self.next_probe_in, 1),
            "soft_risk_score": round(self.soft_risk_score, 3),
            "last_reason": self.last_reason,
            "updated_at": self.updated_at,
        }


class MetricsStore:
    """内存滚动窗口 + SQLite 持久化。线程安全（一把锁，临界区都很短）。"""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        window_seconds: float = 3600.0,
        flush_interval: float = 5.0,
        clock: Any = time.time,
    ) -> None:
        self.db_path = Path(db_path) if db_path else None
        self.window_seconds = window_seconds
        self.flush_interval = flush_interval
        self._clock = clock
        self._lock = threading.RLock()
        self._events: Deque[QueryEvent] = deque(maxlen=20000)
        self._breaker_events: Deque[BreakerEvent] = deque(maxlen=2000)
        self._tasks: Dict[str, TaskState] = {}
        self._pending_events: List[QueryEvent] = []
        self._pending_breaker: List[BreakerEvent] = []
        self._started_at = self._clock()
        self._last_flush = 0.0
        self._conn: Optional[sqlite3.Connection] = None
        if self.db_path is not None:
            self._init_db()
            # 重启后要把窗口内的历史读回来，否则面板一重启图表就空了
            self._restore_from_db()

    # -- 存储 ----------------------------------------------------------

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _restore_from_db(self) -> None:
        if self._conn is None:
            return
        horizon = self._clock() - self.window_seconds
        try:
            rows = self._conn.execute(
                "SELECT ts, task, station, travel_date, outcome, duration_ms FROM query_events"
                " WHERE ts >= ? ORDER BY ts",
                (horizon,),
            ).fetchall()
            for ts, task, station, travel_date, outcome, duration_ms in rows:
                event = QueryEvent(
                    ts=ts,
                    task=task,
                    outcome=outcome,
                    duration_ms=duration_ms,
                    station=station or "",
                    travel_date=travel_date or "",
                )
                self._events.append(event)
                state = self._task_locked(task)
                state.query_count += 1
                state.outcome_counts[outcome] = state.outcome_counts.get(outcome, 0) + 1
                state.last_outcome = outcome
                state.last_duration_ms = duration_ms
                state.station = station or state.station
                state.travel_date = travel_date or state.travel_date
                state.updated_at = ts
                self._started_at = min(self._started_at, ts)

            for ts, task, category, previous_state, state_name, wait_seconds, reason in self._conn.execute(
                "SELECT ts, task, category, previous_state, state, wait_seconds, reason FROM breaker_events"
                " WHERE ts >= ? ORDER BY ts",
                (horizon,),
            ).fetchall():
                self._breaker_events.append(
                    BreakerEvent(
                        ts=ts,
                        task=task,
                        category=category,
                        previous_state=previous_state or "",
                        state=state_name or "",
                        wait_seconds=wait_seconds or 0.0,
                        reason=reason or "",
                    )
                )

            for task, payload in self._conn.execute("SELECT task, payload FROM task_snapshots").fetchall():
                try:
                    data = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                state = self._task_locked(task)
                for key, value in data.items():
                    # 计数类与时间相对量不能从旧快照回灌：
                    # 计数由事件重放累积，next_probe_in / last_query_ago 是「距今」的瞬时值
                    if key in ("outcome_counts", "task", "query_count", "next_probe_in", "last_query_ago"):
                        continue
                    if hasattr(state, key):
                        setattr(state, key, value)
        except sqlite3.Error:
            # 读不回来不影响采集，只是面板少一段历史
            pass

    def _flush_locked(self, force: bool = False) -> None:
        if self._conn is None:
            return
        now = self._clock()
        if not force and (now - self._last_flush) < self.flush_interval:
            return
        self._last_flush = now
        try:
            if self._pending_events:
                self._conn.executemany(
                    "INSERT INTO query_events (ts, task, station, travel_date, outcome, duration_ms)"
                    " VALUES (?,?,?,?,?,?)",
                    [(e.ts, e.task, e.station, e.travel_date, e.outcome, e.duration_ms) for e in self._pending_events],
                )
                self._pending_events.clear()
            if self._pending_breaker:
                self._conn.executemany(
                    "INSERT INTO breaker_events (ts, task, category, previous_state, state, wait_seconds, reason)"
                    " VALUES (?,?,?,?,?,?,?)",
                    [
                        (e.ts, e.task, e.category, e.previous_state, e.state, e.wait_seconds, e.reason)
                        for e in self._pending_breaker
                    ],
                )
                self._pending_breaker.clear()
            if self._tasks:
                self._conn.executemany(
                    "INSERT INTO task_snapshots (task, ts, payload) VALUES (?,?,?)"
                    " ON CONFLICT(task) DO UPDATE SET ts=excluded.ts, payload=excluded.payload",
                    [
                        (task, st.updated_at, json.dumps(st.as_dict(), ensure_ascii=False))
                        for task, st in self._tasks.items()
                    ],
                )
            self._conn.commit()
        except sqlite3.Error:
            # 指标落盘失败绝不能影响抢票主流程
            pass

    def close(self) -> None:
        with self._lock:
            self._flush_locked(force=True)
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # -- 写入 ----------------------------------------------------------

    def _task_locked(self, task: str, label: str = "") -> TaskState:
        state = self._tasks.get(task)
        if state is None:
            state = TaskState(task=task, label=label or task)
            self._tasks[task] = state
        elif label:
            state.label = label
        return state

    def record_query(
        self,
        task: str,
        outcome: str,
        duration_ms: float = 0.0,
        *,
        station: str = "",
        travel_date: str = "",
        label: str = "",
        ts: Optional[float] = None,
    ) -> None:
        now = self._clock() if ts is None else ts
        if outcome not in OUTCOME_TYPES:
            outcome = "no_ticket"
        event = QueryEvent(ts=now, task=task, outcome=outcome, duration_ms=duration_ms, station=station, travel_date=travel_date)
        with self._lock:
            self._events.append(event)
            self._pending_events.append(event)
            state = self._task_locked(task, label)
            state.query_count += 1
            state.outcome_counts[outcome] = state.outcome_counts.get(outcome, 0) + 1
            state.last_outcome = outcome
            state.last_duration_ms = duration_ms
            state.last_query_ago = 0.0
            state.station = station or state.station
            state.travel_date = travel_date or state.travel_date
            state.updated_at = now
            self._trim_locked(now)
            self._flush_locked()

    def record_breaker(
        self,
        task: str,
        category: str,
        *,
        previous_state: str = "",
        state: str = "",
        wait_seconds: float = 0.0,
        reason: str = "",
        label: str = "",
        ts: Optional[float] = None,
    ) -> None:
        now = self._clock() if ts is None else ts
        event = BreakerEvent(
            ts=now,
            task=task,
            category=category,
            previous_state=previous_state,
            state=state,
            wait_seconds=wait_seconds,
            reason=reason,
        )
        with self._lock:
            self._breaker_events.append(event)
            self._pending_breaker.append(event)
            st = self._task_locked(task, label)
            if state:
                st.breaker_state = state
            st.updated_at = now
            self._flush_locked()

    def update_task(self, task: str, **fields: Any) -> None:
        """用熔断器快照刷新任务状态（面板直接读这个）。"""
        with self._lock:
            state = self._task_locked(task, fields.get("label", ""))
            for key, value in fields.items():
                if key == "outcome_counts":
                    continue
                if hasattr(state, key):
                    setattr(state, key, value)
            state.updated_at = self._clock()
            self._flush_locked()

    def sync_breakers(self, snapshots: Dict[str, Dict[str, Any]]) -> None:
        """把 BreakerRegistry.snapshots() 的结果灌进来。"""
        if not snapshots:
            return
        with self._lock:
            for key, snap in snapshots.items():
                state = self._task_locked(key, snap.get("label", ""))
                state.breaker_state = snap.get("state", state.breaker_state)
                state.consecutive_failures = snap.get("consecutive_failures", 0)
                state.consecutive_successes = snap.get("consecutive_successes", 0)
                state.open_count = snap.get("open_count", 0)
                state.next_probe_in = snap.get("next_probe_in", 0.0)
                state.soft_risk_score = snap.get("soft_risk_score", 0.0)
                state.last_reason = snap.get("last_reason", "")
                if snap.get("label"):
                    state.label = snap["label"]
                state.updated_at = self._clock()

    def _trim_locked(self, now: float) -> None:
        horizon = now - self.window_seconds
        while self._events and self._events[0].ts < horizon:
            self._events.popleft()
        while self._breaker_events and self._breaker_events[0].ts < horizon:
            self._breaker_events.popleft()

    # -- 读取 ----------------------------------------------------------

    def tasks(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = self._clock() if now is None else now
        with self._lock:
            out = []
            for state in self._tasks.values():
                item = state.as_dict()
                # 「上次查询距今」是相对量：只要知道最后更新时间就能算，
                # 不必依赖采集时刻写入的值（重启回载后那个值是过期的）
                if state.updated_at:
                    item["last_query_ago"] = round(max(0.0, ts - state.updated_at), 1)
                out.append(item)
            return sorted(out, key=lambda d: d["task"])

    def summary(self, now: Optional[float] = None) -> Dict[str, Any]:
        ts = self._clock() if now is None else now
        with self._lock:
            events = list(self._events)
            durations = [e.duration_ms for e in events if e.duration_ms > 0]
            outcomes: Dict[str, int] = {}
            for event in events:
                outcomes[event.outcome] = outcomes.get(event.outcome, 0) + 1
            # 用窗口长度而不是「进程存活时长」：重启回载历史后，历史也必须计入分母
            window = max(1.0, self.window_seconds)
            opened = len([e for e in self._breaker_events if e.state in ("open", "half_open")])
            return {
                "uptime_seconds": round(ts - self._started_at, 1),
                "task_count": len(self._tasks),
                "queries_total": sum(st.query_count for st in self._tasks.values()),
                "queries_in_window": len(events),
                "queries_per_minute": round(len(events) / window * 60.0, 1),
                "outcomes": outcomes,
                "latency_ms": {
                    "p50": round(percentile(durations, 0.5), 1),
                    "p90": round(percentile(durations, 0.9), 1),
                    "p99": round(percentile(durations, 0.99), 1),
                    "max": round(max(durations), 1) if durations else 0.0,
                },
                "risk_control_total": outcomes.get("risk_control", 0),
                "success_total": outcomes.get("success", 0),
                "circuit_open_total": opened,
                "open_tasks": len([s for s in self._tasks.values() if s.breaker_state in ("open", "backoff")]),
                "sqlite_path": str(self.db_path) if self.db_path else None,
            }

    def series(self, bucket_seconds: float = 60.0, buckets: int = 30, now: Optional[float] = None) -> Dict[str, Any]:
        """返回定长时间序列（面板折线图直接画）。

        桶按「距现在多少个桶」索引，空桶填 0，保证前端拿到等长数组。
        """
        ts = self._clock() if now is None else now
        bucket_seconds = max(1.0, float(bucket_seconds))
        buckets = max(1, int(buckets))
        start = ts - bucket_seconds * buckets
        with self._lock:
            events = [e for e in self._events if e.ts >= start]
            breaker_events = [e for e in self._breaker_events if e.ts >= start]

        counts = [0] * buckets
        tickets = [0] * buckets
        risk = [0] * buckets
        failures = [0] * buckets
        durations: List[List[float]] = [[] for _ in range(buckets)]

        def bucket_of(value: float) -> Optional[int]:
            if value < start or value > ts:
                return None
            idx = int((value - start) / bucket_seconds)
            return max(0, min(buckets - 1, idx))

        for event in events:
            idx = bucket_of(event.ts)
            if idx is None:
                continue
            counts[idx] += 1
            if event.outcome in ("ticket_found", "success"):
                tickets[idx] += 1
            if event.outcome in ("risk_control", "rate_limit"):
                risk[idx] += 1
            if event.outcome in ("server_error", "timeout"):
                failures[idx] += 1
            if event.duration_ms > 0:
                durations[idx].append(event.duration_ms)

        opens = [0] * buckets
        for event in breaker_events:
            idx = bucket_of(event.ts)
            if idx is not None and event.state == "open":
                opens[idx] += 1

        return {
            "bucket_seconds": bucket_seconds,
            "buckets": buckets,
            "start_ts": start,
            "end_ts": ts,
            "labels": [int(start + bucket_seconds * (i + 1)) for i in range(buckets)],
            "queries": counts,
            "tickets": tickets,
            "risk_control": risk,
            "failures": failures,
            "breaker_opens": opens,
            "latency_ms": [
                {"p50": round(percentile(bucket, 0.5), 1), "p90": round(percentile(bucket, 0.9), 1)}
                for bucket in durations
            ],
        }

    def recent_breaker_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            events = list(self._breaker_events)[-limit:]
        return [
            {
                "ts": e.ts,
                "task": e.task,
                "category": e.category,
                "previous_state": e.previous_state,
                "state": e.state,
                "wait_seconds": e.wait_seconds,
                "reason": e.reason,
            }
            for e in reversed(events)
        ]

    def to_prometheus(self, now: Optional[float] = None) -> str:
        """可选的 Prometheus 文本格式导出（spec 第 8 节）。不引入 prometheus_client。"""
        ts = self._clock() if now is None else now
        with self._lock:
            events = list(self._events)
            tasks = list(self._tasks.values())
            breaker_events = list(self._breaker_events)

        lines: List[str] = []
        lines.append("# HELP ticket_query_total 余票查询次数（按结果分类）")
        lines.append("# TYPE ticket_query_total counter")
        per_outcome: Dict[str, int] = {}
        per_task: Dict[str, int] = {}
        for event in events:
            per_outcome[event.outcome] = per_outcome.get(event.outcome, 0) + 1
            per_task[event.task] = per_task.get(event.task, 0) + 1
        for outcome, count in sorted(per_outcome.items()):
            lines.append('ticket_query_total{outcome="%s"} %d' % (outcome, count))
        lines.append("# HELP ticket_query_duration_seconds 查询延迟")
        lines.append("# TYPE ticket_query_duration_seconds summary")
        durations = [e.duration_ms / 1000.0 for e in events if e.duration_ms > 0]
        for quantile, name in ((0.5, "0.5"), (0.9, "0.9"), (0.99, "0.99")):
            lines.append('ticket_query_duration_seconds{quantile="%s"} %.4f' % (name, percentile(durations, quantile)))
        lines.append("# HELP ticket_success_total 下单成功数")
        lines.append("# TYPE ticket_success_total counter")
        lines.append("ticket_success_total %d" % per_outcome.get("success", 0))
        lines.append("# HELP ticket_risk_control_total 风控命中数")
        lines.append("# TYPE ticket_risk_control_total counter")
        lines.append("ticket_risk_control_total %d" % (per_outcome.get("risk_control", 0) + per_outcome.get("rate_limit", 0)))
        lines.append("# HELP task_circuit_breaker_open 任务熔断状态（1=熔断中）")
        lines.append("# TYPE task_circuit_breaker_open gauge")
        for state in tasks:
            value = 1 if state.breaker_state in ("open", "backoff") else 0
            lines.append('task_circuit_breaker_open{task="%s"} %d' % (_escape_label(state.task), value))
        lines.append("# HELP ticket_breaker_open_total 熔断触发次数")
        lines.append("# TYPE ticket_breaker_open_total counter")
        opens = len([e for e in breaker_events if e.state == "open"])
        lines.append("ticket_breaker_open_total %d" % opens)
        lines.append("# HELP ticket_uptime_seconds 进程运行时长")
        lines.append("# TYPE ticket_uptime_seconds gauge")
        lines.append("ticket_uptime_seconds %.1f" % (ts - self._started_at))
        return "\n".join(lines) + "\n"


def _escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_STORE: Optional[MetricsStore] = None


def get_store() -> MetricsStore:
    """全局单例；面板和查询循环共用。"""
    global _STORE
    if _STORE is None:
        _STORE = MetricsStore()
    return _STORE


def set_store(store: MetricsStore) -> None:
    global _STORE
    _STORE = store
