"""上游桥接：把 core 的配置/账号推给上游 py12306，并把上游状态取回来。

为什么要有这一层：
- 上游的配置来自 env.py，我们改成「环境变量优先」；
- 上游的账号结构是 USER_ACCOUNTS（dict 列表），密钥来自 .env，需要转换；
- 上游默认会跳过凌晨时段（12306 维护），但桌面程序里用户可能就想立刻试一次，
  所以这里提供一个开关；
- 上游一些对象（Query/User）只有在初始化之后才存在，取值必须防御式处理。

界面和 Web 面板都通过 Monitor -> UpstreamBridge.describe() 读展示信息。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .config import Config as CoreConfig

logger = logging.getLogger(__name__)


def accounts_to_upstream(config: Any) -> List[Dict[str, Any]]:
    """把 core.config 的账号转成上游 USER_ACCOUNTS 需要的结构。

    上游字段（参考 env.py.example）：user_name / password / type（1=扫码 2=账号密码？）
    这里只填上游真正用到的：user_name、password、type、order_type、south。
    """
    accounts: List[Dict[str, Any]] = []
    for account in getattr(config, "accounts", []) or []:
        item: Dict[str, Any] = {"user_name": account.username}
        if account.password:
            item["password"] = account.password.reveal()
        # 上游：1 = 扫码登录（推荐），2 = 账号密码
        item["type"] = 1 if account.login_type in ("qr", "", None) else 2
        item["order_type"] = account.south or 0
        accounts.append(item)
    return accounts


def jobs_from_env(env: Dict[str, str]) -> List[Dict[str, Any]]:
    """QUERY_JOBS_JSON 解析（每次运行的任务配置）。"""
    raw = (env.get("QUERY_JOBS_JSON") or env.get("QUERY_JOBS") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("QUERY_JOBS_JSON 不是合法 JSON：%s" % exc) from None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("QUERY_JOBS_JSON 必须是数组，或单个对象")
    jobs: List[Dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError("QUERY_JOBS_JSON[%d] 不是对象" % index)
        jobs.append(item)
    return jobs


class UpstreamBridge:
    """配置/状态的双向桥接对象。"""

    def __init__(
        self,
        config: Optional[CoreConfig] = None,
        *,
        query_jobs: Optional[List[Dict[str, Any]]] = None,
        ignore_market_hours: bool = False,
    ) -> None:
        self.config = config
        self.query_jobs = list(query_jobs or [])
        self.ignore_market_hours = ignore_market_hours
        self._patched = False

    # -- 推送 ----------------------------------------------------------

    def apply(self, *, patch_market_hours: bool = True) -> None:
        """把 core 的配置推到上游 Config（必需 .env 已加载）。"""
        from py12306.config import Config as UpstreamConfig

        upstream = UpstreamConfig()

        accounts = accounts_to_upstream(self.config) if self.config else []
        if accounts:
            upstream.USER_ACCOUNTS = accounts

        if self.query_jobs:
            upstream.QUERY_JOBS = self.query_jobs

        # 单机单进程：关掉集群与 Redis 相关能力
        upstream.CLUSTER_ENABLED = 0
        upstream.WEB_ENABLE = self._panel_enabled()

        # 日志写文件（界面/面板都要读）
        upstream.OUT_PUT_LOG_TO_FILE_ENABLED = 1
        upstream.OUT_PUT_LOG_TO_FILE_PATH = str(paths.logs_dir() / "12306.log")

        # 冷门时段跳过：桌面上提供开关
        if patch_market_hours and self.ignore_market_hours:
            patch_market_hours_check()
        self._patched = True

    def _panel_enabled(self) -> bool:
        raw = os.environ.get("WEB_ENABLE")
        if raw is not None and raw != "":
            return int(float(raw)) if str(raw).replace(".", "").isdigit() else (str(raw).lower() in {"1", "true", "yes", "on"})
        return 0

    # -- 取回 ----------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        """给界面/面板用的只读状态快照。"""
        info: Dict[str, Any] = {
            "accounts": len(getattr(self.config, "accounts", []) or []),
            "notify_adapters": [],
            "log_file": "",
            "login_states": [],
            "task_count": len(self.query_jobs),
        }
        if self.config is not None:
            notify = getattr(self.config, "notify", None)
            if notify is not None:
                info["notify_adapters"] = list(notify.adapters or [])
            try:
                info["login_states"] = self._audit_login_state()
            except Exception:
                info["login_states"] = []
        try:
            from py12306.config import Config as UpstreamConfig

            info["log_file"] = getattr(UpstreamConfig(), "OUT_PUT_LOG_TO_FILE_PATH", "") or ""
        except Exception:
            info["log_file"] = str(paths.logs_dir() / "12306.log")
        return info

    def _audit_login_state(self) -> List[Dict[str, Any]]:
        from .runtime_state import LoginStateStore

        store = LoginStateStore(paths.login_state_dir(), self._secret())
        return store.audit()

    def _secret(self) -> Optional[str]:
        if self.config is None:
            return None
        key = getattr(self.config, "enc_key", None)
        return key.reveal() if key else None

    # -- 登录态 --------------------------------------------------------

    def purge_login_state(self) -> int:
        from .runtime_state import LoginStateStore

        store = LoginStateStore(paths.login_state_dir(), self._secret())
        removed = store.purge()
        logger.info("已清除 %d 个登录态文件", len(removed))
        return len(removed)


def patch_market_hours_check() -> None:
    """让上游的 app_available_check 永远放行（用户明确要求忽略维护时段）。"""
    import py12306.app as upstream_app

    if getattr(upstream_app, "_py12306_market_patched", False):
        return

    def always_available() -> bool:
        return True

    upstream_app.app_available_check = always_available
    upstream_app._py12306_market_patched = True  # type: ignore[attr-defined]
    logger.info("已忽略 12306 维护时段检查（IGNORE_MARKET_HOURS=1）")


def default_log_path() -> Path:
    return paths.logs_dir() / "12306.log"
