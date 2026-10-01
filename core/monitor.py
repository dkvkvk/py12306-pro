"""状态门面：界面只通过它读数据，不直接碰 py12306 的内部对象。

好处：
- 界面（Qt）与 Web 面板读到的是**同一份指标**（core.metrics），两处显示不会打架；
- 上游对象（Query/User/Job）可能还没初始化或正在被引擎线程改，这里做防御式取值；
- 单元测试可以直接构造 Snapshot，不需要 Qt。

数据来源：
    core.metrics.MetricsStore  —— 查询次数、延迟分位、任务与熔断状态
    core.engine.EngineState    —— 引擎是否在跑、跑了多久
    core.config.Config         —— 账号数、通知适配器等展示信息
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .metrics import MetricsStore, get_store


@dataclass
class TaskRow:
    """表格里的一行（= 一个「任务 × 日期 × 车站」组合）。"""

    task: str
    label: str = ""
    station: str = ""
    travel_date: str = ""
    breaker_state: str = "closed"
    queries: int = 0
    tickets: int = 0
    risk_hits: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    open_count: int = 0
    next_probe_in: float = 0.0
    last_query_ago: Optional[float] = None
    last_latency_ms: float = 0.0
    last_outcome: str = ""
    last_reason: str = ""
    soft_risk_score: float = 0.0

    @property
    def success_rate(self) -> float:
        """有票率（有票+成功）/ 查询次数。"""
        if not self.queries:
            return 0.0
        return (self.tickets / self.queries) * 100.0


@dataclass
class Snapshot:
    """一次刷新拿到的全部展示数据。"""

    ts: float = 0.0
    engine_running: bool = False
    engine_phase: str = "空闲"
    engine_uptime: float = 0.0
    engine_passes: int = 0

    queries_total: int = 0
    queries_in_window: int = 0
    queries_per_minute: float = 0.0
    tickets_total: int = 0
    success_total: int = 0
    risk_control_total: int = 0
    circuit_open_total: int = 0
    open_tasks: int = 0
    task_count: int = 0

    latency_p50: float = 0.0
    latency_p90: float = 0.0
    latency_p99: float = 0.0
    latency_max: float = 0.0

    tasks: List[TaskRow] = field(default_factory=list)
    breaker_events: List[Dict[str, Any]] = field(default_factory=list)
    series: Dict[str, Any] = field(default_factory=dict)
    outcomes: Dict[str, int] = field(default_factory=dict)

    # 配置/环境（只读展示）
    accounts: int = 0
    notify_adapters: List[str] = field(default_factory=list)
    login_states: List[Dict[str, Any]] = field(default_factory=list)
    log_file: str = ""
    metrics_db: str = ""


class Monitor:
    """把 MetricsStore + 引擎状态 + 配置整理成 Snapshot。"""

    def __init__(
        self,
        store: Optional[MetricsStore] = None,
        *,
        engine: Any = None,
        config: Any = None,
        uplink: Any = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._config = config
        self._uplink = uplink

    # -- 依赖注入 ------------------------------------------------------

    def bind_store(self, store: MetricsStore) -> None:
        self._store = store

    def bind_engine(self, engine: Any) -> None:
        self._engine = engine

    def bind_uplink(self, uplink: Any) -> None:
        """上游桥接对象（core.uplink.UpstreamBridge）。"""
        self._uplink = uplink

    @property
    def store(self) -> MetricsStore:
        if self._store is None:
            self._store = get_store()
        return self._store

    # -- 组装 ----------------------------------------------------------

    def snapshot(self, *, buckets: int = 60, bucket_seconds: float = 60.0) -> Snapshot:
        now = time.time()
        store = self.store
        summary = store.summary(now)
        outcomes = dict(summary.get("outcomes") or {})
        lat = dict(summary.get("latency_ms") or {})
        snap = Snapshot(
            ts=now,
            queries_total=int(summary.get("queries_total") or 0),
            queries_in_window=int(summary.get("queries_in_window") or 0),
            queries_per_minute=float(summary.get("queries_per_minute") or 0.0),
            tickets_total=int(outcomes.get("ticket_found", 0)),
            success_total=int(outcomes.get("success", 0)),
            risk_control_total=int(summary.get("risk_control_total") or 0),
            circuit_open_total=int(summary.get("circuit_open_total") or 0),
            open_tasks=int(summary.get("open_tasks") or 0),
            task_count=int(summary.get("task_count") or 0),
            latency_p50=float(lat.get("p50") or 0.0),
            latency_p90=float(lat.get("p90") or 0.0),
            latency_p99=float(lat.get("p99") or 0.0),
            latency_max=float(lat.get("max") or 0.0),
            breaker_events=store.recent_breaker_events(limit=50),
            series=store.series(bucket_seconds=bucket_seconds, buckets=buckets, now=now),
            outcomes=outcomes,
            metrics_db=str(store.db_path) if store.db_path else "",
        )

        for item in store.tasks(now):
            counts = dict(item.get("outcome_counts") or {})
            snap.tasks.append(
                TaskRow(
                    task=str(item.get("task") or ""),
                    label=str(item.get("label") or ""),
                    station=str(item.get("station") or ""),
                    travel_date=str(item.get("travel_date") or ""),
                    breaker_state=str(item.get("breaker_state") or "closed"),
                    queries=int(item.get("query_count") or 0),
                    tickets=counts.get("ticket_found", 0) + counts.get("success", 0),
                    risk_hits=counts.get("risk_control", 0) + counts.get("rate_limit", 0),
                    failures=counts.get("server_error", 0) + counts.get("timeout", 0),
                    consecutive_failures=int(item.get("consecutive_failures") or 0),
                    open_count=int(item.get("open_count") or 0),
                    next_probe_in=float(item.get("next_probe_in") or 0.0),
                    last_query_ago=item.get("last_query_ago"),
                    last_latency_ms=float(item.get("last_duration_ms") or 0.0),
                    last_outcome=str(item.get("last_outcome") or ""),
                    last_reason=str(item.get("last_reason") or ""),
                    soft_risk_score=float(item.get("soft_risk_score") or 0.0),
                )
            )

        self._fill_engine(snap)
        self._fill_environment(snap)
        return snap

    def _fill_engine(self, snap: Snapshot) -> None:
        engine = self._engine
        if engine is None:
            return
        state = getattr(engine, "state", None)
        if state is None:
            return
        snap.engine_running = bool(getattr(state, "running", False))
        snap.engine_phase = str(getattr(state, "phase", "") or "")
        snap.engine_passes = int(getattr(state, "passes", 0) or 0)
        uptime = getattr(state, "uptime", None)
        if callable(uptime):
            snap.engine_uptime = float(uptime())
        elif uptime is not None:
            snap.engine_uptime = float(uptime)

    def _fill_environment(self, snap: Snapshot) -> None:
        uplink = self._uplink
        if uplink is not None:
            info = uplink.describe()
            snap.accounts = int(info.get("accounts") or 0)
            snap.notify_adapters = list(info.get("notify_adapters") or [])
            snap.log_file = str(info.get("log_file") or "")
            snap.login_states = list(info.get("login_states") or [])
            if not snap.task_count:
                snap.task_count = int(info.get("task_count") or 0)

    def purge_login_state(self) -> int:
        """一键清除登录态（界面按钮）。返回删除文件数。"""
        if self._uplink is not None:
            return int(self._uplink.purge_login_state())
        return 0
