"""统一入口引导：环境变量 -> 上游 Config 桥接、core 装配、命令分发。

三个命令：
    python app.py -t / --test        启动自检（不查票），退出码 0/1
    python app.py serve [--port N]   单独启动可视化面板
    python app.py --purge-login-state 清除登录态

设计要点：上游 Config 用 EnvLoader.exec(env.py) 取配置，我们在它读取之后做一次
「环境变量覆盖」，键名沿用上游的（USER_ACCOUNTS / QUERY_JOBS / DINGTALK_WEBHOOK ...），
优先级：真实环境变量 > .env > env.py > 代码默认值。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent


def force_utf8_console() -> None:
    """Windows 控制台默认 GBK，中文自检输出会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def load_env_file(path: Optional[Path] = None) -> None:
    """把 .env 读进 os.environ（不覆盖已存在的真实环境变量）。"""
    from .config import load_dotenv

    root = path or PROJECT_DIR
    load_dotenv(root / ".env", os.environ)  # type: ignore[arg-type]


# --- 上游 Config 桥接 -----------------------------------------------------


class ConfigBridge:
    """把环境变量叠加到上游 py12306.config.Config 上。

    只在「上游 Config 已经初始化完成」之后调用，键必须是上游本来就有的属性名，
    避免把无关环境变量灌进配置对象。
    """

    #: 上游 Config 属性 -> 环境变量名（默认同名；这里只列需要改名的）
    ALIASES = {
        "REDIS_HOST": "REDIS_HOST",
        "REDIS_PORT": "REDIS_PORT",
        "REDIS_PASSWORD": "REDIS_PASSWORD",
        "JWT_SECRET_KEY": "JWT_SECRET_KEY",
    }

    #: 需要额外解析的 JSON 型配置
    JSON_KEYS = ("USER_ACCOUNTS", "QUERY_JOBS", "WEB_USER")

    def __init__(self, config_instance: Any, env: Optional[Dict[str, str]] = None) -> None:
        self.config = config_instance
        self.env = dict(os.environ if env is None else env)
        self.applied: List[str] = []

    def _coerce(self, target: Any, raw: str) -> Any:
        """按目标属性现有类型做转换，避免把 '0' 当成真值。"""
        if isinstance(target, bool):
            return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
        if isinstance(target, int):
            try:
                return int(raw)
            except ValueError:
                return target
        if isinstance(target, float):
            try:
                return float(raw)
            except ValueError:
                return target
        if isinstance(target, (list, dict)):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return target
        return raw

    def apply(self) -> List[str]:
        for key in dir(self.config):
            if not key.isupper():
                continue
            env_key = self.ALIASES.get(key, key)
            raw = self.env.get(env_key)
            if raw is None or raw == "":
                continue
            target = getattr(self.config, key, None)
            if key in self.JSON_KEYS:
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    logger.warning("%s 不是合法 JSON，已忽略：%s", env_key, exc)
                    continue
            else:
                value = self._coerce(target, raw)
            if value != target:
                setattr(self.config, key, value)
                self.applied.append(key)
        # 记录来源，便于面板展示
        try:
            envs = getattr(self.config, "envs", []) or []
            for key in self.applied:
                envs.append([key, getattr(self.config, key)])
            self.config.envs = envs
        except Exception:
            pass
        return self.applied


def patch_env_loader() -> None:
    """让上游 Config 初始化完成后立刻叠加环境变量。"""
    from py12306.config import Config as UpstreamConfig

    if getattr(UpstreamConfig, "_core_bridge_patched", False):
        return
    original_init = UpstreamConfig.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        bridge = ConfigBridge(self)
        applied = bridge.apply()
        if applied:
            logger.info("已用环境变量覆盖上游配置：%s", ", ".join(sorted(applied)))
        else:
            logger.info("没有环境变量需要覆盖上游配置（继续使用 env.py）")

    UpstreamConfig.__init__ = patched_init
    UpstreamConfig._core_bridge_patched = True


# --- core 装配 ---------------------------------------------------------


def build_integration_config(config: Any):
    from .integration import IntegrationConfig
    from .risk import RiskConfig
    from .timing import QueryTimingConfig

    risk = RiskConfig(**dict(config.risk))
    timing = QueryTimingConfig(
        normal_base_seconds=max(1.0, config.query.interval_seconds),
        window_base_seconds=max(1.0, config.query.interval_seconds) + 1.0,
        pre_sale_base_seconds=min(1.5, max(1.0, config.query.interval_seconds)),
    )
    return IntegrationConfig(
        risk=risk,
        timing=timing,
        query_interval=config.query.interval_seconds,
        max_station_pairs=config.query.max_station_pairs,
        pre_sale_window_minutes=config.query.pre_sale_window_minutes,
    )


def setup(base_dir: Optional[Path] = None, *, patch_query_loop: bool = True):
    """初始化 core：加载 .env、校验配置、装脱敏、建指标库、接查询循环。

    返回 (core_config, integration)。
    """
    from .config import ConfigError, build_config
    from .metrics import MetricsStore, set_store
    from .redaction import install_everywhere

    root = Path(base_dir or PROJECT_DIR).resolve()
    load_env_file(root)

    try:
        config = build_config(base_dir=root, strict=True)
    except ConfigError as exc:
        # 配置错误在自检里有完整展示；这里只提示，交给调用方决定是否退出
        logger.error("配置校验失败：\n%s", exc)
        raise

    policy = install_everywhere(config.redaction_policy())
    config.paths.ensure()

    store = MetricsStore(db_path=config.paths.data_dir / "metrics.sqlite3")
    set_store(store)

    patch_env_loader()

    from .integration import set_integration
    from .notifier import NotifyHub

    hub = NotifyHub.from_env(config.raw_env)

    integration = None
    if patch_query_loop:
        from .integration import install as install_integration

        integration = install_integration(
            config=build_integration_config(config),
            store=store,
            hub=hub,
        )
    else:
        # serve 模式不打补丁，但仍要建集成：面板需要熔断器注册表、通知历史和配置展示
        from .integration import QueryLoopIntegration

        integration = QueryLoopIntegration(
            config=build_integration_config(config),
            store=store,
            hub=hub,
        )
        set_integration(integration)
    return config, integration, policy


def start_breaker_sync(integration: Any, interval: float = 2.0) -> Any:
    """后台把熔断器快照同步进指标库（面板直接读指标库）。"""
    import threading

    if integration is None:
        return None

    def loop() -> None:
        while True:
            try:
                integration.store.sync_breakers(integration.registry.snapshots())
            except Exception:
                logger.debug("同步熔断器快照失败", exc_info=True)
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="core-breaker-sync", daemon=True)
    thread.start()
    return thread


def setup_logging(config: Any) -> None:
    level = getattr(getattr(config, "logs", None), "level", "INFO") or "INFO"
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )


# --- 命令 -----------------------------------------------------------------


def cmd_selfcheck(argv: List[str]) -> int:
    from .selfcheck import render, run

    parser = argparse.ArgumentParser(prog="app.py -t", add_help=False)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--notify-test", action="store_true")
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument("--health-only", action="store_true")
    parser.add_argument("--base-dir", default=None)
    args, _unknown = parser.parse_known_args(argv)

    report = run(
        base_dir=Path(args.base_dir).resolve() if args.base_dir else PROJECT_DIR,
        probe_notifications=args.notify_test,
        skip_network=args.offline,
        health_only=args.health_only,
    )
    if args.as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return report.exit_code


def cmd_purge_login_state(argv: List[str]) -> int:
    from .config import build_config
    from .runtime_state import LoginStateStore

    try:
        config = build_config(base_dir=PROJECT_DIR, strict=False)
        store = LoginStateStore.from_config(config)
    except Exception:
        store = LoginStateStore(PROJECT_DIR / "runtime" / "user")
    removed = store.purge()
    for path in removed:
        print("已删除 %s" % path)
    print("共清除 %d 个登录态文件；下次运行需要重新登录" % len(removed))
    return 0


def cmd_waitlist(argv: List[str]) -> int:
    """候补模式：走 12306 官方候补渠道（spec 第 6 条）。

    用法：
        python app.py waitlist              按 WAITLIST_JSON 提交并轮询
        python app.py waitlist --simulate   离线演练：不碰网络，用模拟后端跑完整状态机
        python app.py waitlist --dry-run    只打印将要提交的表单，不发送
    """
    parser = argparse.ArgumentParser(prog="app.py waitlist")
    parser.add_argument("--simulate", action="store_true", help="用模拟后端离线跑通状态机")
    parser.add_argument("--dry-run", action="store_true", help="只打印表单，不提交")
    parser.add_argument("--max-checks", type=int, default=None)
    parser.add_argument("--once", action="store_true", help="只查一次状态就退出")
    args, _unknown = parser.parse_known_args(argv)

    from .metrics import get_store
    from .notifier import NotifyHub
    from .waitlist import (
        HttpWaitlistBackend,
        SimulatedBackend,
        WaitlistRunner,
        WaitlistRunnerConfig,
        WaitlistState,
        request_from_env,
    )

    force_utf8_console()
    load_env_file(PROJECT_DIR)

    try:
        request = request_from_env()
    except ValueError as exc:
        print("WAITLIST_JSON 配置有问题：%s" % exc, file=sys.stderr)
        return 2
    if request is None:
        print(
            "没有配置候补申请：请在 .env 里设置 WAITLIST_JSON（格式见 .env.example）。\n"
            "想先看流程可以执行：python app.py waitlist --simulate",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        print("将提交的候补表单（未发送任何请求）：")
        for key, value in sorted(request.to_form().items()):
            print("  %-22s = %s" % (key, value))
        print("\n端点：%s" % _waitlist_endpoint_summary())
        print("提醒：12306 候补端点未经实测，首次使用前请按抓包核对 WAITLIST_*_URL。")
        return 0

    hub = NotifyHub.from_env(os.environ)
    store = get_store()
    config = WaitlistRunnerConfig()
    if args.max_checks is not None:
        config.max_checks = max(1, args.max_checks)

    if args.simulate:
        backend = SimulatedBackend([WaitlistState.QUEUED, WaitlistState.QUEUED, WaitlistState.FULFILLED])
        # 演示模式：模拟脚本用尽后会停在最后一个状态，必须用小上限，否则会按真实退避
        # （90s 起步）空转很久
        config.max_checks = 6
        print("[模拟模式] 不发送任何真实请求，仅演示状态机与告警（最多 6 次查询）")
        runner = WaitlistRunner(backend, request, hub=hub, config=config, sleeper=lambda _: None)
    else:
        session = _waitlist_session()
        if session is None:
            print("需要 py12306 的请求会话，但导入失败（请先 pip install -r requirements-lock.txt）", file=sys.stderr)
            return 1
        backend = HttpWaitlistBackend(session)
        runner = WaitlistRunner(backend, request, hub=hub, config=config)
        print("端点：%s" % _waitlist_endpoint_summary())
        print("提醒：端点未经实测；若返回无法识别的状态，请对比抓包后调整 WAITLIST_*_URL。")

    _bind_waitlist_metrics(store, request.key())

    print("候补申请：%s %s->%s 车次=%s 席别=%s" % (
        request.left_date, request.left_station, request.arrive_station,
        ",".join(request.train_numbers) or "(不限)", ",".join(request.seat_types) or "(不限)",
    ))

    if args.once:
        if runner.status.state == WaitlistState.PENDING_SUBMIT:
            runner.submit()
        status = runner.check_once()
        _sync_waitlist_metrics(store, request.key(), status)
        print("当前状态：%s %s" % (status.state, status.message))
        return 0

    status = runner.run_until_terminal()
    _sync_waitlist_metrics(store, request.key(), status)
    print("最终状态：%s %s" % (status.state, status.message))
    if status.state == WaitlistState.FULFILLED:
        print("候补已兑现，请尽快到 12306 官方渠道确认支付。")
        return 0
    return 3 if status.is_terminal else 0


def _waitlist_endpoint_summary() -> str:
    from .waitlist import WaitlistEndpoints

    endpoints = WaitlistEndpoints.from_env()
    return "submit=%s query=%s cancel=%s" % (endpoints.submit, endpoints.query, endpoints.cancel)


def _waitlist_session():
    """复用上游的请求会话（带 Cookie 与 User-Agent 轮换）。"""
    try:
        from py12306.helpers.request import Request

        return Request()
    except Exception as exc:
        logger.warning("构造上游请求会话失败：%s", exc)
        return None


def _bind_waitlist_metrics(store, key: str) -> None:
    """让候补状态出现在面板的任务列表里。"""
    store.update_task(key, label="候补 " + key.split("|")[0], breaker_state="closed")


def _sync_waitlist_metrics(store, key: str, status) -> None:
    mapping = {
        "pending_submit": "no_ticket",
        "queued": "no_ticket",
        "fulfilled": "success",
        "failed": "risk_control",
        "expired": "no_ticket",
        "canceled": "no_ticket",
        "error": "server_error",
    }
    store.record_query(key, mapping.get(status.state, "no_ticket"), 0.0, label="候补")
    store.record_breaker(
        key,
        "waitlist",
        state="open" if status.state == "fulfilled" else "closed",
        reason="候补状态：%s %s" % (status.state, status.message),
        label="候补",
    )


def cmd_serve(argv: List[str]) -> int:
    """单独启动面板（不查票）。"""
    parser = argparse.ArgumentParser(prog="app.py serve")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    args, _unknown = parser.parse_known_args(argv)

    force_utf8_console()
    try:
        config, integration, _policy = setup(patch_query_loop=False)
    except Exception as exc:
        print("启动失败：%s" % exc, file=sys.stderr)
        return 1
    setup_logging(config)
    # 面板要能读到循环里累积的指标，所以后台起一个同步线程
    start_breaker_sync(integration)

    from flask import Flask

    from webpanel.view import panel

    app = Flask("py12306-panel")
    app.register_blueprint(panel)
    app.config["JSON_AS_ASCII"] = False

    host = args.host or os.environ.get("PANEL_BIND") or "127.0.0.1"
    port = args.port or int(os.environ.get("PANEL_PORT") or 8010)
    print("可视化面板：http://%s:%d/panel/  （风险提示见 README）" % (host, port))
    app.run(host=host, port=port, debug=args.debug, threaded=True)
    return 0


USAGE = """py12306-pro

用法：
    python app.py -t | --test        启动自检（不查票，退出码 0/1）
    python app.py                    上游正常抢票流程（自带面板与风控）
    python app.py serve [--port N]   只启动可视化面板
    python app.py waitlist           候补模式（官方渠道，按 WAITLIST_JSON）
    python app.py waitlist --simulate 离线演练候补状态机与告警
    python app.py --purge-login-state 清除登录态
    python app.py --version

风险提示：本工具违反 12306 服务条款，使用即承担账号被封、订单被取消的风险；
          官方候补是更稳妥的选择，应优先使用。
"""


def run_upstream(argv: List[str]) -> int:
    """走上游 main.py 的抢票流程，但先完成 core 装配。"""
    force_utf8_console()
    try:
        config, integration, _policy = setup(patch_query_loop=True)
    except Exception as exc:
        print("配置校验失败，已中止启动：\n%s" % exc, file=sys.stderr)
        return 1
    setup_logging(config)

    # 上游 main.py 自己会解析 argv（-c/--config、-t/--test），这里原样透传
    sys.argv = ["main.py"] + [a for a in argv]
    from upstream_entry import upstream_main  # 兼容两种入口布局

    return upstream_main() or 0


def main(argv: Optional[List[str]] = None) -> int:
    force_utf8_console()
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--version" in argv:
        from . import __version__

        print("py12306-pro (core %s)" % __version__)
        return 0
    if "--purge-login-state" in argv:
        return cmd_purge_login_state(argv)
    if "serve" in argv:
        return cmd_serve(argv[argv.index("serve") + 1 :])
    if "waitlist" in argv:
        return cmd_waitlist(argv[argv.index("waitlist") + 1 :])
    if "-t" in argv or "--test" in argv:
        return cmd_selfcheck(argv)
    if not argv:
        print(USAGE)
        return 0
    return run_upstream(argv)
