"""候补购票模式（spec 第 6 条）：走 12306 官方候补渠道，比脚本硬抢更合规、成功率往往更高。

设计原则
--------
1. **端点集中、可覆盖**：12306 的候补接口未公开文档，且路径会变。这里把端点放在
   WaitlistEndpoints 里，允许用环境变量覆盖，不把「猜的路径」散落在业务代码里。
   >>> 首次使用前请对着自己的抓包核对 WAITLIST_ENDPOINTS（见 README「候补模式」）。<<<
2. **后端可注入**：WaitlistBackend 是协议，HttpBackend 走真实请求，SimulatedBackend 用于
   离线端到端验证（CI 里跑的就是它）。跑不通网络也能验证状态机、退避、通知、面板展示。
3. **状态机显式**：候补不是「提交完就完事」，要跟踪 待提交 -> 排队中 -> 已兑现 / 已失效 / 已取消，
   并且只在「状态真的变化」时告警，避免每分钟一条噪音。
4. **退避**：候补查询不需要高频。默认 90s 起步、指数放大到 30min 封顶，带抖动。

合规提醒：候补是官方功能，但仍属自动化操作，风险与责任由使用者承担（见 README 风险声明）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from .notifier import Event, NotifyHub, make_notifier
from .redaction import redact
from .timing import RandomStream, exponential_backoff, new_stream

logger = logging.getLogger(__name__)


class WaitlistState:
    """候补订单状态（与 12306 返回的语义对齐，名字保持稳定便于面板展示）。"""

    PENDING_SUBMIT = "pending_submit"   # 本地已构造，尚未提交
    QUEUED = "queued"                   # 已提交，正在排队候补
    FULFILLED = "fulfilled"             # 候补成功（已兑现，等待支付/已支付）
    FAILED = "failed"                   # 候补失败（未兑现）
    CANCELED = "canceled"               # 已被取消
    EXPIRED = "expired"                 # 超时失效
    ERROR = "error"                     # 提交/查询出错（可重试）

    ALL = (PENDING_SUBMIT, QUEUED, FULFILLED, FAILED, CANCELED, EXPIRED, ERROR)
    #: 终态：不再需要轮询
    TERMINAL = (FULFILLED, FAILED, CANCELED, EXPIRED)
    #: 需要提醒用户处理的终态
    NOTIFY_TERMINAL = (FULFILLED, FAILED, EXPIRED)


#: 12306 候补相关端点。
#: 这些路径**未经作者在本版本实测**（无法在未登录状态下核实），请以你自己的抓包为准，
#: 用环境变量覆盖：
#:   WAITLIST_SUBMIT_URL / WAITLIST_QUERY_URL / WAITLIST_CANCEL_URL
DEFAULT_ENDPOINTS: Dict[str, str] = {
    "submit": "https://kyfw.12306.cn/otn/confirmPassenger/confirmHB",
    "query": "https://kyfw.12306.cn/otn/queryOrder/queryMyOrderNoComplete",
    "cancel": "https://kyfw.12306.cn/otn/confirmPassenger/cancelNoCompleteMyOrder",
}


@dataclass
class WaitlistEndpoints:
    submit: str = DEFAULT_ENDPOINTS["submit"]
    query: str = DEFAULT_ENDPOINTS["query"]
    cancel: str = DEFAULT_ENDPOINTS["cancel"]

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "WaitlistEndpoints":
        import os

        env = dict(os.environ if env is None else env)
        return cls(
            submit=env.get("WAITLIST_SUBMIT_URL") or cls.submit,
            query=env.get("WAITLIST_QUERY_URL") or cls.query,
            cancel=env.get("WAITLIST_CANCEL_URL") or cls.cancel,
        )


@dataclass
class WaitlistRequest:
    """一条候补申请。字段与 12306 下单表单的语义对应，但保持可读。"""

    left_date: str
    left_station: str          # 站名或站码
    arrive_station: str
    train_numbers: List[str] = field(default_factory=list)
    seat_types: List[str] = field(default_factory=list)
    passengers: List[Dict[str, str]] = field(default_factory=list)
    purpose_codes: str = "ADULT"
    #: 是否接受无座
    accept_no_seat: bool = True
    #: 是否接受相邻座位拆分（12306 有对应勾选）
    accept_adjacent: bool = False
    #: 候补截止时间（12306 要求填写，默认开车前 2 小时）
    deadline_minutes_before_departure: int = 120
    extra: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return "|".join(
            [
                self.left_date,
                "%s-%s" % (self.left_station, self.arrive_station),
                ",".join(sorted(self.train_numbers)),
                ",".join(sorted(self.seat_types)),
                ",".join(sorted(p.get("passenger_name", "") for p in self.passengers)),
            ]
        )

    def to_form(self, *, session_cookies: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """把申请转成提交表单。

        这里刻意保持「一层薄转换」：字段名直接来自 12306 表单（train_date / fromStation 等），
        避免中间再发明一套命名。真正的字段校验交给真实接口返回的错误信息。
        """
        seat_type = self.seat_types[0] if self.seat_types else "O"
        ticket_parts: List[str] = []
        old_parts: List[str] = []
        for passenger in self.passengers:
            name = str(passenger.get("passenger_name", ""))
            id_no = str(passenger.get("passenger_id_no", ""))
            id_type = str(passenger.get("passenger_id_type_code", "1"))
            passenger_type = str(passenger.get("passenger_type", "1"))
            # 12306 的 passengerTicketStr 形如: 席别,票种,姓名,证件号,证件类型,手机号
            ticket_parts.append(",".join([seat_type, "0", passenger_type, name, id_no, id_type]))
            old_parts.append(",".join([name, id_type, id_no, passenger_type]) + "_")

        form: Dict[str, Any] = {
            "train_date": self.left_date,
            "from_station_name": self.left_station,
            "to_station_name": self.arrive_station,
            "train_no": ",".join(self.train_numbers),
            "seatType": ",".join(self.seat_types),
            "passengerTicketStr": "_".join(ticket_parts),
            "oldPassengerStr": "".join(old_parts),
            "purpose_codes": self.purpose_codes,
            "acceptNoSeat": "1" if self.accept_no_seat else "0",
            "isAdjacent": "1" if self.accept_adjacent else "0",
            "hbDeadlineMinutes": str(self.deadline_minutes_before_departure),
        }
        form.update(self.extra)
        return form


@dataclass
class WaitlistStatus:
    state: str
    order_id: str = ""
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0
    #: 排队位次（若接口提供）
    queue_position: Optional[int] = None
    #: 兑现截止时间（时间戳，若接口提供）
    deadline_ts: Optional[float] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in WaitlistState.TERMINAL

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "order_id": self.order_id,
            "message": redact(self.message),
            "queue_position": self.queue_position,
            "deadline_ts": self.deadline_ts,
            "updated_at": self.updated_at,
        }


class WaitlistBackend(Protocol):
    """候补后端协议：真实 HTTP 与离线模拟都实现它。"""

    def submit(self, request: WaitlistRequest) -> WaitlistStatus: ...

    def query(self, order_id: str) -> WaitlistStatus: ...

    def cancel(self, order_id: str) -> WaitlistStatus: ...


# --- 结果解析 -------------------------------------------------------------

#: 12306 返回里常见的「成功」标志
_SUCCESS_FLAGS = ("status", "success", "isSuccess")
#: 语义关键字 -> 本地状态（按顺序匹配，先命中先返回）
_STATE_KEYWORDS: Sequence[tuple] = (
    ("兑现成功", WaitlistState.FULFILLED),
    ("已兑现", WaitlistState.FULFILLED),
    ("候补成功", WaitlistState.FULFILLED),
    ("待支付", WaitlistState.FULFILLED),
    ("兑现失败", WaitlistState.FAILED),
    ("候补失败", WaitlistState.FAILED),
    ("已失效", WaitlistState.EXPIRED),
    ("已取消", WaitlistState.CANCELED),
    ("排队", WaitlistState.QUEUED),
    ("候补中", WaitlistState.QUEUED),
    ("等待", WaitlistState.QUEUED),
)


def parse_status(payload: Any, *, order_id: str = "", now: Optional[float] = None) -> WaitlistStatus:
    """把 12306 的响应体翻译成本地状态。纯函数，便于单测。

    解析策略（从保守到激进）：
    1. 显式状态字段（data.hbStatus / data.status）先映射；
    2. 否则扫文本关键字；
    3. 都识别不出来 -> ERROR（宁可让用户去看原文，也不要猜成成功）。
    """
    ts = time.time() if now is None else now
    raw = payload if isinstance(payload, dict) else {}
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    # 订单号 / 排队位次可能出现在任何一层，先统一取出来（识别出状态后就直接返回，
    # 所以必须在这里解析，不能等到最后）
    resolved_order_id = order_id or str(data.get("orderId") or raw.get("orderId") or "")
    resolved_position = _as_int(data.get("queuePosition") or data.get("position")
                                or raw.get("queuePosition") or raw.get("position"))

    explicit = None
    for container in (data, raw):
        for key in ("hbStatus", "waitListStatus", "status", "orderStatus"):
            value = container.get(key)
            if isinstance(value, str) and value:
                explicit = value
                break
        if explicit:
            break

    mapping = {
        "0": WaitlistState.QUEUED,
        "1": WaitlistState.QUEUED,
        "2": WaitlistState.QUEUED,
        "5": WaitlistState.FULFILLED,
        "6": WaitlistState.FAILED,
        "7": WaitlistState.EXPIRED,
        "8": WaitlistState.CANCELED,
    }
    haystack = " ".join(filter(None, [explicit or "", text]))
    for keyword, state in _STATE_KEYWORDS:
        if keyword in haystack:
            return WaitlistStatus(
                state=state,
                order_id=resolved_order_id,
                message=explicit or keyword,
                raw=raw if isinstance(raw, dict) else {"raw_text": str(payload)[:500]},
                updated_at=ts,
                queue_position=resolved_position,
            )
    if explicit and explicit in mapping:
        return WaitlistStatus(
            state=mapping[explicit],
            order_id=resolved_order_id,
            message=explicit,
            raw=raw if isinstance(raw, dict) else {},
            updated_at=ts,
            queue_position=resolved_position,
        )
    if raw.get("status") is True or raw.get("success") is True:
        return WaitlistStatus(WaitlistState.QUEUED, resolved_order_id, "ok", raw, ts)
    return WaitlistStatus(
        WaitlistState.ERROR, resolved_order_id, "无法识别的候补状态：" + redact(text[:200]), raw, ts
    )


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- 真实 HTTP 后端 -------------------------------------------------------


class HttpWaitlistBackend:
    """用 py12306 已有的 session（requests-html）发请求。

    只依赖「有 .get/.post 且返回带 status_code/text/json() 的对象」，所以测试里能塞假 session。
    """

    def __init__(
        self,
        session: Any,
        endpoints: Optional[WaitlistEndpoints] = None,
        *,
        timeout: float = 10.0,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.session = session
        self.endpoints = endpoints or WaitlistEndpoints.from_env()
        self.timeout = timeout
        self.extra_headers = extra_headers or {}

    def _payload(self, response: Any) -> Any:
        try:
            data = response.json()
            return data if isinstance(data, (dict, list)) else {"raw": data}
        except Exception:
            try:
                return json.loads(getattr(response, "text", "") or "{}")
            except Exception:
                return {"raw_text": (getattr(response, "text", "") or "")[:500]}

    def submit(self, request: WaitlistRequest) -> WaitlistStatus:
        try:
            response = self.session.post(
                self.endpoints.submit,
                data=request.to_form(),
                headers=self.extra_headers,
                timeout=self.timeout,
            )
        except Exception as exc:
            return WaitlistStatus(WaitlistState.ERROR, message="提交候补失败：%s" % type(exc).__name__, updated_at=time.time())
        status = parse_status(self._payload(response), now=time.time())
        if status.state == WaitlistState.QUEUED:
            status.message = status.message or "已提交候补"
        return status

    def query(self, order_id: str = "") -> WaitlistStatus:
        try:
            response = self.session.get(
                self.endpoints.query, params={"orderId": order_id} if order_id else None,
                headers=self.extra_headers, timeout=self.timeout,
            )
        except Exception as exc:
            return WaitlistStatus(WaitlistState.ERROR, order_id=order_id, message="查询候补失败：%s" % type(exc).__name__, updated_at=time.time())
        return parse_status(self._payload(response), order_id=order_id, now=time.time())

    def cancel(self, order_id: str) -> WaitlistStatus:
        try:
            response = self.session.post(
                self.endpoints.cancel, data={"orderId": order_id},
                headers=self.extra_headers, timeout=self.timeout,
            )
        except Exception as exc:
            return WaitlistStatus(WaitlistState.ERROR, order_id=order_id, message="取消候补失败：%s" % type(exc).__name__, updated_at=time.time())
        status = parse_status(self._payload(response), order_id=order_id, now=time.time())
        if status.state not in (WaitlistState.ERROR, WaitlistState.CANCELED):
            status.state = WaitlistState.CANCELED
        return status


# --- 离线模拟后端 ---------------------------------------------------------


class SimulatedBackend:
    """离线后端：按预设剧本推进状态，用于 CI 与本地验证整条链路。

    script 形如 ['queued', 'queued', 'fulfilled']：每次 query 取下一个状态，
    用完就停在最后一个。submit 固定返回 queued。
    """

    def __init__(
        self,
        script: Optional[Sequence[str]] = None,
        *,
        submit_state: str = WaitlistState.QUEUED,
        order_id: str = "SIM-ORDER-1",
        raise_on_submit: bool = False,
    ) -> None:
        self.script = list(script or [WaitlistState.QUEUED, WaitlistState.FULFILLED])
        self.submit_state = submit_state
        self.order_id = order_id
        self.raise_on_submit = raise_on_submit
        self.submit_calls = 0
        self.query_calls = 0
        self.cancel_calls = 0

    def submit(self, request: WaitlistRequest) -> WaitlistStatus:
        self.submit_calls += 1
        if self.raise_on_submit:
            raise RuntimeError("模拟提交失败")
        return WaitlistStatus(
            state=self.submit_state,
            order_id=self.order_id,
            message="模拟提交",
            updated_at=time.time(),
            raw={"simulated": True, "form_keys": sorted(request.to_form().keys())},
        )

    def query(self, order_id: str = "") -> WaitlistStatus:
        index = min(self.query_calls, len(self.script) - 1)
        self.query_calls += 1
        state = self.script[index] if self.script else WaitlistState.QUEUED
        return WaitlistStatus(
            state=state,
            order_id=order_id or self.order_id,
            message="模拟查询 #%d" % self.query_calls,
            updated_at=time.time(),
            queue_position=max(1, 100 - self.query_calls),
        )

    def cancel(self, order_id: str) -> WaitlistStatus:
        self.cancel_calls += 1
        return WaitlistStatus(WaitlistState.CANCELED, order_id=order_id or self.order_id, message="模拟取消", updated_at=time.time())


# --- 运行器 ---------------------------------------------------------------


@dataclass
class WaitlistRunnerConfig:
    #: 首次查询等待
    first_check_seconds: float = 90.0
    #: 退避倍率与上限
    backoff_factor: float = 1.6
    backoff_cap: float = 1800.0
    jitter_ratio: float = 0.2
    #: 最多轮询次数（防止无人值守时无限跑）
    max_checks: int = 200
    #: 提交失败的重试次数
    submit_retries: int = 2


class WaitlistRunner:
    """驱动一条候补申请：提交 -> 轮询 -> 终态告警。

    只做「状态机 + 退避 + 通知」，不做线程调度：调用方决定怎么驱动
    （同步循环、线程、还是事件循环），这样测试里可以完全确定性地推进。
    """

    def __init__(
        self,
        backend: WaitlistBackend,
        request: WaitlistRequest,
        *,
        hub: Optional[NotifyHub] = None,
        config: Optional[WaitlistRunnerConfig] = None,
        stream: Optional[RandomStream] = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.backend = backend
        self.request = request
        self.hub = hub or NotifyHub.from_env()
        self.config = config or WaitlistRunnerConfig()
        self._notify = make_notifier(self.hub)
        self._stream = stream or new_stream("waitlist:" + request.key())
        self._clock = clock
        # 可注入的等待函数：内部重试也要能被测试替换，否则单测会真的 sleep
        self._sleeper = sleeper or time.sleep
        self.status = WaitlistStatus(WaitlistState.PENDING_SUBMIT, updated_at=time.time())
        self.history: List[WaitlistStatus] = []
        self.checks = 0

    # -- 提交 ----------------------------------------------------------

    def submit(self) -> WaitlistStatus:
        last: Optional[WaitlistStatus] = None
        for attempt in range(1, self.config.submit_retries + 2):
            try:
                last = self.backend.submit(self.request)
            except Exception as exc:
                last = WaitlistStatus(WaitlistState.ERROR, message="%s: %s" % (type(exc).__name__, redact(str(exc))), updated_at=time.time())
            if last.state != WaitlistState.ERROR:
                break
            self._notify(Event.TASK_ERROR, "候补提交失败（第 %d 次）：%s" % (attempt, last.message), {"key": self.request.key()})
            if attempt <= self.config.submit_retries:
                self._sleep(self._backoff(attempt))
        self._transition(last or WaitlistStatus(WaitlistState.ERROR, updated_at=time.time()))
        if self.status.state == WaitlistState.QUEUED:
            self._notify(
                Event.SYSTEM,
                "已提交候补：%s %s->%s %s" % (
                    self.request.left_date, self.request.left_station, self.request.arrive_station,
                    ",".join(self.request.train_numbers) or "(多车次)",
                ),
                {"order_id": self.status.order_id, "key": self.request.key()},
            )
        return self.status

    # -- 轮询 ----------------------------------------------------------

    def next_interval(self) -> float:
        """下一次查询前等待多久（指数退避 + 抖动）。

        候补结果不会秒出，高频查询只有坏处；同时也不能太慢，否则错过兑现通知。
        """
        base = self.config.first_check_seconds
        attempt = max(1, self.checks)
        return exponential_backoff(
            attempt,
            base,
            self.config.backoff_factor,
            self.config.backoff_cap,
            jitter_ratio=self.config.jitter_ratio,
            stream=self._stream,
        )

    def check_once(self) -> WaitlistStatus:
        """查一次状态并返回。状态变化时才告警。"""
        if self.status.is_terminal:
            return self.status
        try:
            status = self.backend.query(self.status.order_id)
        except Exception as exc:
            status = WaitlistStatus(
                WaitlistState.ERROR,
                order_id=self.status.order_id,
                message="%s: %s" % (type(exc).__name__, redact(str(exc))),
                updated_at=time.time(),
            )
        self.checks += 1
        changed = status.state != self.status.state
        self._transition(status, record=changed)
        if changed:
            self._announce(status)
        return self.status

    def run_until_terminal(self, *, max_checks: Optional[int] = None, sleeper: Optional[Callable[[float], None]] = None) -> WaitlistStatus:
        """同步跑到底。

        sleeper 的优先级：显式参数 > 构造时注入的 sleeper > time.sleep。
        之前这里直接取 time.sleep，导致构造时注入的 sleeper 在提交重试路径上用到了、
        在轮询路径上却被忽略 —— 表现为「测试里传了 sleeper 还是真的等了 90 秒」。
        """
        limit = max_checks if max_checks is not None else self.config.max_checks
        sleep_fn = sleeper or self._sleeper
        if self.status.state == WaitlistState.PENDING_SUBMIT:
            self.submit()
        while not self.status.is_terminal and self.checks < limit:
            sleep_fn(self.next_interval())
            self.check_once()
        if not self.status.is_terminal:
            self._notify(
                Event.TASK_ERROR,
                "候补查询达到上限 %d 次仍未出结果，已停止轮询（可手动再跑）" % limit,
                {"order_id": self.status.order_id, "state": self.status.state},
            )
        return self.status

    def cancel(self) -> WaitlistStatus:
        status = self.backend.cancel(self.status.order_id)
        self._transition(status)
        self._notify(Event.SYSTEM, "已取消候补：%s" % status.message, {"order_id": status.order_id})
        return self.status

    # -- 内部 ----------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        return exponential_backoff(
            attempt, self.config.first_check_seconds / 10.0, self.config.backoff_factor,
            self.config.backoff_cap, jitter_ratio=self.config.jitter_ratio, stream=self._stream,
        )

    def _sleep(self, seconds: float) -> None:
        self._sleeper(max(0.0, seconds))

    def _transition(self, status: WaitlistStatus, *, record: bool = True) -> None:
        self.status = status
        if record:
            self.history.append(status)

    def _announce(self, status: WaitlistStatus) -> None:
        if status.state == WaitlistState.FULFILLED:
            self._notify(
                Event.TICKET_SUCCESS,
                "候补已兑现：%s %s->%s（订单 %s），请尽快确认支付"
                % (self.request.left_date, self.request.left_station, self.request.arrive_station, status.order_id),
                {"order_id": status.order_id, "state": status.state},
            )
        elif status.state in (WaitlistState.FAILED, WaitlistState.EXPIRED):
            self._notify(
                Event.TICKET_ALL_FAILED,
                "候补未兑现（%s）：%s %s->%s。可考虑改签其他车次或重试候补"
                % (status.state, self.request.left_date, self.request.left_station, self.request.arrive_station),
                {"order_id": status.order_id, "state": status.state},
            )
        elif status.state == WaitlistState.ERROR:
            self._notify(
                Event.TASK_ERROR,
                "候补查询持续失败：" + status.message,
                {"order_id": status.order_id, "key": self.request.key()},
            )


# --- 环境变量入口 ---------------------------------------------------------


def request_from_env(env: Optional[Dict[str, str]] = None) -> Optional[WaitlistRequest]:
    """从 WAITLIST_JSON 构造候补申请。

    WAITLIST_JSON 形如：
    {
      "left_date": "2026-10-01",
      "left_station": "北京",
      "arrive_station": "上海",
      "train_numbers": ["G1", "G3"],
      "seat_types": ["O"],
      "passengers": [{"passenger_name": "张三", "passenger_id_no": "110101...", "passenger_id_type_code": "1", "passenger_type": "1"}],
      "accept_no_seat": true,
      "accept_adjacent": false
    }
    """
    import os

    env = dict(os.environ if env is None else env)
    raw = (env.get("WAITLIST_JSON") or "").strip()
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("WAITLIST_JSON 必须是对象")
    for required in ("left_date", "left_station", "arrive_station"):
        if not data.get(required):
            raise ValueError("WAITLIST_JSON 缺少必填字段 %s" % required)
    return WaitlistRequest(
        left_date=str(data["left_date"]),
        left_station=str(data["left_station"]),
        arrive_station=str(data["arrive_station"]),
        train_numbers=[str(x) for x in data.get("train_numbers", [])],
        seat_types=[str(x) for x in data.get("seat_types", [])],
        passengers=list(data.get("passengers", [])),
        purpose_codes=str(data.get("purpose_codes", "ADULT")),
        accept_no_seat=bool(data.get("accept_no_seat", True)),
        accept_adjacent=bool(data.get("accept_adjacent", False)),
        deadline_minutes_before_departure=int(data.get("deadline_minutes_before_departure", 120)),
        extra=dict(data.get("extra", {})),
    )
