"""可视化面板：Flask 蓝图，独立路由 /panel，零构建（原生 JS + 手写 SVG 图表）。

为什么不重写上游前端：
- 上游 py12306/web/static 里是已编译的 Vue2 产物，仓库里没有源码，改不动；
- 引入 Node 构建链会显著拉高部署与 CI 复杂度，而面板要解决的问题是「看清状态」，
  不是「好看的交互」，原生 JS 足够。

安全（spec 第 7 条）：
- 默认只允许 127.0.0.1/::1 访问 /panel 与 /panel/api/*
- 需要远程访问时必须显式设置 PANEL_ALLOW_REMOTE=1，并可配 PANEL_TOKEN 二次校验
- 面板不返回任何密钥；配置信息走 Config.scrub()
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, Response, jsonify, request, send_file

panel = Blueprint("panel", __name__, url_prefix="/panel")

#: 面板入口 HTML 路径（零构建，随仓库分发）
HTML_PATH = Path(__file__).with_name("ui") / "index.html"
#: 图标由 tools/make_favicon.py 用标准库生成，避免把二进制资源不明来源地塞进仓库
FAVICON_PATH = Path(__file__).with_name("ui") / "favicon.png"

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


# --- 访问控制 -------------------------------------------------------------


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def _allow_remote() -> bool:
    return (os.environ.get("PANEL_ALLOW_REMOTE", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}


def _panel_token() -> str:
    return (os.environ.get("PANEL_TOKEN", "") or "").strip()


def access_denied() -> Optional[Tuple[Any, int]]:
    """返回错误响应表示拒绝访问，None 表示放行。"""
    ip = _client_ip()
    token_needed = _panel_token()
    token_given = request.headers.get("X-Panel-Token", "") or request.args.get("token", "")
    if token_needed and token_given == token_needed:
        return None
    if ip in _LOOPBACK or ip.startswith("127."):
        return None
    if not _allow_remote():
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "panel_access_denied",
                    "message": "面板默认只允许本机访问。需要远程访问请设置 PANEL_ALLOW_REMOTE=1 并建议配 PANEL_TOKEN。",
                    "client_ip": ip,
                }
            ),
            403,
        )
    if token_needed and token_given != token_needed:
        return jsonify({"ok": False, "error": "panel_bad_token", "message": "缺少或错误的 X-Panel-Token"}), 401
    return None


def _guard() -> Any:
    denied = access_denied()
    if denied is not None:
        return denied
    return None


# --- 数据装配 -------------------------------------------------------------


def _store():
    from railkit.metrics import get_store

    return get_store()


def _integration():
    from railkit.integration import get_integration

    return get_integration()


def _railkit_config():
    try:
        from railkit.config import load_config

        return load_config()
    except Exception:
        return None


def _upstream_meta() -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    try:
        from py12306.config import Config as UpstreamConfig

        upstream = UpstreamConfig()
        jobs = getattr(upstream, "QUERY_JOBS", []) or []
        meta["query_jobs"] = len(jobs)
        meta["upstream_interval"] = getattr(upstream, "QUERY_INTERVAL", None)
        meta["web_port"] = getattr(upstream, "WEB_PORT", None)
        meta["cluster_enabled"] = bool(getattr(upstream, "CLUSTER_ENABLED", 0))
        meta["query_job_thread_enabled"] = bool(getattr(upstream, "QUERY_JOB_THREAD_ENABLED", 0))
        meta["log_to_file"] = bool(getattr(upstream, "OUT_PUT_LOG_TO_FILE_ENABLED", 0))
        meta["log_file"] = getattr(upstream, "OUT_PUT_LOG_TO_FILE_PATH", "")
    except Exception as exc:
        meta["upstream_error"] = type(exc).__name__
    try:
        from py12306.user.user import User

        meta["user_count"] = len(User().users)
    except Exception:
        meta["user_count"] = None
    return meta


def _redis_health(timeout: float = 2.0) -> Dict[str, Any]:
    """用 socket 直连做 PING，避免面板依赖 redis 库的异常语义。"""
    import socket
    import urllib.parse

    url = ""
    try:
        from py12306.config import Config as UpstreamConfig

        upstream = UpstreamConfig()
        host = getattr(upstream, "REDIS_HOST", "") or ""
        if host:
            password = getattr(upstream, "REDIS_PASSWORD", "") or ""
            port = getattr(upstream, "REDIS_PORT", "6379") or "6379"
            url = "redis://%s%s:%s/0" % ((":" + password + "@") if password else "", host, port)
    except Exception:
        url = ""
    if not url:
        cfg = _railkit_config()
        url = cfg.redis.url.reveal() if (cfg and cfg.redis) else ""
    if not url:
        return {"configured": False, "ok": False, "detail": "未配置 Redis"}
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            if parsed.password:
                _resp(sock, ["AUTH", parsed.password])
            reply = _resp(sock, ["PING"])
        return {
            "configured": True,
            "ok": reply in ("PONG", b"PONG"),
            "target": "%s:%d" % (host, port),
            "latency_ms": round((time.time() - started) * 1000, 1),
        }
    except OSError as exc:
        return {
            "configured": True,
            "ok": False,
            "target": "%s:%d" % (host, port),
            "detail": "%s: %s" % (type(exc).__name__, exc),
        }


def _resp(sock, args):
    payload = ("*" + str(len(args)) + "\r\n").encode()
    for arg in args:
        raw = str(arg).encode()
        payload += ("$" + str(len(raw)) + "\r\n").encode() + raw + b"\r\n"
    sock.sendall(payload)
    return _read_reply(sock)


def _read_reply(sock):
    line = _read_line(sock)
    prefix, body = line[:1], line[1:]
    if prefix == b"+":
        return body.decode()
    if prefix == b"-":
        raise OSError(body.decode("utf-8", "replace"))
    if prefix == b"$":
        length = int(body)
        if length < 0:
            return None
        data = _read_exact(sock, length + 2)
        return data[:-2]
    return line


def _read_line(sock):
    buf = bytearray()
    while not buf.endswith(b"\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise OSError("连接被对端关闭")
        buf += chunk
        if len(buf) > 65536:
            raise OSError("响应行过长")
    return bytes(buf[:-2])


def _read_exact(sock, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise OSError("连接被对端关闭")
        buf += chunk
    return bytes(buf)


def _login_states() -> List[Dict[str, Any]]:
    config = _railkit_config()
    if config is None:
        return []
    try:
        from railkit.runtime_state import LoginStateStore

        store = LoginStateStore.from_config(config)
        return store.audit()
    except Exception:
        return []


def _notify_status() -> Dict[str, Any]:
    integration = _integration()
    if integration is None:
        return {"adapters": [], "status": {}}
    hub = integration.hub
    return {
        "adapters": [
            {"name": adapter.name, "events": list(getattr(adapter, "events", ()) or ())}
            for adapter in hub.adapters
        ],
        "status": hub.status(),
        "history": [
            {"ts": item["ts"], "event": item["event"], "title": item["title"], "results": item["results"]}
            for item in list(hub.history)[-10:][::-1]
        ],
    }


# --- 路由 -----------------------------------------------------------------


@panel.route("/", methods=["GET"])
@panel.route("/index.html", methods=["GET"])
def index():
    denied = _guard()
    if denied is not None:
        return denied
    if not HTML_PATH.is_file():
        return Response("面板静态文件缺失：%s" % HTML_PATH, status=500, mimetype="text/plain")
    return send_file(str(HTML_PATH))


@panel.route("/favicon.png", methods=["GET"])
@panel.route("/favicon.ico", methods=["GET"])
def favicon():
    """没这个路由浏览器每次都报 404，日志里全是噪音。"""
    if FAVICON_PATH.is_file():
        return send_file(str(FAVICON_PATH), mimetype="image/png", max_age=86400)
    return Response(status=204)


@panel.route("/api/overview", methods=["GET"])
def api_overview():
    denied = _guard()
    if denied is not None:
        return denied
    store = _store()
    integration = _integration()
    config = _railkit_config()
    risk_notice = (
        "本工具违反 12306 服务条款，使用即承担账号被封、订单被取消的风险；"
        "12306 风控会识别高频请求，做了退避也无法保证不被封。官方候补是更稳妥的选择。"
    )
    return jsonify(
        {
            "ok": True,
            "ts": time.time(),
            "summary": store.summary(),
            "tasks": store.tasks(),
            "redis": _redis_health(),
            "notify": _notify_status(),
            "upstream": _upstream_meta(),
            "login_states": _login_states(),
            "integration": {
                "installed": integration is not None,
                "patched": bool(integration.patched) if integration else False,
                "max_station_pairs": integration.config.max_station_pairs if integration else None,
                "jitter_ratio": integration.config.risk.jitter_ratio if integration else None,
                "breaker_base_s": integration.config.risk.breaker_base if integration else None,
                "breaker_cap_s": integration.config.risk.breaker_cap if integration else None,
                "query_interval_s": integration.config.query_interval if integration else None,
            },
            "config": config.scrub() if config else None,
            "panel": {
                "allow_remote": _allow_remote(),
                "token_required": bool(_panel_token()),
                "client_ip": _client_ip(),
            },
            "risk_notice": risk_notice,
        }
    )


@panel.route("/api/series", methods=["GET"])
def api_series():
    denied = _guard()
    if denied is not None:
        return denied
    try:
        bucket = float(request.args.get("bucket", 60))
    except ValueError:
        bucket = 60.0
    try:
        buckets = int(request.args.get("buckets", 30))
    except ValueError:
        buckets = 30
    buckets = max(5, min(buckets, 240))
    return jsonify({"ok": True, "series": _store().series(bucket_seconds=bucket, buckets=buckets)})


@panel.route("/api/breaker-events", methods=["GET"])
def api_breaker_events():
    denied = _guard()
    if denied is not None:
        return denied
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    return jsonify({"ok": True, "events": _store().recent_breaker_events(limit=max(1, min(limit, 200)))})


@panel.route("/api/actions/reset-breaker", methods=["POST"])
def api_reset_breaker():
    denied = _guard()
    if denied is not None:
        return denied
    integration = _integration()
    if integration is None:
        return jsonify({"ok": False, "error": "integration_not_installed"}), 409
    payload = request.get_json(silent=True) or {}
    task = str(payload.get("task") or "")
    if not task:
        return jsonify({"ok": False, "error": "task_required"}), 400
    import threading

    with integration._lock:
        breaker = integration.registry._breakers.get(task)
    if breaker is None:
        return jsonify({"ok": False, "error": "task_not_found", "task": task}), 404
    breaker.reset("manual reset from panel")
    return jsonify({"ok": True, "task": task, "state": breaker.state})


@panel.route("/api/actions/notify-test", methods=["POST"])
def api_notify_test():
    denied = _guard()
    if denied is not None:
        return denied
    integration = _integration()
    if integration is None:
        return jsonify({"ok": False, "error": "integration_not_installed"}), 409
    payload = request.get_json(silent=True) or {}
    event = str(payload.get("event") or "SYSTEM")
    message = str(payload.get("message") or "面板自检：如果你看到这条，说明告警链路是通的。")
    results = integration.hub.notify(event, message, {"source": "panel", "ip": _client_ip()})
    failed = [name for name, item in results.items() if not item.get("ok") and not item.get("skipped")]
    return jsonify({"ok": not failed, "results": results, "event": event})


@panel.route("/api/actions/purge-login-state", methods=["POST"])
def api_purge_login_state():
    denied = _guard()
    if denied is not None:
        return denied
    config = _railkit_config()
    if config is None:
        return jsonify({"ok": False, "error": "config_invalid"}), 409
    from railkit.runtime_state import LoginStateStore

    store = LoginStateStore.from_config(config)
    removed = [str(p) for p in store.purge()]
    return jsonify({"ok": True, "removed": removed, "count": len(removed)})


@panel.route("/api/logs", methods=["GET"])
def api_logs():
    """轮询式日志读取（末端行）。比 SSE 更省资源，前端 2 秒拉一次足够。"""
    denied = _guard()
    if denied is not None:
        return denied
    try:
        lines = int(request.args.get("lines", 200))
    except ValueError:
        lines = 200
    lines = max(10, min(lines, 2000))
    meta = _upstream_meta()
    path = meta.get("log_file") or ""
    if not path or not Path(path).is_file():
        return jsonify({"ok": True, "lines": [], "path": path, "detail": "日志文件不存在或未开启写文件"})
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.readlines()
    except OSError as exc:
        return jsonify({"ok": False, "error": "read_failed", "detail": str(exc)}), 500
    return jsonify({"ok": True, "path": path, "lines": [line.rstrip("\n") for line in content[-lines:]]})


@panel.route("/api/logs/stream", methods=["GET"])
def api_logs_stream():
    """SSE：增量推送日志尾部。"""
    denied = _guard()
    if denied is not None:
        return denied
    meta = _upstream_meta()
    path = meta.get("log_file") or ""

    def generate():
        position = 0
        if path and Path(path).is_file():
            position = Path(path).stat().st_size
        yield "event: hello\ndata: %s\n\n" % json.dumps({"path": path}, ensure_ascii=False)
        # 先把尾部已有日志补一遍，否则打开页面只能看到「之后的」日志，历史全丢
        if path and Path(path).is_file():
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    tail = handle.readlines()[-200:]
                for line in tail:
                    if line.strip():
                        yield "data: %s\n\n" % json.dumps({"line": line.rstrip("\n")}, ensure_ascii=False)
            except OSError:
                pass
        idle = 0
        while True:
            if not path or not Path(path).is_file():
                time.sleep(2)
                idle += 1
                if idle > 300:
                    return
                continue
            try:
                size = Path(path).stat().st_size
                if size < position:  # 日志被轮转
                    position = 0
                if size > position:
                    with open(path, "r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(position)
                        chunk = handle.read()
                        position = handle.tell()
                    for line in chunk.splitlines():
                        if line.strip():
                            yield "data: %s\n\n" % json.dumps({"line": line}, ensure_ascii=False)
                idle = 0
            except OSError:
                pass
            time.sleep(1.0)

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@panel.route("/api/metrics.prom", methods=["GET"])
def api_prometheus():
    """Prometheus 文本格式（spec 第 8 节的指标，不引入 prometheus_client）。"""
    denied = None
    ip = _client_ip()
    token_needed = _panel_token()
    token_given = request.headers.get("X-Panel-Token", "") or request.args.get("token", "")
    if not (ip in _LOOPBACK or ip.startswith("127.") or (token_needed and token_given == token_needed)):
        if not _allow_remote():
            denied = jsonify({"ok": False, "error": "panel_access_denied"}), 403
    if denied is not None:
        return denied
    return Response(_store().to_prometheus(), mimetype="text/plain; version=0.0.4")


@panel.route("/api/health", methods=["GET"])
def api_health():
    store = _store()
    redis = _redis_health(timeout=1.5)
    summary = store.summary()
    ok = redis.get("ok", False) if redis.get("configured") else True
    return jsonify({"ok": bool(ok), "redis": redis, "tasks": summary["task_count"], "uptime_s": summary["uptime_seconds"]})
