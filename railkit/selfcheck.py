"""首次启动自检：python main.py -t

规格书第 10 条要求：能列出账号、连通性、Redis 状态；第 12 条要求把风险提示打出来。
本模块只依赖标准库 + railkit 自身，Redis/Crypto 都做优雅降级，没装也不崩。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .config import Config, ConfigError, build_config
from .notifier import NotifyHub
from .redaction import install_everywhere, redact
from .risk import RiskBreaker, RiskConfig, classify_response
from .runtime_state import LoginStateStore, StateEncryptionUnavailable, harden_dir
from .timing import QueryTimingConfig, new_stream, next_query_delay

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

RISK_NOTICE = (
    "本工具违反 12306 服务条款，使用即承担账号被封、订单被取消的风险；"
    "12306 风控会识别高频请求，做了退避也无法保证不被封。"
    "官方候补功能是更稳妥的选择，应优先使用。"
)


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class SelfCheckReport:
    results: List[CheckResult]
    config: Optional[Config] = None

    @property
    def exit_code(self) -> int:
        return 1 if any(r.failed for r in self.results) else 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "railkit_version": __version__,
            "exit_code": self.exit_code,
            "checks": [
                {"name": r.name, "status": r.status, "detail": r.detail, "data": r.data} for r in self.results
            ],
            "config": self.config.scrub() if self.config else None,
            "risk_notice": RISK_NOTICE,
        }


# --- 单项检查 -------------------------------------------------------------


def check_python() -> CheckResult:
    info = sys.version_info
    detail = "Python %d.%d.%d (%s)" % (info.major, info.minor, info.micro, sys.executable)
    if info[:2] < (3, 9):
        return CheckResult("Python 版本", FAIL, detail + " —— 低于 3.9，3.6 已 EOL，必须先升级基础镜像")
    if info[:2] < (3, 11):
        return CheckResult("Python 版本", WARN, detail + " —— 建议 3.11（Dockerfile 已用 python:3.11-slim）")
    return CheckResult("Python 版本", PASS, detail)


def check_config(base_dir: Path) -> tuple:
    try:
        config = build_config(base_dir=base_dir, strict=True)
    except ConfigError as exc:
        return CheckResult("配置校验", FAIL, str(exc)), None
    warnings = list(config.warnings)
    adapters = ",".join(config.notify.adapters)
    detail = "账号 %d 个；通知适配器 %s" % (len(config.accounts), adapters)
    if warnings:
        detail += "；告警：" + "；".join(warnings)
    return CheckResult("配置校验", WARN if warnings else PASS, detail), config


def check_secrets_hygiene(config: Config) -> CheckResult:
    """检查是否存在会把明文密钥带进日志/镜像的文件。"""
    root = config.paths.base_dir
    problems: List[str] = []
    if (root / "env.py").is_file():
        problems.append("env.py 仍然存在（含明文密码/token）——按规格书 3.1 应当删除，改用环境变量")
    for name in (".env", ".env.local"):
        candidate = root / name
        if candidate.is_file():
            try:
                mode = candidate.stat().st_mode & 0o077
                if mode:
                    problems.append("%s 权限过宽（%s），建议 chmod 600" % (name, oct(candidate.stat().st_mode & 0o777)))
            except OSError:
                pass
    if config.web and not config.web.jwt_secret:
        problems.append("JWT_SECRET_KEY 未设置")
    if problems:
        return CheckResult("密钥卫生", WARN, "；".join(problems))
    return CheckResult("密钥卫生", PASS, "未发现明文密钥文件；密钥均来自环境变量")


def check_redaction(config: Config) -> CheckResult:
    policy = config.redaction_policy()
    install_everywhere(policy)
    jwt = config.web.jwt_secret.reveal() if config.web and config.web.jwt_secret else "no-jwt-set"
    probe = (
        "GET /query?cookie=RAIL_DEVICEID=abcdefg1234567890;JSESSIONID=XYZ9876543210 "
        "password=hunter2 phone=13812345678 id=11010119900307721X jwt=" + jwt
    )
    masked = redact(probe, policy)
    leaked = []
    for needle in ("hunter2", "13812345678", "11010119900307721X", "XYZ9876543210", "abcdefg1234567890"):
        if needle in masked:
            leaked.append(needle)
    if config.web and config.web.jwt_secret and config.web.jwt_secret.reveal() in masked:
        leaked.append("jwt")
    data = {"sample": masked}
    if leaked:
        return CheckResult("日志脱敏", FAIL, "以下内容未被脱敏：" + str(leaked), data)
    return CheckResult("日志脱敏", PASS, "示例输出：" + masked, data)


def check_runtime_dirs(config: Config) -> CheckResult:
    try:
        config.paths.ensure()
    except OSError as exc:
        return CheckResult("目录与权限", FAIL, "创建 runtime/data/logs 失败：%s" % exc)
    store = LoginStateStore.from_config(config)
    harden_dir(store.root)
    encrypted = "是" if config.enc_key else "否"
    states = store.list_states()
    detail = "runtime 目录 %s；已存在登录态 %d 个；加密：%s" % (config.paths.state_dir, len(states), encrypted)
    if not config.enc_key and not store.allow_plaintext:
        return CheckResult(
            "目录与权限",
            WARN,
            detail + " —— 未设置 RUNTIME_ENC_KEY，写入登录态会直接报错（这是刻意的失败关闭）",
        )
    return CheckResult("目录与权限", PASS, detail)


def check_redis(config: Config, *, timeout: float = 3.0) -> CheckResult:
    """不依赖 redis 库，直接用 socket 做 RESP PING，方便自检。"""
    url = config.redis.url.reveal() if config.redis else ""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    password = parsed.password
    db = (parsed.path or "/0").lstrip("/") or "0"
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            if password:
                _resp(sock, ["AUTH", password])
            if db != "0":
                _resp(sock, ["SELECT", db])
            reply = _resp(sock, ["PING"])
    except OSError as exc:
        return CheckResult("Redis 连通性", FAIL, "%s:%d 连接失败：%s（队列强依赖 Redis，必须先起）" % (host, port, exc))
    except RuntimeError as exc:
        return CheckResult("Redis 连通性", FAIL, "%s:%d 认证/协议错误：%s" % (host, port, exc))
    elapsed = (time.time() - started) * 1000
    if reply in ("PONG", b"PONG"):
        return CheckResult("Redis 连通性", PASS, "%s:%d db=%s PONG（%.0fms）" % (host, port, db, elapsed))
    return CheckResult("Redis 连通性", WARN, "%s:%d 响应异常：%r" % (host, port, reply))


def _resp(sock: socket.socket, args: Sequence[str]) -> Any:
    payload = ("*" + str(len(args)) + "\r\n").encode()
    for arg in args:
        raw = str(arg).encode()
        payload += ("$" + str(len(raw)) + "\r\n").encode() + raw + b"\r\n"
    sock.sendall(payload)
    return _read_reply(sock)


def _read_reply(sock: socket.socket) -> Any:
    line = _read_line(sock)
    prefix, body = line[:1], line[1:]
    if prefix == b"+":
        return body.decode()
    if prefix == b"-":
        raise RuntimeError(body.decode("utf-8", "replace"))
    if prefix == b":":
        return int(body)
    if prefix == b"$":
        length = int(body)
        if length < 0:
            return None
        data = _read_exact(sock, length + 2)
        return data[:-2]
    if prefix == b"*":
        count = int(body)
        if count < 0:
            return None
        return [_read_reply(sock) for _ in range(count)]
    return line


def _read_line(sock: socket.socket) -> bytes:
    buf = bytearray()
    while not buf.endswith(b"\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise RuntimeError("连接被对端关闭")
        buf += chunk
        if len(buf) > 65536:
            raise RuntimeError("响应行过长")
    return bytes(buf[:-2])


def _read_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise RuntimeError("连接被对端关闭")
        buf += chunk
    return bytes(buf)


def check_notifier(config: Config, *, probe: bool, hub: NotifyHub) -> CheckResult:
    kinds = ",".join(a.name for a in hub.adapters)
    if not probe:
        return CheckResult("通知适配器", PASS, "已装配：%s（加 --notify-test 可实发测试消息）" % kinds)
    results = hub.notify("SYSTEM", "自检消息：如果你看到这条，说明告警链路是通的。", {"source": "main.py -t"})
    detail = "已装配：%s；发送结果：%s" % (kinds, json.dumps(results, ensure_ascii=False))
    bad = {k: v for k, v in results.items() if not v.get("ok") and not v.get("skipped")}
    status = WARN if bad else PASS
    return CheckResult("通知适配器", status, detail, {"results": results})


def check_risk_control(config: Config) -> CheckResult:
    """用假时钟跑一遍「连续异常 -> 退避 -> 命中风控 -> 熔断 -> 探针复归」。"""
    risk_config = RiskConfig(**dict(config.risk))
    events: List[Dict[str, Any]] = []
    breaker = RiskBreaker(
        risk_config,
        key="selfcheck",
        notifier=lambda event, message, extra: events.append({"event": event, "message": message, **extra}),
        stream=new_stream("selfcheck", seed=7),
    )
    now = 1000.0
    timeline: List[Dict[str, Any]] = []

    for _ in range(risk_config.failure_threshold):
        d = breaker.before_query(now=now)
        timeline.append({"t": round(now, 1), "action": "query", "allow": d.allow, "state": d.state})
        breaker.report_failure("transport", "connection timeout", now=now)
        now += 0.1
    d = breaker.before_query(now=now)
    timeline.append(
        {"t": round(now, 1), "action": "after-failures", "allow": d.allow, "wait": round(d.wait_seconds, 1), "state": d.state}
    )

    detected = classify_response(200, {}, '{"result_message":"您的访问过于频繁，请稍后再试"}')
    timeline.append({"t": round(now, 1), "action": "classify", "category": detected.category, "reason": detected.reason})
    breaker.report_failure(detected.category or "risk_control", detected.reason, now=now)
    d = breaker.before_query(now=now)
    timeline.append(
        {"t": round(now, 1), "action": "after-risk", "allow": d.allow, "wait": round(d.wait_seconds, 1), "state": d.state}
    )

    now += d.wait_seconds + 0.1
    d = breaker.before_query(now=now)
    timeline.append({"t": round(now, 1), "action": "probe", "allow": d.allow, "probe": d.probe, "state": d.state})
    for _ in range(risk_config.require_successes):
        breaker.report_success(now=now)
        now += 0.1
    snap = breaker.snapshot(now=now)
    timeline.append({"t": round(now, 1), "action": "final", "state": snap.state})

    flow = " -> ".join(str(t["state"]) for t in timeline if "state" in t)
    details = "；".join(str(e["message"]) for e in events)
    return CheckResult(
        "风控熔断演练",
        PASS,
        "状态流转：%s；告警事件：%s" % (flow, details or "（无）"),
        {"timeline": timeline, "events": events, "snapshot": snap.as_dict()},
    )


def check_query_schedule(config: Config, *, limit: int = 4) -> CheckResult:
    timing = QueryTimingConfig(
        normal_base_seconds=max(config.query.interval_seconds, 1.0),
        pre_sale_base_seconds=min(1.5, max(config.query.interval_seconds, 1.0)),
        window_base_seconds=max(config.query.interval_seconds, 1.0) + 1.0,
        window_start_minutes=config.query.pre_sale_window_minutes // 60 or 1,
    )
    rows: List[Dict[str, Any]] = []
    for idx in range(limit):
        key = "task-%d|2026-10-01|北京-上海" % (idx % 2)
        stream = new_stream(key)
        rows.append(
            {
                "task": idx % 2,
                "normal": round(next_query_delay(timing, False, stream), 2),
                "pre_sale": round(next_query_delay(timing, True, stream), 2),
            }
        )
    jitter_pct = int(config.risk.get("jitter_ratio", 0.2) * 100)
    return CheckResult(
        "抖动间隔抽样",
        PASS,
        "基础间隔 %ss，±%d%% 抖动；抽样 %s" % (config.query.interval_seconds, jitter_pct, rows),
        {"rows": rows},
    )


def check_login_state(config: Config) -> CheckResult:
    store = LoginStateStore.from_config(config)
    try:
        payload = {"cookies": {"RAIL_DEVICEID": "probe-device-id"}, "probe": True}
        path = store.save("selfcheck-probe", payload)
        loaded = store.load("selfcheck-probe")
        assert loaded == payload, "登录态加解密往返不一致"
        encrypted = path.name.endswith(".enc")
        store.purge(["selfcheck-probe"])
        if not encrypted:
            return CheckResult("登录态加密", WARN, "自检写入的是明文（ALLOW_PLAINTEXT_STATE=1）")
        return CheckResult("登录态加密", PASS, "加密写入 %s 并可正确读回，随后已清除" % path.name)
    except StateEncryptionUnavailable as exc:
        return CheckResult("登录态加密", WARN, str(exc))
    except Exception as exc:
        return CheckResult("登录态加密", FAIL, "%s: %s" % (type(exc).__name__, exc))


# --- 主入口 ---------------------------------------------------------------


def run(
    *,
    base_dir: Optional[Path] = None,
    probe_notifications: bool = False,
    skip_network: bool = False,
    health_only: bool = False,
) -> SelfCheckReport:
    root = Path(base_dir or os.getcwd()).resolve()
    results: List[CheckResult] = [check_python()]

    config_result, config = check_config(root)
    results.append(config_result)
    if config is None:
        return SelfCheckReport(results, None)

    install_everywhere(config.redaction_policy())
    hub = NotifyHub.from_env(config.raw_env)

    if health_only:
        results.append(check_redis(config))
        results.append(CheckResult("服务状态", PASS, "健康检查通过"))
        return SelfCheckReport(results, config)

    results.append(check_secrets_hygiene(config))
    results.append(check_redaction(config))
    results.append(check_runtime_dirs(config))
    results.append(check_login_state(config))
    results.append(check_redis(config) if not skip_network else CheckResult("Redis 连通性", SKIP, "--offline 跳过"))
    results.append(check_notifier(config, probe=probe_notifications, hub=hub))
    results.append(check_risk_control(config))
    results.append(check_query_schedule(config))
    return SelfCheckReport(results, config)


# --- 输出 -----------------------------------------------------------------

_COLORS = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[36m"}
_RESET = "\033[0m"


def render(report: SelfCheckReport, *, color: Optional[bool] = None) -> str:
    if color is None:
        color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    lines: List[str] = []
    lines.append("py12306 自检 (railkit %s)" % __version__)
    lines.append("=" * 72)
    for item in report.results:
        if color:
            tag = "%s%-4s%s" % (_COLORS.get(item.status, ""), item.status, _RESET)
        else:
            tag = "%-4s" % item.status
        lines.append("[%s] %s" % (tag, item.name))
        if item.detail:
            for ln in str(item.detail).splitlines():
                lines.append("       " + ln)
    lines.append("=" * 72)

    if report.config:
        config = report.config
        lines.append("账号：")
        for acc in config.accounts:
            has_pwd = "已设置" if acc.password else "无"
            lines.append("  - %s（登录方式 %s，密码 %s）" % (acc.username, acc.login_type, has_pwd))
        lines.append("数据目录：%s" % config.paths.data_dir)
        lines.append("登录态目录：%s" % (config.paths.state_dir / "user"))
        lines.append("Redis：%s" % config.redis.display())
        lines.append("通知：%s" % ",".join(config.notify.adapters))
        lines.append("")
        lines.append("风险提示：")
        lines.append("  " + RISK_NOTICE)

    failed = [r for r in report.results if r.failed]
    lines.append("")
    lines.append("结果：%d/%d 项通过" % (len(report.results) - len(failed), len(report.results)))
    return "\n".join(lines)
