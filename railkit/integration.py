"""把 railkit 的风控熔断 / 自适应抖动接进上游 py12306 的查询循环。

为什么用「打补丁」而不是改 job.py：
- 上游 query/job.py 里还混着下单、乘客校验、CDN 开关等逻辑，直接改容易伤到业务；
- 补丁点集中在 3 个方法上（safe_stay / get_results / handle_response），可读、可测、可撤。

接入的三个点：
1. Job.safe_stay()   —— 换成「熔断决策 + 逐组合抖动间隔」，熔断中直接不发请求
2. Job.get_results() —— 无论请求成功还是被 Request 层吞成空响应，都记一次指标
3. Job.handle_response() —— 拿到余票结果后记「有票 / 无票」并让熔断器学习

另外提供：
- record_login_expired() / record_captcha_failed()，供 User / 打码模块调用
- sync_breakers_to_metrics()，把熔断器快照灌进指标库（面板读这个）
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from .metrics import MetricsStore, get_store
from .notifier import Event, NotifyHub, make_notifier
from .risk import (
    BreakerRegistry,
    FailureCategory,
    RiskBreaker,
    RiskConfig,
    classify_response,
)
from .timing import QueryTimingConfig, new_stream, next_query_delay
from .redaction import redact

logger = logging.getLogger(__name__)


class IntegrationConfig:
    """集成层自己的配置（与 railkit.config.Config 解耦，便于单测）。"""

    def __init__(
        self,
        risk: Optional[RiskConfig] = None,
        timing: Optional[QueryTimingConfig] = None,
        *,
        query_interval: float = 4.0,
        max_station_pairs: int = 5,
        pre_sale_window_minutes: int = 60,
        risk_patterns: Optional[tuple] = None,
    ) -> None:
        self.risk = risk or RiskConfig()
        self.timing = timing or QueryTimingConfig(
            normal_base_seconds=max(1.0, query_interval),
            window_base_seconds=max(1.0, query_interval) + 1.0,
        )
        self.query_interval = query_interval
        self.max_station_pairs = max_station_pairs
        self.pre_sale_window_minutes = pre_sale_window_minutes
        self.risk_patterns = risk_patterns


class JobRiskAdapter:
    """把「一个 Job 的一次查询」翻译成熔断器的输入输出。"""

    def __init__(
        self,
        job: Any,
        *,
        registry: BreakerRegistry,
        store: MetricsStore,
        config: IntegrationConfig,
        notify: Optional[Callable[..., None]] = None,
    ) -> None:
        self.job = job
        self.registry = registry
        self.store = store
        self.config = config
        self.notify = notify
        self.key = self._build_key(job)
        # 抖动流按「任务 × 日期 × 车站」派生并长期持有：
        # 每次重建流会让 random 永远返回同一个首值，看起来就像没有抖动。
        self._stream = new_stream(self.key)
        self.breaker: RiskBreaker = registry.get(self.key, label=self._label(job))
        self.last_decision: Any = None
        self.last_outcome: str = ""

    # -- 组装 ----------------------------------------------------------

    @staticmethod
    def _build_key(job: Any) -> str:
        name = getattr(job, "job_name", None) or "job"
        station = "%s-%s" % (
            getattr(job, "left_station_code", "") or getattr(job, "left_station", ""),
            getattr(job, "arrive_station_code", "") or getattr(job, "arrive_station", ""),
        )
        date = getattr(job, "left_date", "") or ""
        # 任务 × 日期 × 车站组合：每个组合独立抖动、独立熔断
        return "%s|%s|%s" % (name, date, station)

    @staticmethod
    def _label(job: Any) -> str:
        return "%s %s->%s" % (
            getattr(job, "job_name", ""),
            getattr(job, "left_station", ""),
            getattr(job, "arrive_station", ""),
        )

    def rebind(self) -> None:
        """车站/日期切换后 key 会变，需要重新取熔断器。"""
        key = self._build_key(self.job)
        if key != self.key:
            self.key = key
            self._stream = new_stream(key)
            self.breaker = self.registry.get(key, label=self._label(self.job))

    # -- 决策 ----------------------------------------------------------

    def is_pre_sale(self, now: Optional[float] = None) -> bool:
        """是否处于「即将开售」的激进窗口。

        12306 每天 8:00-18:00 整点/半点放票，这里按「距下一个整点不足 N 分钟」判断。
        """
        window = max(0, self.config.pre_sale_window_minutes)
        if window == 0:
            return False
        current = time.localtime(now if now is not None else time.time())
        minutes = current.tm_min
        minutes_to_hour = 60 - minutes
        return 0 <= minutes_to_hour <= window

    def next_delay(self) -> float:
        """返回下一次查询前应等待的秒数（熔断中返回熔断剩余时间）。"""
        self.rebind()
        decision = self.breaker.before_query(pre_sale=self.is_pre_sale())
        self.last_decision = decision
        if not decision.allow:
            self.store.record_query(
                self.key,
                "circuit_open",
                0.0,
                station=self._station(),
                travel_date=str(getattr(self.job, "left_date", "") or ""),
                label=self._label(self.job),
            )
            return max(0.5, min(decision.wait_seconds, self.config.risk.max_wait_seconds))
        delay = next_query_delay(
            self.config.timing,
            pre_sale=self.is_pre_sale(),
            stream=self._stream,
            override=self.config.query_interval,
        )
        return max(0.5, delay)

    # -- 结果上报 ------------------------------------------------------

    def _station(self) -> str:
        return "%s-%s" % (getattr(self.job, "left_station", ""), getattr(self.job, "arrive_station", ""))

    def _record(self, outcome: str, duration_ms: float = 0.0) -> None:
        self.last_outcome = outcome
        self.store.record_query(
            self.key,
            outcome,
            duration_ms,
            station=self._station(),
            travel_date=str(getattr(self.job, "left_date", "") or ""),
            label=self._label(self.job),
        )
    def publish_state(self) -> None:
        """把熔断器快照同步进指标库。

        必须在上报之后调用：否则面板上的「连续失败次数 / 下次探针 / 熔断次数」
        会永远慢一拍甚至停在零。
        """
        try:
            snapshot = self.breaker.snapshot()
            payload = snapshot.as_dict()
            payload["label"] = self._label(self.job)
            self.store.sync_breakers({self.key: payload})
        except Exception:
            logger.debug("同步熔断器快照失败", exc_info=True)

    def on_response(self, response: Any, duration_ms: float = 0.0) -> str:
        """收到响应（含被 Request 层吞掉的错误响应）。返回判定出的 outcome。"""
        status = getattr(response, "status_code", 0) or 0
        reason = str(getattr(response, "reason", "") or "")
        body = ""
        try:
            body = response.text or ""
        except Exception:
            body = ""

        if status == 0:
            # Request.request() 吞掉异常后返回的空响应
            self._record("timeout", duration_ms)
            self._report(FailureCategory.TRANSPORT, reason or "empty response")
            return "timeout"

        detection = classify_response(status, dict(getattr(response, "headers", {}) or {}), body)
        if detection.category == FailureCategory.RISK_CONTROL:
            self._record("risk_control", duration_ms)
            self._report(FailureCategory.RISK_CONTROL, detection.reason, detection.retry_after)
            return "risk_control"
        if detection.category == FailureCategory.RATE_LIMIT:
            self._record("rate_limit", duration_ms)
            self._report(FailureCategory.RATE_LIMIT, detection.reason, detection.retry_after)
            return "rate_limit"
        if detection.category == FailureCategory.SERVER:
            self._record("server_error", duration_ms)
            self._report(FailureCategory.SERVER, detection.reason, detection.retry_after)
            return "server_error"
        if detection.category == FailureCategory.AUTH:
            self._record("auth_error", duration_ms)
            self._report(FailureCategory.AUTH, detection.reason, detection.retry_after)
            return "auth_error"

        # 200 且无风控特征：正常一轮，但「有没有票」由 handle_response 判定
        self._record("no_ticket", duration_ms)
        self.breaker.report_success()
        self.publish_state()
        return "no_ticket"

    def on_ticket_found(self, train_number: str = "") -> None:
        self._record("ticket_found")
        self._notify(Event.TICKET_SUCCESS, "查到余票：%s %s" % (self._label(self.job), train_number))

    def on_order_success(self) -> None:
        self._record("success")
        self._notify(Event.TICKET_SUCCESS, "下单成功：%s" % self._label(self.job))

    def _report(self, category: str, reason: str, retry_after: Optional[float] = None) -> None:
        try:
            self.breaker.report_failure(category, redact(reason), retry_after=retry_after)
        except Exception:  # 熔断器内部有锁和告警，绝不能把主循环带崩
            logger.exception("熔断器上报失败")
        finally:
            # 上报之后再发布，保证面板看到的是「上报完成后」的熔断器状态
            self.publish_state()

    def _notify(self, event: str, message: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(event, message, {"key": self.key, "label": self._label(self.job)})
        except Exception:
            pass


class QueryLoopIntegration:
    """全局集成状态：注册表、指标库、通知、每个 Job 的适配器。"""

    def __init__(
        self,
        *,
        config: Optional[IntegrationConfig] = None,
        registry: Optional[BreakerRegistry] = None,
        store: Optional[MetricsStore] = None,
        hub: Optional[NotifyHub] = None,
    ) -> None:
        self.config = config or IntegrationConfig()
        self.store = store or get_store()
        self.hub = hub or NotifyHub.from_env()
        self.registry = registry or BreakerRegistry(
            self.config.risk,
            max_keys=self.config.max_station_pairs,
            notifier=self._on_breaker_event,
        )
        self._adapters: Dict[int, JobRiskAdapter] = {}
        self._lock = threading.RLock()
        self.enabled = True
        self.patched = False

    # -- 适配器生命周期 --------------------------------------------------

    def adapter_for(self, job: Any) -> JobRiskAdapter:
        with self._lock:
            key = id(job)
            adapter = self._adapters.get(key)
            if adapter is None:
                adapter = JobRiskAdapter(
                    job,
                    registry=self.registry,
                    store=self.store,
                    config=self.config,
                    notify=make_notifier(self.hub),
                )
                self._adapters[key] = adapter
            else:
                adapter.rebind()
            return adapter

    def forget(self, job: Any) -> None:
        with self._lock:
            self._adapters.pop(id(job), None)

    # -- 熔断器事件 -> 指标 + 告警 ---------------------------------------

    def _on_breaker_event(self, event: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = dict(extra or {})
        task = str(payload.get("key") or payload.get("label") or "unknown")
        self.store.record_breaker(
            task,
            str(payload.get("category") or event),
            previous_state=str(payload.get("previous_state") or ""),
            state=str(payload.get("state") or ""),
            wait_seconds=float(payload.get("wait_seconds") or 0.0),
            reason=redact(message),
            label=str(payload.get("label") or ""),
        )
        # 归一化后交给通知层（RISK_CONTROL 会走 high 级告警）
        self.hub.notify(event, message, payload)

    # -- 查询循环回调 ----------------------------------------------------

    def before_query(self, job: Any) -> float:
        if not self.enabled:
            return 0.0
        return self.adapter_for(job).next_delay()

    def after_response(self, job: Any, response: Any, duration_ms: float = 0.0) -> str:
        if not self.enabled:
            return ""
        return self.adapter_for(job).on_response(response, duration_ms)

    def on_ticket_found(self, job: Any, train_number: str = "") -> None:
        if self.enabled:
            self.adapter_for(job).on_ticket_found(train_number)

    def on_order_success(self, job: Any) -> None:
        if self.enabled:
            self.adapter_for(job).on_order_success()

    def record_login_expired(self, who: str = "") -> None:
        self.hub.notify(Event.LOGIN_EXPIRED, "登录态失效%s" % (": " + who if who else ""))

    def record_captcha_failed(self, who: str = "", reason: str = "") -> None:
        self.hub.notify(Event.CAPTCHA_FAILED, "验证码识别失败 %s %s" % (who, redact(reason)))

    # -- 打补丁 ----------------------------------------------------------

    def patch(self) -> bool:
        """给上游 Job 打补丁。幂等；返回是否处于已打补丁状态。"""
        if self.patched:
            return True
        try:
            from py12306.query.job import Job
        except Exception as exc:
            logger.warning("找不到 py12306.query.job，跳过查询循环接入：%s", exc)
            return False

        integration = self
        original_safe_stay = Job.safe_stay
        original_get_results = Job.get_results
        original_handle_response = Job.handle_response

        def patched_safe_stay(job_self, *args, **kwargs):
            try:
                delay = integration.before_query(job_self)
            except Exception:
                logger.exception("熔断决策失败，退回上游间隔")
                return original_safe_stay(job_self, *args, **kwargs)

            # 被熔断拦下：不查，直接等（并记一条 circuit_open）
            adapter = integration.adapter_for(job_self)
            decision = adapter.last_decision
            if decision is not None and not decision.allow:
                from py12306.log.query_log import QueryLog

                QueryLog.add_stay_log("熔断中 %.1fs（%s）" % (delay, decision.reason))
                time.sleep(delay)
                return None

            from py12306.log.query_log import QueryLog

            QueryLog.add_stay_log("自适应 %.2fs（%s）" % (delay, getattr(decision, "state", "closed")))
            time.sleep(delay)
            return None

        def patched_get_results(job_self, response, *args, **kwargs):
            started = time.perf_counter()
            result = original_get_results(job_self, response, *args, **kwargs)
            duration_ms = (time.perf_counter() - started) * 1000.0
            try:
                integration.after_response(job_self, response, duration_ms)
            except Exception:
                logger.exception("记录查询指标失败")
            return result

        def patched_handle_response(job_self, response, *args, **kwargs):
            try:
                results = job_self.get_results(response)
            except Exception:
                results = None
            if results:
                # 有返回就不换接口，直接走上游逻辑
                for result in results:
                    try:
                        job_self.ticket_info = result.split("|")
                        if getattr(job_self, "INDEX_TICKET_NUM", 11) < len(job_self.ticket_info):
                            if job_self.ticket_info[job_self.INDEX_TICKET_NUM] == "Y" and \
                                    job_self.ticket_info[job_self.INDEX_ORDER_TEXT] == "预订":
                                integration.on_ticket_found(job_self, job_self.ticket_info[job_self.INDEX_TRAIN_NUMBER])
                                break
                    except Exception:
                        continue
            return original_handle_response(job_self, response, *args, **kwargs)

        Job.safe_stay = patched_safe_stay
        Job.get_results = patched_get_results
        Job.handle_response = patched_handle_response
        self.patched = True
        logger.info("已接入 railkit 风控熔断与自适应抖动（safe_stay/get_results/handle_response）")
        return True


_INTEGRATION: Optional[QueryLoopIntegration] = None


def get_integration() -> Optional[QueryLoopIntegration]:
    return _INTEGRATION


def set_integration(integration: Optional[QueryLoopIntegration]) -> None:
    global _INTEGRATION
    _INTEGRATION = integration


def install(
    *,
    config: Optional[IntegrationConfig] = None,
    store: Optional[MetricsStore] = None,
    hub: Optional[NotifyHub] = None,
    patch: bool = True,
) -> QueryLoopIntegration:
    """入口一次性调用：建集成、打补丁、挂指标库。"""
    integration = QueryLoopIntegration(config=config, store=store, hub=hub)
    if patch:
        integration.patch()
    set_integration(integration)
    return integration
