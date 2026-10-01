"""P0-2 风控熔断 + 退避。

规格书要求：
- 连续 N 次请求异常（5xx / 超时 / 风控响应）-> 退避，退避时间带随机抖动
- 命中风控特征 -> 熔断，停止该任务查询并告警，不要继续撞墙
- 熔断后指数级延长重试间隔（30s -> 5min -> 30min），而不是无脑重试

公开接口（给 py12306 的查询任务用）：
    breaker = RiskBreaker(RiskConfig(), key="task-1", notifier=notify)
    d = breaker.before_query()          # 决策：这次能不能查、要等多久
    if not d.allow: await asyncio.sleep(d.wait_seconds)
    try:
        resp = session.get(QUERY_URL, params=...)
    except requests.RequestException as exc:
        breaker.report_failure(*classify_exception(exc))
    else:
        cat, reason, retry_after = classify_response(resp.status_code, resp.headers, resp.text)
        if cat is None:
            breaker.report_success()
        else:
            breaker.report_failure(cat, reason, retry_after=retry_after)
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from .notifier import Event
from .redaction import redact
from .timing import RandomStream, clamp_delay, exponential_backoff, new_stream

# --- 失败分类 -------------------------------------------------------------


class FailureCategory:
    TRANSPORT = "transport"
    SERVER = "server"
    AUTH = "auth"
    CAPTCHA = "captcha"
    RATE_LIMIT = "rate_limit"
    RISK_CONTROL = "risk_control"


#: 会记入「连续异常」并触发退避的分类
BACKOFF_CATEGORIES = frozenset({FailureCategory.TRANSPORT, FailureCategory.SERVER})
#: 立即熔断的分类
TRIP_CATEGORIES = frozenset({FailureCategory.RATE_LIMIT, FailureCategory.RISK_CONTROL})
#: 不计入熔断计数的分类（登录态过期要靠重新登录解决，撞墙没用）
NEUTRAL_CATEGORIES = frozenset({FailureCategory.AUTH, FailureCategory.CAPTCHA})


class BreakerState:
    CLOSED = "closed"
    BACKOFF = "backoff"
    OPEN = "open"
    HALF_OPEN = "half_open"

    ALL = (CLOSED, BACKOFF, OPEN, HALF_OPEN)


# --- 风控特征识别 ---------------------------------------------------------

#: 12306 在命中风控时的响应特征。默认值来自公开的社区经验，允许用
#: RISK_CONTROL_PATTERNS 环境变量覆盖/追加。
DEFAULT_RISK_PATTERNS: Tuple[str, ...] = (
    "您的访问过于频繁",
    "访问过于频繁",
    "请求过于频繁",
    "操作过于频繁",
    "请使用本人账号登录",
    "抱歉，当前排队人数超过",
    "网络可能存在问题，请您重试一下",
    "验证码校验失败次数过多",
    "非法请求",
    "系统繁忙，请稍后重试",
)

DEFAULT_RISK_HEADERS: Tuple[str, ...] = ("x-risk-control", "x-captcha-challenge")


@dataclass(frozen=True)
class Detection:
    """一次响应的判定结果。category=None 表示这是一次正常响应。"""

    category: Optional[str]
    reason: str = ""
    retry_after: Optional[float] = None

    @property
    def ok(self) -> bool:
        return self.category is None


def classify_response(
    status_code: Optional[int],
    headers: Optional[Dict[str, Any]] = None,
    body: Optional[str] = None,
    patterns: Tuple[str, ...] = DEFAULT_RISK_PATTERNS,
    header_names: Tuple[str, ...] = DEFAULT_RISK_HEADERS,
    max_body_scan: int = 4096,
) -> Detection:
    """判定 HTTP 响应属于哪一类失败。纯函数。"""
    retry_after = None
    if headers:
        lowered = {str(k).lower(): v for k, v in headers.items()}
        raw = lowered.get("retry-after")
        if raw is not None:
            from .timing import parse_retry_after

            retry_after = parse_retry_after(str(raw))
        for name in header_names:
            if name in lowered:
                return Detection(
                    FailureCategory.RISK_CONTROL,
                    f"risk-control header present: {name}",
                    retry_after,
                )

    code = status_code if status_code is not None else 0
    text = (body or "")[:max_body_scan]

    # 明确的限流/拒绝优先
    if code in (403, 429):
        # 403 同时可能是风控，用特征串区分原因描述
        for pat in patterns:
            if pat in text:
                return Detection(FailureCategory.RISK_CONTROL, f"HTTP {code} + {pat!r}", retry_after)
        return Detection(FailureCategory.RATE_LIMIT, f"HTTP {code}", retry_after)

    # 2xx 也可能带风控特征（12306 常见：200 + {"result_message":"..."}）
    if code and 200 <= code < 300:
        for pat in patterns:
            if pat in text:
                return Detection(FailureCategory.RISK_CONTROL, f"HTTP {code} + {pat!r}", retry_after)
        return Detection(None, "", retry_after)

    if code >= 500:
        return Detection(FailureCategory.SERVER, f"HTTP {code}", retry_after)
    if code in (401, 302):
        return Detection(FailureCategory.AUTH, f"HTTP {code}", retry_after)
    if 400 <= code < 500:
        for pat in patterns:
            if pat in text:
                return Detection(FailureCategory.RISK_CONTROL, f"HTTP {code} + {pat!r}", retry_after)
        return Detection(FailureCategory.SERVER, f"HTTP {code}", retry_after)
    return Detection(FailureCategory.TRANSPORT, "no status code", retry_after)


def classify_exception(exc: BaseException) -> Detection:
    """把 requests / urllib3 抛出的异常归类。纯函数（按类名判断，不 import requests）。"""
    name = type(exc).__name__
    mro = [c.__name__ for c in type(exc).__mro__]
    if "Timeout" in name or "Timeout" in "".join(mro):
        return Detection(FailureCategory.TRANSPORT, f"timeout: {name}")
    if "ConnectionError" in mro or "ConnectionError" in name:
        return Detection(FailureCategory.TRANSPORT, f"connection error: {name}")
    if "SSLError" in mro or "SSLError" in name:
        return Detection(FailureCategory.TRANSPORT, f"tls error: {name}")
    if "RequestException" in mro:
        return Detection(FailureCategory.TRANSPORT, f"request error: {name}")
    return Detection(FailureCategory.TRANSPORT, f"unknown error: {name}")


# --- 配置 -----------------------------------------------------------------


def _positive(value: float, name: str) -> float:
    if value <= 0:
        raise ValueError(f"{name} 必须为正数，收到 {value!r}")
    return value


@dataclass
class RiskConfig:
    # 连续异常 -> 退避
    failure_threshold: int = 3
    failure_backoff_base: float = 3.0
    failure_backoff_factor: float = 2.0
    failure_backoff_cap: float = 120.0

    # 熔断 -> 停止查询 + 告警，探针复归
    breaker_base: float = 30.0
    breaker_cap: float = 1800.0        # 30min
    breaker_multiplier: float = 2.0
    require_successes: int = 2
    half_open_probes: int = 1

    # 软风控信号累计窗（存在显式 429/403 时立即熔断，不看这个）
    soft_risk_window: float = 300.0
    soft_risk_threshold: float = 3.0

    # 抖动与上限
    jitter_ratio: float = 0.2
    max_wait_seconds: float = 3600.0

    # 开售前激进窗口（T-60min 起），只影响正常间隔，不影响熔断退避
    pre_sale_window_offset: float = 3600.0
    pre_sale_window: float = 1800.0
    respect_pre_sale_window: bool = True
    #: 熔断后是否同时禁用「开售前激进」——必须为 True，熔断了就不许再激进
    freeze_aggressive_when_open: bool = True

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold 必须 >= 1")
        if self.require_successes < 1:
            raise ValueError("require_successes 必须 >= 1")
        if self.half_open_probes < 1:
            raise ValueError("half_open_probes 必须 >= 1")
        if not 0.0 <= self.jitter_ratio < 1.0:
            raise ValueError("jitter_ratio 需在 [0, 1) 之间")
        for name in (
            "failure_backoff_base",
            "failure_backoff_factor",
            "failure_backoff_cap",
            "breaker_base",
            "breaker_cap",
            "breaker_multiplier",
            "max_wait_seconds",
        ):
            _positive(getattr(self, name), name)
        if self.breaker_cap < self.breaker_base:
            raise ValueError("breaker_cap 不能小于 breaker_base")
        if self.failure_backoff_cap < self.failure_backoff_base:
            raise ValueError("failure_backoff_cap 不能小于 failure_backoff_base")


# --- 决策 -----------------------------------------------------------------


@dataclass
class Decision:
    """before_query() 的结果。"""

    allow: bool
    wait_seconds: float
    state: str
    reason: str
    probe: bool = False

    def __bool__(self) -> bool:  # 方便 if breaker.before_query(): ...
        return self.allow


@dataclass
class BreakerSnapshot:
    key: str
    state: str
    consecutive_failures: int
    consecutive_successes: int
    trip_count: int
    open_count: int
    probe_used: bool
    probe_available: bool
    next_probe_in: float
    soft_risk_score: float
    last_category: Optional[str]
    last_reason: str
    last_result_ago: Optional[float]
    counters: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Clock(Protocol):
    def monotonic(self) -> float: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


# --- 熔断器 ---------------------------------------------------------------

_EVENT_BY_CATEGORY = {
    FailureCategory.RATE_LIMIT: "risk_control",
    FailureCategory.RISK_CONTROL: "risk_control",
    FailureCategory.AUTH: "login_expired",
    FailureCategory.CAPTCHA: "captcha_failed",
}


@dataclass
class BreakerCounters:
    """按分类累计，给 Web 界面 / 指标用。"""

    total: int = 0
    success: int = 0
    transport: int = 0
    server: int = 0
    auth: int = 0
    captcha: int = 0
    rate_limit: int = 0
    risk_control: int = 0

    def bump(self, name: str) -> None:
        setattr(self, name, getattr(self, name, 0) + 1)


class RiskBreaker:
    """单个「任务 × 日期 × 车站」组合的风控熔断器。

    为什么是 per-key 而不是全局：撞墙的往往只是一个组合。全局熔断会误伤其他
    本来正常的组合；而规格书要求「停止该任务查询」。同时 soft_risk 分数按
    key 独立累计，避免一个组合的信号污染另一个。

    线程安全：内部一把锁；py12306 的查询是多线程/多协程混合的。
    """

    def __init__(
        self,
        config: Optional[RiskConfig] = None,
        *,
        key: str = "",
        notifier: Optional[Callable[..., Any]] = None,
        stream: Optional[RandomStream] = None,
        clock: Optional[Clock] = None,
        label: str = "",
    ) -> None:
        self.config = config or RiskConfig()
        self.key = key
        self.label = label or key or "task"
        self.notifier = notifier
        self.clock: Clock = clock or _SystemClock()
        # 带 key 的独立抖动流：不同组合不会同步退避
        self.stream: RandomStream = stream or new_stream(key)
        self.counters = BreakerCounters()

        self._lock = threading.RLock()
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._trip_count = 0
        self._open_count = 0
        self._state_since = self.clock.monotonic()
        self._open_until = 0.0
        self._current_wait = 0.0
        self._probe_used = False
        self._probes_in_flight = 0
        self._soft_risk_score = 0.0
        self._last_soft_risk_ts = 0.0
        self._last_category: Optional[str] = None
        self._last_reason = ""
        self._last_result_ts: Optional[float] = None
        self.state_history: list = []

    # -- 查询生命周期 --------------------------------------------------

    def before_query(self, *, pre_sale: bool = False, now: Optional[float] = None) -> Decision:
        """进入一次查询前调用。返回是否允许立即查询，以及不允许时要等多久。"""
        ts = self.clock.monotonic() if now is None else now
        with self._lock:
            self._decay_soft_risk(ts)

            if self._state == BreakerState.OPEN:
                if ts < self._open_until:
                    return Decision(
                        allow=False,
                        wait_seconds=self._open_until - ts,
                        state=BreakerState.OPEN,
                        reason=f"熔断中：{self._last_reason}",
                    )
                self._transition(BreakerState.HALF_OPEN, ts, "冷却结束，进入探针模式")
                self._probe_used = False

            if self._state == BreakerState.HALF_OPEN:
                if self._probes_in_flight >= self.config.half_open_probes:
                    return Decision(
                        allow=False,
                        wait_seconds=self._current_wait or self.config.breaker_base,
                        state=BreakerState.HALF_OPEN,
                        reason="已有探针在飞",
                    )
                self._probes_in_flight += 1
                self._probe_used = True
                return Decision(allow=True, wait_seconds=0.0, state=BreakerState.HALF_OPEN, reason="探针放行", probe=True)

            if self._state == BreakerState.BACKOFF:
                remaining = self._open_until - ts
                if remaining > 0:
                    return Decision(
                        allow=False,
                        wait_seconds=remaining,
                        state=BreakerState.BACKOFF,
                        reason=f"退避中：{self._last_reason}",
                    )
                self._transition(BreakerState.CLOSED, ts, "退避结束")
                self._consecutive_failures = 0

            return Decision(allow=True, wait_seconds=0.0, state=self._state, reason="正常")

    def report_success(self, *, now: Optional[float] = None) -> None:
        ts = self.clock.monotonic() if now is None else now
        with self._lock:
            self.counters.bump("success")
            self.counters.total += 1
            self._consecutive_failures = 0
            self._last_result_ts = ts
            self._last_category = None
            self._last_reason = ""
            self._decay_soft_risk(ts)

            if self._state == BreakerState.HALF_OPEN:
                self._probes_in_flight = max(0, self._probes_in_flight - 1)
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.config.require_successes:
                    self._trip_count = 0
                    self._current_wait = 0.0
                    self._soft_risk_score = 0.0
                    self._transition(BreakerState.CLOSED, ts, "探针连续成功，熔断解除")
                    self._notify("recovered", "熔断已解除", extra={"key": self.key})
                return

            if self._state == BreakerState.BACKOFF:
                self._transition(BreakerState.CLOSED, ts, "异常后恢复正常")

    def report_failure(
        self,
        category: str,
        reason: str = "",
        *,
        retry_after: Optional[float] = None,
        now: Optional[float] = None,
        explicit: bool = True,
    ) -> Decision:
        """上报一次失败。返回上报后的决策（含新的等待时间）。

        explicit=False 表示是「软信号」（例如验证码触发频率异常升高），
        需要累计到 soft_risk_threshold 才熔断。
        """
        ts = self.clock.monotonic() if now is None else now
        safe_reason = redact(reason)[:200]
        with self._lock:
            self.counters.bump(category)
            self.counters.total += 1
            self._last_result_ts = ts
            self._last_category = category
            self._last_reason = safe_reason

            if category in NEUTRAL_CATEGORIES:
                # 登录态/验证码问题不计入熔断，但要立刻告警，让用户去处理
                self._notify(_EVENT_BY_CATEGORY.get(category, "task_error"), f"{category}: {safe_reason}")
                return self._decision_locked(ts, allow_immediately=True)

            if category in TRIP_CATEGORIES:
                if explicit or self._soft_risk_score + 1.0 >= self.config.soft_risk_threshold:
                    if category == FailureCategory.RATE_LIMIT or explicit:
                        self._open(ts, category, safe_reason, retry_after)
                    else:
                        self._open(ts, category, safe_reason, retry_after)
                else:
                    self._bump_soft_risk(ts)
                    return self._decision_locked(ts, allow_immediately=True)
                return self._decision_locked(ts, allow_immediately=False)

            if not explicit:
                # 软的风控信号：衰减累计
                self._bump_soft_risk(ts)
                if self._soft_risk_score >= self.config.soft_risk_threshold:
                    self._open(ts, FailureCategory.RISK_CONTROL, safe_reason, retry_after)
                    return self._decision_locked(ts, allow_immediately=False)
                return self._decision_locked(ts, allow_immediately=True)

            # 退避类
            self._consecutive_failures += 1
            within_half_open = self._state == BreakerState.HALF_OPEN
            if within_half_open:
                self._probes_in_flight = max(0, self._probes_in_flight - 1)

            if self._consecutive_failures >= self.config.failure_threshold:
                # 退避阶梯按「超出阈值的次数」指数增长；已经在退避中时继续放大，
                # 而不是每次都退回 3s（否则持续异常等于没有退避）。
                steps = self._consecutive_failures - self.config.failure_threshold + 1
                wait = exponential_backoff(
                    steps,
                    self.config.failure_backoff_base,
                    self.config.failure_backoff_factor,
                    self.config.failure_backoff_cap,
                    jitter_ratio=self.config.jitter_ratio,
                    stream=self.stream,
                )
                self._open_until = ts + wait
                self._current_wait = wait
                already_backing_off = self._state == BreakerState.BACKOFF
                self._transition(BreakerState.BACKOFF, ts, f"连续 {self._consecutive_failures} 次异常")
                if not already_backing_off or self._consecutive_failures % self.config.failure_threshold == 0:
                    self._notify(
                        "backoff",
                        f"连续 {self._consecutive_failures} 次异常，退避 {wait:.1f}s（{category}）",
                    )
                return self._decision_locked(ts, allow_immediately=False)

            if within_half_open:
                # 探针失败 -> 立刻重新熔断，等待时间翻倍
                self._open(ts, category, safe_reason, retry_after)
                return self._decision_locked(ts, allow_immediately=False)

            return self._decision_locked(ts, allow_immediately=True)

    # -- 内部 ----------------------------------------------------------

    def _decision_locked(self, ts: float, *, allow_immediately: bool) -> Decision:
        if allow_immediately and self._state not in (BreakerState.OPEN, BreakerState.BACKOFF, BreakerState.HALF_OPEN):
            return Decision(allow=True, wait_seconds=0.0, state=self._state, reason="继续查询")
        remaining = max(0.0, self._open_until - ts)
        return Decision(
            allow=allow_immediately and remaining <= 0,
            wait_seconds=remaining,
            state=self._state,
            reason=self._last_reason or self._state,
        )

    def _candidate_wait(self, ts: float, retry_after: Optional[float]) -> float:
        # _trip_count 在 _open() 里已经被自增，所以这里直接用：第 1 次熔断 = base(30s)
        wait = exponential_backoff(
            self._trip_count,
            self.config.breaker_base,
            self.config.breaker_multiplier,
            self.config.breaker_cap,
            jitter_ratio=self.config.jitter_ratio,
            stream=self.stream,
        )
        if retry_after is not None:
            # 服务端明确说了等多久就听它的（但不超过上限）
            wait = max(wait, retry_after)
        # 下限只防「0 秒空转」，不能取 breaker_base——否则会把上面的抖动全部抹平。
        return clamp_delay(wait, 1.0, self.config.max_wait_seconds)

    def _open(self, ts: float, category: str, reason: str, retry_after: Optional[float]) -> None:
        previous = self._state
        self._tripp_count_increment()
        wait = self._candidate_wait(ts, retry_after)
        self._open_until = ts + wait
        self._current_wait = wait
        self._consecutive_successes = 0
        self._probes_in_flight = 0
        self._transition(BreakerState.OPEN, ts, f"熔断（{category}）")
        if self.freeze_aggressive_when_open():
            pass
        self._notify(
            Event.RISK_CONTROL,
            f"命中风控熔断：{reason or category}；已停止该组合查询 {wait:.0f}s（第 {self._open_count} 次）",
            extra={
                "key": self.key,
                "label": self.label,
                "category": category,
                "wait_seconds": round(wait, 1),
                "open_count": self._open_count,
                "previous_state": previous,
            },
        )

    def _tripp_count_increment(self) -> None:
        self._trip_count += 1
        self._open_count += 1

    def freeze_aggressive_when_open(self) -> bool:
        return self.config.freeze_aggressive_when_open

    def _bump_soft_risk(self, ts: float) -> None:
        """累加一个软信号，并刷新衰减基准时间。"""
        self._decay_soft_risk(ts)
        self._soft_risk_score += 1.0
        self._last_soft_risk_ts = ts

    def _decay_soft_risk(self, ts: float) -> None:
        """软信号衰减：按半衰期指数衰减，避免旧信号永久累积。"""
        if self._soft_risk_score <= 0:
            return
        if not self._last_soft_risk_ts:
            # 首次记录：以当前时刻为基准，不衰减
            self._last_soft_risk_ts = ts
            return
        last = self._last_soft_risk_ts
        elapsed = ts - last
        if elapsed <= 0:
            return
        half_life = max(1.0, self.config.soft_risk_window / 2.0)
        self._soft_risk_score *= 0.5 ** (elapsed / half_life)
        self._last_soft_risk_ts = ts
        if self._soft_risk_score < 0.05:
            self._soft_risk_score = 0.0

    def _transition(self, state: str, ts: float, reason: str) -> None:
        if state == self._state:
            self._state_since = ts
            return
        previous = self._state
        self._state = state
        self._state_since = ts
        if state == BreakerState.CLOSED:
            self._current_wait = 0.0
            self._open_until = 0.0
            self._probe_used = False
            self._probes_in_flight = 0
        if state != BreakerState.HALF_OPEN:
            self._probes_in_flight = 0
        self.state_history.append({"ts": ts, "from": previous, "to": state, "reason": reason})

    def _notify(self, event: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        if self.notifier is None:
            return
        payload = {"key": self.key, "label": self.label, "state": self._state}
        if extra:
            payload.update(extra)
        try:
            self.notifier(event, redact(message), payload)
        except Exception:  # 通知失败绝不能影响主流程
            pass

    # -- 状态 ----------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def is_open(self) -> bool:
        return self.state in (BreakerState.OPEN, BreakerState.BACKOFF)

    def snapshot(self, now: Optional[float] = None) -> BreakerSnapshot:
        ts = self.clock.monotonic() if now is None else now
        with self._lock:
            self._decay_soft_risk(ts)
            last_ago = None if self._last_result_ts is None else round(ts - self._last_result_ts, 3)
            return BreakerSnapshot(
                key=self.key,
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                consecutive_successes=self._consecutive_successes,
                trip_count=self._trip_count,
                open_count=self._open_count,
                probe_used=self._probe_used,
                probe_available=(
                    self._state == BreakerState.HALF_OPEN
                    and self._probes_in_flight < self.config.half_open_probes
                ),
                next_probe_in=max(0.0, self._open_until - ts),
                soft_risk_score=round(self._soft_risk_score, 3),
                last_category=self._last_category,
                last_reason=redact(self._last_reason),
                last_result_ago=last_ago,
                counters={
                    "total": self.counters.total,
                    "success": self.counters.success,
                    "transport": self.counters.transport,
                    "server": self.counters.server,
                    "auth": self.counters.auth,
                    "captcha": self.counters.captcha,
                    "rate_limit": self.counters.rate_limit,
                    "risk_control": self.counters.risk_control,
                },
            )

    def reset(self, reason: str = "manual reset", *, now: Optional[float] = None) -> None:
        ts = self.clock.monotonic() if now is None else now
        with self._lock:
            self._state = BreakerState.CLOSED
            self._consecutive_failures = 0
            self._consecutive_successes = 0
            self._trip_count = 0
            self._soft_risk_score = 0.0
            self._open_until = 0.0
            self._current_wait = 0.0
            self._probes_in_flight = 0
            self._probe_used = False
            self._transition(BreakerState.CLOSED, ts, reason)


class BreakerRegistry:
    """按 key 管理熔断器，并保证「多车站组合」数量有上限（规格书 5.6）。"""

    def __init__(
        self,
        config: Optional[RiskConfig] = None,
        *,
        max_keys: int = 5,
        notifier: Optional[Callable[..., Any]] = None,
    ) -> None:
        if max_keys < 1:
            raise ValueError("max_keys 必须 >= 1")
        self.config = config or RiskConfig()
        self.max_keys = max_keys
        self.notifier = notifier
        self._breakers: Dict[str, RiskBreaker] = {}
        self._lock = threading.RLock()

    def get(self, key: str, *, label: str = "", stream: Optional[RandomStream] = None) -> RiskBreaker:
        with self._lock:
            existing = self._breakers.get(key)
            if existing is not None:
                return existing
            if len(self._breakers) >= self.max_keys:
                raise ValueError(
                    f"车站组合超过上限 {self.max_keys}（当前 {len(self._breakers)}，新增 {key!r}）——"
                    "查询量会爆炸并触发风控，请减少组合数"
                )
            breaker = RiskBreaker(
                self.config, key=key, label=label, notifier=self.notifier, stream=stream
            )
            self._breakers[key] = breaker
            return breaker

    def all_open(self) -> bool:
        with self._lock:
            if not self._breakers:
                return False
            return all(b.is_open() for b in self._breakers.values())

    def snapshots(self, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: b.snapshot(now).as_dict() for k, b in self._breakers.items()}

    def keys(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._breakers)
