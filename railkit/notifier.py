"""P0-2 通知层：统一接口 + 适配器（规格书第 4 节的接口先在这里立起来）。

P0 只做熔断告警真正会用到的部分：
- 统一入口 notify(event, message, extra)
- 事件类型枚举化
- 适配器互不影响、失败重试、失败记录、冷却
- 绝不因为通知失败影响主流程

适配器边界：本模块只用标准库发 HTTP（urllib），不依赖 requests，
这样单测可以在完全离线的情况下跑（注入 fake transport）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .redaction import redact

logger = logging.getLogger(__name__)


class Event:
    """事件类型（规格书 4 要求的枚举化）。"""

    TICKET_SUCCESS = "TICKET_SUCCESS"
    NO_TICKET = "NO_TICKET"
    LOGIN_EXPIRED = "LOGIN_EXPIRED"
    CAPTCHA_FAILED = "CAPTCHA_FAILED"
    RISK_CONTROL = "RISK_CONTROL"
    TICKET_ALL_FAILED = "TICKET_ALL_FAILED"
    TASK_ERROR = "TASK_ERROR"
    SYSTEM = "SYSTEM"

    ALL = (
        TICKET_SUCCESS,
        NO_TICKET,
        LOGIN_EXPIRED,
        CAPTCHA_FAILED,
        RISK_CONTROL,
        TICKET_ALL_FAILED,
        TASK_ERROR,
        SYSTEM,
    )

    #: 熔断器内部用的短名字，统一映射到上面的枚举
    ALIASES = {
        "risk_control": RISK_CONTROL,
        "backoff": RISK_CONTROL,
        "recovered": SYSTEM,
        "login_expired": LOGIN_EXPIRED,
        "captcha_failed": CAPTCHA_FAILED,
        "task_error": TASK_ERROR,
    }

    @classmethod
    def normalize(cls, event: str) -> str:
        if event in cls.ALL:
            return event
        return cls.ALIASES.get(event, cls.SYSTEM)


#: 告警级别：决定适配器是「打扰用户」还是「只留痕」
SEVERITY = {
    Event.TICKET_SUCCESS: "info",
    Event.NO_TICKET: "low",
    Event.LOGIN_EXPIRED: "high",
    Event.CAPTCHA_FAILED: "high",
    Event.RISK_CONTROL: "high",
    Event.TICKET_ALL_FAILED: "high",
    Event.TASK_ERROR: "high",
    Event.SYSTEM: "low",
}


@dataclass(frozen=True)
class Message:
    event: str
    title: str
    body: str
    extra: Dict[str, Any] = field(default_factory=dict)
    severity: str = "low"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "extra": self.extra,
        }

    def to_markdown(self) -> str:
        lines = [self.body]
        if self.extra:
            lines.append("")
            for k, v in self.extra.items():
                lines.append(f"- {k}: {v}")
        return "\n".join(lines)


# --- 传输层抽象 -----------------------------------------------------------

HttpResponse = Tuple[int, str]


def urllib_post_json(
    url: str,
    payload: Dict[str, Any],
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
) -> HttpResponse:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)), resp.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""


def urllib_get(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
) -> HttpResponse:
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)), resp.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""


# --- 适配器 ---------------------------------------------------------------


@dataclass
class SendResult:
    ok: bool
    detail: str = ""
    attempts: int = 1


class Adapter:
    """所有通知适配器的基类。

    子类只需实现 send()；启停、重试、冷却、记录都在 NotifyHub 里统一处理。
    """

    name: str = "adapter"
    #: 该适配器关心的事件；空表示全部
    events: Tuple[str, ...] = ()

    def wants(self, event: str) -> bool:
        if not self.events:
            return True
        return event in self.events

    def send(self, message: Message, timeout: float = 10.0) -> SendResult:
        raise NotImplementedError


class ConsoleAdapter(Adapter):
    """默认适配器：打到 stdout。容器里 docker logs 就能看到，零配置。"""

    name = "console"

    def __init__(self, out: Optional[Callable[[str], None]] = None, min_severity: str = "low") -> None:
        self._out = out or (lambda s: logger.warning("%s", s))
        order = {"info": 0, "low": 1, "high": 2}
        self._min = order.get(min_severity, 1)

    def send(self, message: Message, timeout: float = 10.0) -> SendResult:
        order = {"info": 0, "low": 1, "high": 2}
        if order.get(message.severity, 1) < self._min:
            return SendResult(True, "skipped(severity)")
        self._out(f"[{message.event}] {message.title} | {message.to_markdown()}")
        return SendResult(True, "logged")


class MemoryAdapter(Adapter):
    """测试/自检用：把消息记在内存里，不发网络请求。"""

    name = "memory"

    def __init__(self) -> None:
        self.messages: List[Message] = []

    def send(self, message: Message, timeout: float = 10.0) -> SendResult:
        self.messages.append(message)
        return SendResult(True, "recorded")


class WebhookAdapter(Adapter):
    """通用 webhook：POST JSON。适合把告警接到自己的看板/中间层。"""

    name = "webhook"

    def __init__(self, url: str, *, headers: Optional[Dict[str, str]] = None, transport: Any = None) -> None:
        if not url:
            raise ValueError("WebhookAdapter 需要 url")
        self.url = url
        self.headers = headers or {}
        self._post = transport or urllib_post_json

    def send(self, message: Message, timeout: float = 10.0) -> SendResult:
        payload = {
            "event": message.event,
            "title": message.title,
            "body": message.body,
            "severity": message.severity,
            "extra": message.extra,
            "text": f"{message.title}\n{message.to_markdown()}",
        }
        code, body = self._post(self.url, payload, headers=self.headers, timeout=timeout)
        if 200 <= code < 300:
            return SendResult(True, f"HTTP {code}")
        return SendResult(False, f"HTTP {code} {body[:120]}")


class DingTalkAdapter(Adapter):
    """钉钉机器人（支持加签）。"""

    name = "dingtalk"

    def __init__(self, webhook: str, *, secret: str = "", transport: Any = None, keyword: str = "") -> None:
        if not webhook:
            raise ValueError("DingTalkAdapter 需要 webhook")
        self.webhook = webhook
        self.secret = secret
        self.keyword = keyword
        self._post = transport or urllib_post_json

    def _signed_url(self, now_ms: Optional[int] = None) -> str:
        if not self.secret:
            return self.webhook
        ts = now_ms if now_ms is not None else int(time.time() * 1000)
        string_to_sign = f"{ts}\n{self.secret}"
        digest = hmac.new(
            self.secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{sep}timestamp={ts}&sign={sign}"

    def send(self, message: Message, timeout: float = 10.0) -> SendResult:
        title = f"{self.keyword}{message.title}"
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n\n{message.to_markdown()}"},
        }
        code, body = self._post(self._signed_url(), payload, timeout=timeout)
        if 200 <= code < 300:
            try:
                data = json.loads(body or "{}")
            except Exception:
                return SendResult(True, f"HTTP {code}")
            if data.get("errcode", 0) == 0:
                return SendResult(True, f"HTTP {code}")
            return SendResult(False, f"errcode={data.get('errcode')} {data.get('errmsg')}")
        return SendResult(False, f"HTTP {code}")


class ServerChanAdapter(Adapter):
    """Server 酱（sctapi.ftqq.com）。"""

    name = "serverchan"

    def __init__(self, key: str, *, transport: Any = None) -> None:
        if not key:
            raise ValueError("ServerChanAdapter 需要 key")
        self.key = key
        self._post = transport or urllib_post_json

    def send(self, message: Message, timeout: float = 10.0) -> SendResult:
        url = f"https://sctapi.ftqq.com/{self.key}.send"
        payload = {"title": message.title[:64], "desp": message.to_markdown()}
        code, body = self._post(url, payload, timeout=timeout)
        if 200 <= code < 300:
            try:
                data = json.loads(body or "{}")
            except Exception:
                return SendResult(True, f"HTTP {code}")
            if data.get("code", 0) == 0:
                return SendResult(True, "ok")
            return SendResult(False, f"code={data.get('code')} {data.get('message')}")
        return SendResult(False, f"HTTP {code}")


class BarkAdapter(Adapter):
    """Bark（iOS 推送）。"""

    name = "bark"

    def __init__(self, url: str, *, group: str = "", transport: Any = None) -> None:
        if not url:
            raise ValueError("BarkAdapter 需要 url（形如 https://api.day.app/<key>）")
        self.url = url.rstrip("/")
        self.group = group
        self._post = transport or urllib_post_json

    def send(self, message: Message, timeout: float = 10.0) -> SendResult:
        payload: Dict[str, Any] = {
            "title": message.title,
            "body": message.body,
        }
        if self.group:
            payload["group"] = self.group
        payload["level"] = "timeSensitive" if message.severity == "high" else "active"
        code, body = self._post(f"{self.url}", payload, timeout=timeout)
        if 200 <= code < 300:
            return SendResult(True, f"HTTP {code}")
        return SendResult(False, f"HTTP {code}")


ADAPTER_FACTORIES: Dict[str, Callable[[Dict[str, str]], Adapter]] = {}


def register_adapter(name: str, factory: Callable[[Dict[str, str]], Adapter]) -> None:
    ADAPTER_FACTORIES[name] = factory


register_adapter("console", lambda env: ConsoleAdapter(min_severity=env.get("NOTIFY_CONSOLE_MIN_SEVERITY", "low")))
register_adapter("memory", lambda env: MemoryAdapter())
register_adapter(
    "webhook",
    lambda env: WebhookAdapter(env.get("WEBHOOK_URL", ""), headers=json.loads(env.get("WEBHOOK_HEADERS_JSON", "") or "{}")),
)
register_adapter(
    "dingtalk",
    lambda env: DingTalkAdapter(
        env.get("DINGTALK_WEBHOOK", ""), secret=env.get("DINGTALK_SECRET", ""), keyword=env.get("DINGTALK_KEYWORD", "")
    ),
)
register_adapter("serverchan", lambda env: ServerChanAdapter(env.get("SERVERCHAN_KEY", "")))
register_adapter("bark", lambda env: BarkAdapter(env.get("BARK_URL", ""), group=env.get("BARK_GROUP", "")))


# --- Hub ------------------------------------------------------------------


@dataclass
class _AdapterState:
    failures: int = 0
    success: int = 0
    last_error: str = ""
    last_error_at: Optional[float] = None
    disabled_until: float = 0.0


class NotifyHub:
    """统一通知入口。

    hub = NotifyHub([ConsoleAdapter(), DingTalkAdapter(...)])
    hub.notify(Event.RISK_CONTROL, "命中风控", {"key": "task-1"})
    """

    def __init__(
        self,
        adapters: Optional[Sequence[Adapter]] = None,
        *,
        max_attempts: int = 3,
        retry_base: float = 0.5,
        cooldown: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        history_size: int = 200,
    ) -> None:
        self._adapters: List[Adapter] = list(adapters or [])
        self.max_attempts = max(1, max_attempts)
        self.retry_base = retry_base
        self.cooldown = cooldown
        self._sleep = sleep
        self._states: Dict[str, _AdapterState] = {a.name: _AdapterState() for a in self._adapters}
        self._history: List[Dict[str, Any]] = []
        self._history_size = history_size
        self._lock = threading.RLock()

    # -- 组装 ----------------------------------------------------------

    def add(self, adapter: Adapter) -> None:
        with self._lock:
            self._adapters.append(adapter)
            self._states.setdefault(adapter.name, _AdapterState())

    @property
    def adapters(self) -> Tuple[Adapter, ...]:
        with self._lock:
            return tuple(self._adapters)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "NotifyHub":
        import os

        env = dict(os.environ if env is None else env)
        raw = (env.get("NOTIFY_ADAPTERS", "") or "").strip()
        names = [n.strip().lower() for n in raw.split(",") if n.strip()] or ["console"]
        adapters: List[Adapter] = []
        for name in names:
            factory = ADAPTER_FACTORIES.get(name)
            if factory is None:
                logger.warning("未知的通知适配器 %r，已忽略（可用：%s）", name, ",".join(sorted(ADAPTER_FACTORIES)))
                continue
            try:
                adapters.append(factory(env))
            except Exception as exc:
                # 一个适配器配置错了，不能连累其他适配器
                logger.warning("通知适配器 %r 初始化失败，已跳过：%s", name, redact(str(exc)))
        if not adapters:
            logger.warning("没有任何可用的通知适配器，回落到 console")
            adapters.append(ConsoleAdapter())
        return cls(adapters)

    # -- 发送 ----------------------------------------------------------

    def notify(
        self,
        event: str,
        message: str = "",
        extra: Optional[Dict[str, Any]] = None,
        *,
        title: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """广播一个事件。返回每个适配器的结果；单个适配器失败只记录，不抛异常。"""
        normalized = Event.normalize(event)
        safe_extra = {k: redact(str(v)) for k, v in (extra or {}).items()}
        msg = Message(
            event=normalized,
            title=title or self._default_title(normalized),
            body=redact(message or self._default_body(normalized)),
            extra=safe_extra,
            severity=SEVERITY.get(normalized, "low"),
        )
        results: Dict[str, Any] = {}
        with self._lock:
            targets = [(a, self._states.setdefault(a.name, _AdapterState())) for a in self._adapters]
        for adapter, state in targets:
            if not adapter.wants(normalized):
                results[adapter.name] = {"ok": True, "skipped": "event-filter"}
                continue
            now = time.monotonic()
            if state.disabled_until > now:
                results[adapter.name] = {"ok": False, "skipped": f"cooldown {state.disabled_until - now:.0f}s"}
                continue
            result = self._send_with_retry(adapter, msg, timeout)
            results[adapter.name] = {
                "ok": result.ok,
                "detail": result.detail,
                "attempts": result.attempts,
            }
            if result.ok:
                state.success += 1
                state.failures = 0
            else:
                state.failures += 1
                state.last_error = result.detail
                state.last_error_at = now
                if state.failures >= self.max_attempts:
                    state.disabled_until = now + self.cooldown
        self._record(msg, results)
        return results

    def _send_with_retry(self, adapter: Adapter, msg: Message, timeout: float) -> SendResult:
        last = SendResult(False, "no attempt")
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = adapter.send(msg, timeout=timeout)
            except Exception as exc:
                result = SendResult(False, f"{type(exc).__name__}: {redact(str(exc))}")
            result.attempts = attempt
            last = result
            if result.ok:
                return result
            if attempt < self.max_attempts:
                self._sleep(self.retry_base * (2 ** (attempt - 1)))
        return last

    def _record(self, msg: Message, results: Dict[str, Any]) -> None:
        with self._lock:
            self._history.append({"ts": time.time(), "event": msg.event, "title": msg.title, "results": results})
            if len(self._history) > self._history_size:
                del self._history[: len(self._history) - self._history_size]

    @property
    def history(self) -> Tuple[Dict[str, Any], ...]:
        with self._lock:
            return tuple(self._history)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                name: {
                    "failures": st.failures,
                    "success": st.success,
                    "last_error": st.last_error,
                    "cooldown_remaining": max(0.0, st.disabled_until - time.monotonic()),
                }
                for name, st in self._states.items()
            }

    @staticmethod
    def _default_title(event: str) -> str:
        return {
            Event.TICKET_SUCCESS: "抢票成功",
            Event.NO_TICKET: "本次未抢到",
            Event.LOGIN_EXPIRED: "登录态已失效",
            Event.CAPTCHA_FAILED: "验证码识别失败",
            Event.RISK_CONTROL: "命中风控，已熔断",
            Event.TICKET_ALL_FAILED: "全部任务失败",
            Event.TASK_ERROR: "任务异常",
            Event.SYSTEM: "系统消息",
        }.get(event, event)

    @staticmethod
    def _default_body(event: str) -> str:
        return {
            Event.RISK_CONTROL: "查询已被暂停并进入指数退避，请勿手动重启任务，等待自动探针复归。",
            Event.LOGIN_EXPIRED: "请重新扫码登录；在登录态恢复前继续查询只会加重风控。",
        }.get(event, "")


def make_notifier(hub: NotifyHub) -> Callable[[str, str, Dict[str, Any]], None]:
    """把 Hub 适配成 RiskBreaker 需要的 notifier(event, message, extra) 回调。"""

    def _notify(event: str, message: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
        hub.notify(event, message, extra or {})

    return _notify
