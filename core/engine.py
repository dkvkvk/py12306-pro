"""抢票引擎：把上游 py12306 的查询流程跑在后台线程里，并且能被界面安全地启动/停止。

与参考项目（host_app）的取舍一致：
- **不依赖 Redis**：单机单进程跑，集群模式保持关闭；队列/日志的上游 Redis 依赖在
  core/upstream_compat.py 里用本地实现顶替。
- **可停止**：上游的查询循环本身是 while True，这里不用它，改成「自己控制的一趟循环」——
  每趟把所有任务/日期/车站查一遍，趟与趟之间检查停止标志，所以点「停止」最多等一趟结束。
- **纯标准库 + core 内部依赖**，不 import Qt：界面只是它的观察者（通过 core.metrics 读状态）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .metrics import get_store
from .paths import logs_dir

logger = logging.getLogger(__name__)


@dataclass
class EngineState:
    """引擎对外暴露的状态（界面直接显示这个）。"""

    running: bool = False
    stopping: bool = False
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    passes: int = 0
    queries: int = 0            # 本进程内上游报告过的查询次数（尽力统计）
    last_pass_at: Optional[float] = None
    last_error: str = ""
    phase: str = "空闲"          # 给界面显示的当前阶段文案

    @property
    def uptime(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.stopped_at or time.time()
        return max(0.0, end - self.started_at)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "stopping": self.stopping,
            "uptime": round(self.uptime, 1),
            "passes": self.passes,
            "queries": self.queries,
            "last_pass_at": self.last_pass_at,
            "last_error": self.last_error,
            "phase": self.phase,
        }


class EngineError(RuntimeError):
    """启动引擎失败（配置错、账号缺失等）。"""


class TicketEngine:
    """后台抢票引擎。

    用法：
        engine = TicketEngine(on_state=..., on_log=...)
        engine.start()
        ...
        engine.stop()          # 阻塞到线程退出（最多一趟查询的时间）
    """

    def __init__(
        self,
        *,
        on_state: Optional[Callable[[EngineState], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        query_jobs: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.state = EngineState()
        self._on_state = on_state
        self._on_log = on_log
        self._query_jobs = query_jobs
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    # -- 对外接口 ------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop_event.clear()
            self.state = EngineState(running=True, started_at=time.time(), phase="正在初始化")
            self._emit_state()
            self._thread = threading.Thread(target=self._run, name="py12306-engine", daemon=True)
            self._thread.start()

    def stop(self, timeout: Optional[float] = None) -> bool:
        """请求停止；返回是否在超时前真的退出。"""
        with self._lock:
            if not self.is_running:
                self.state.running = False
                self.state.stopping = False
                self._emit_state()
                return True
            self.state.stopping = True
            self.state.phase = "正在停止（等当前这一趟查询结束）"
            self._emit_state()
            self._stop_event.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            still_alive = thread.is_alive()
            if not still_alive:
                self.state.running = False
                self.state.stopping = False
                self.state.stopped_at = time.time()
                self.state.phase = "已停止"
                self._emit_state()
            return not still_alive
        return True

    # -- 内部 ----------------------------------------------------------

    def _log(self, message: str) -> None:
        logger.info(message)
        if self._on_log is not None:
            try:
                self._on_log(message)
            except Exception:  # 界面回调出错不能影响引擎
                pass

    def _emit_state(self) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(self.state)
        except Exception:
            pass

    def _run(self) -> None:
        try:
            self._bootstrap()
        except Exception as exc:
            self.state.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.state.phase = "启动失败"
            self.state.running = False
            self.state.stopped_at = time.time()
            self._log("引擎启动失败：%s" % self.state.last_error)
            self._emit_state()
            return

        from py12306.config import Config
        from py12306.helpers.func import jobs_do
        from py12306.query.query import Query

        query = Query.wait_for_ready()
        self.state.phase = "运行中"
        self._emit_state()

        while not self._stop_event.is_set():
            started = time.time()
            try:
                jobs_do(query.jobs, "run")
            except Exception as exc:
                self.state.last_error = "%s: %s" % (type(exc).__name__, exc)
                self._log("这一趟查询出错：%s" % self.state.last_error)
                # 单趟出错不退出循环，退避一会儿继续（风控熔断由 core.risk 负责）
                if self._stop_event.wait(5.0):
                    break
                continue

            self.state.passes += 1
            self.state.last_pass_at = time.time()
            self._emit_state()

            if self._stop_event.is_set():
                break
            # 生产模式下 Job.run() 内部已经按自适应间隔等待过；这里再留一个最小间隔，
            # 避免「任务为空」时变成死循环空转。
            interval = float(getattr(Config(), "QUERY_INTERVAL", 4) or 4)
            if not query.jobs:
                self.state.phase = "没有可执行的任务（检查 .env 里的 QUERY_JOBS）"
                self._emit_state()
                if self._stop_event.wait(max(5.0, interval)):
                    break
                continue
            elapsed = time.time() - started
            if elapsed < 0.5:
                if self._stop_event.wait(0.5):
                    break

        self.state.running = False
        self.state.stopping = False
        self.state.stopped_at = time.time()
        self.state.phase = "已停止"
        self._log("引擎已停止")
        self._emit_state()

    def _bootstrap(self) -> None:
        """准备上游运行环境（对应上游 main.py 里 App.run/run_check 那一段）。"""
        from py12306.app import App, Const
        from py12306.config import Config
        from py12306.helpers.cdn import Cdn
        from py12306.query.query import Query
        from py12306.user.user import User

        self._log("正在加载配置…")
        App.run()
        Const.IS_TEST = False  # 关键：上游循环里的测试分支必须关掉

        config = Config()
        if self._query_jobs:
            config.QUERY_JOBS = self._query_jobs

        # 日志写文件，面板/界面才能读到实时日志
        config.OUT_PUT_LOG_TO_FILE_ENABLED = 1
        config.OUT_PUT_LOG_TO_FILE_PATH = str(logs_dir() / "12306.log")

        App.run_check()
        self.state.phase = "正在初始化任务"
        self._emit_state()
        Query.check_before_run()
        Cdn.run()
        User.run()
        self._log("任务已初始化，共 %d 个" % len(Query().jobs))
