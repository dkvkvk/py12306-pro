"""界面可改的设置：JSON 落盘到数据目录，不含任何密钥。

分工（与参考项目一致）：
- 这里放「用户会在界面上调整、且不含密码」的参数：查询节奏、风控阈值、告警渠道、面板端口；
- 账号、密码、token、JWT/加密密钥仍然只从 .env 读（见 core/config.py），界面只显示状态，
  不落盘、不回写，避免把明文密钥又写回 JSON。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from . import paths


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    """界面可改的设置。字段名与 .env 里的变量名一一对应，便于互相覆盖。"""

    # 查询节奏
    query_interval: float = 4.0
    max_station_pairs: int = 5
    pre_sale_window_minutes: int = 60

    # 风控
    risk_failure_threshold: int = 3
    risk_breaker_base: float = 30.0
    risk_breaker_cap: float = 1800.0
    risk_jitter_ratio: float = 0.2
    risk_soft_threshold: float = 3.0

    # 告警
    notify_adapters: List[str] = field(default_factory=lambda: ["console"])
    notify_on_risk_control: bool = True
    notify_on_login_expired: bool = True

    # Web 面板（保留能力，但默认不自动启动）
    panel_enabled: bool = False
    panel_bind: str = "127.0.0.1"
    panel_port: int = 8010

    # 界面
    log_level: str = "INFO"
    minimize_to_tray: bool = False
    keep_screen_on_top: bool = False

    # 引擎行为
    is_debug: bool = False
    auto_start_engine: bool = False

    # -- 读写 ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        target = Path(path) if path else paths.config_file()
        data: Dict[str, Any] = {}
        if target.is_file():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Settings":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        settings = cls()
        for key, value in (data or {}).items():
            if key not in known:
                continue  # 忽略未知字段：旧配置不炸新版本
            current = getattr(settings, key)
            if isinstance(current, bool):
                setattr(settings, key, _as_bool(value, current))
            elif isinstance(current, int):
                setattr(settings, key, _as_int(value, current))
            elif isinstance(current, float):
                setattr(settings, key, _as_float(value, current))
            elif isinstance(current, list):
                setattr(settings, key, [str(v) for v in value] if isinstance(value, (list, tuple)) else current)
            else:
                setattr(settings, key, str(value))
        settings.overlay_env(os.environ)
        settings.validate()
        return settings

    def overlay_env(self, env: Dict[str, str]) -> "Settings":
        """环境变量优先于 JSON：让命令行/服务场景能覆盖界面设置。"""
        mapping = {
            "QUERY_INTERVAL": ("query_interval", float),
            "MAX_STATION_PAIRS": ("max_station_pairs", int),
            "PRE_SALE_WINDOW_MINUTES": ("pre_sale_window_minutes", int),
            "RISK_FAILURE_THRESHOLD": ("risk_failure_threshold", int),
            "RISK_BREAKER_BASE": ("risk_breaker_base", float),
            "RISK_BREAKER_CAP": ("risk_breaker_cap", float),
            "RISK_JITTER_RATIO": ("risk_jitter_ratio", float),
            "RISK_SOFT_THRESHOLD": ("risk_soft_threshold", float),
            "PANEL_BIND": ("panel_bind", str),
            "PANEL_PORT": ("panel_port", int),
            "LOG_LEVEL": ("log_level", str),
            "DEV_MODE": ("is_debug", bool),
        }
        for env_key, (attr, kind) in mapping.items():
            raw = env.get(env_key)
            if raw is None or raw == "":
                continue
            if kind is bool:
                setattr(self, attr, _as_bool(raw, getattr(self, attr)))
            elif kind is int:
                setattr(self, attr, _as_int(raw, getattr(self, attr)))
            elif kind is float:
                setattr(self, attr, _as_float(raw, getattr(self, attr)))
            else:
                setattr(self, attr, str(raw))
        raw_adapters = env.get("NOTIFY_ADAPTERS")
        if raw_adapters:
            self.notify_adapters = [a.strip().lower() for a in raw_adapters.split(",") if a.strip()]
        return self

    def validate(self) -> "Settings":
        """把越界值拉回合法区间，并且在界面上能解释清楚为什么。"""
        self.query_interval = max(1.0, min(self.query_interval, 3600.0))
        self.max_station_pairs = max(1, min(self.max_station_pairs, 10))
        self.pre_sale_window_minutes = max(0, min(self.pre_sale_window_minutes, 240))
        self.risk_failure_threshold = max(1, self.risk_failure_threshold)
        self.risk_breaker_base = max(1.0, self.risk_breaker_base)
        self.risk_breaker_cap = max(self.risk_breaker_base, self.risk_breaker_cap)
        self.risk_jitter_ratio = max(0.0, min(self.risk_jitter_ratio, 0.9))
        self.risk_soft_threshold = max(1.0, self.risk_soft_threshold)
        self.panel_port = max(1, min(self.panel_port, 65535))
        self.log_level = (self.log_level or "INFO").upper()
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            self.log_level = "INFO"
        if not self.notify_adapters:
            self.notify_adapters = ["console"]
        return self

    def save(self, path: Path | None = None) -> Path:
        target = Path(path) if path else paths.config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
        return target

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_env(self) -> Dict[str, str]:
        """转成环境变量，供 core.config 使用（密钥不在这里）。"""
        return {
            "QUERY_INTERVAL": str(self.query_interval),
            "MAX_STATION_PAIRS": str(self.max_station_pairs),
            "PRE_SALE_WINDOW_MINUTES": str(self.pre_sale_window_minutes),
            "RISK_FAILURE_THRESHOLD": str(self.risk_failure_threshold),
            "RISK_BREAKER_BASE": str(self.risk_breaker_base),
            "RISK_BREAKER_CAP": str(self.risk_breaker_cap),
            "RISK_JITTER_RATIO": str(self.risk_jitter_ratio),
            "RISK_SOFT_THRESHOLD": str(self.risk_soft_threshold),
            "NOTIFY_ADAPTERS": ",".join(self.notify_adapters),
            "PANEL_BIND": self.panel_bind,
            "PANEL_PORT": str(self.panel_port),
            "LOG_LEVEL": self.log_level,
        }
