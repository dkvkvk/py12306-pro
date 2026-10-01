"""P0-3 配置与密钥管理。

规格书要求（3.1 / 3.2 / 3.3 / 7）：
- 所有密钥从 env.py 移到环境变量，配置文件只留占位符和注释
- JWT 密钥从环境变量读，默认值不能是硬编码弱密钥
- 任务配置要校验：字段名写错要启动即报错，而不是静默失效

设计要点：
- 纯环境变量读取 + 聚合报错（一次性列出所有问题，而不是修一个报一个）
- 配置对象只在内存中持有明文；对外有一个 scrub() 用于打印
- 真实密钥会注册进 RedactionPolicy，日志里出现也会被替换
- 不 import 任何第三方库（requests/flask/redis 都不需要），保证启动早期就能校验
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from .redaction import RedactionPolicy, redact

logger = logging.getLogger(__name__)

MIN_PYTHON = (3, 9)
RECOMMENDED_PYTHON = (3, 11)

#: QUERY_INTERVAL 低于这个值直接拒绝启动（规格书 5.1：1 秒太激进）
HARD_MIN_QUERY_INTERVAL = 1.0
RECOMMENDED_MIN_QUERY_INTERVAL = 3.0

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$")


class ConfigError(Exception):
    """配置错误：启动即失败，附全部问题清单。"""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        body = "\n".join(f"  {i}. {p}" for i, p in enumerate(self.problems, 1))
        super().__init__(f"配置校验失败，共 {len(self.problems)} 个问题：\n{body}")


class Secret(str):
    """标记为敏感的值。str 子类，能直接当字符串用；repr/日志里只显示指纹。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 展示用
        return f"<secret len={len(self)}>"

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return "<secret>"

    def reveal(self) -> str:
        """显式取出明文。调用点必须能解释为什么需要明文。"""
        return str.__str__(self)


def _secret(value: Optional[str]) -> Optional[Secret]:
    return None if value is None else Secret(value)


# --- 环境变量解析 ---------------------------------------------------------

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _get(env: Mapping[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    raw = env.get(key)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw != "" else default


def _as_bool(env: Mapping[str, str], key: str, default: bool, problems: List[str]) -> bool:
    raw = _get(env, key)
    if raw is None:
        return default
    low = raw.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    problems.append(f"{key}={raw!r} 不是合法布尔值（可选 {sorted(_TRUE | _FALSE)}）")
    return default


def _as_int(env: Mapping[str, str], key: str, default: int, problems: List[str], *, minimum: Optional[int] = None) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        problems.append(f"{key}={raw!r} 不是数字")
        return default
    if minimum is not None and value < minimum:
        problems.append(f"{key}={value} 小于最小值 {minimum}")
        return default
    return value


def _as_float(
    env: Mapping[str, str],
    key: str,
    default: float,
    problems: List[str],
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        problems.append(f"{key}={raw!r} 不是数字")
        return default
    if minimum is not None and value < minimum:
        problems.append(f"{key}={value} 小于最小值 {minimum}")
        return default
    if maximum is not None and value > maximum:
        problems.append(f"{key}={value} 大于最大值 {maximum}")
        return default
    return value


def parse_duration(raw: str) -> Optional[float]:
    """解析 '5' / '5s' / '3m' / '2h' / '1d' 为秒。"""
    m = _DURATION_RE.match(raw.strip())
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2) or "s"
    return value * {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[unit]


# --- 时间区间（规格书 5.4 的解析部分）-------------------------------------

def to_minutes(text: str) -> int:
    """'08:30' -> 510。非法格式抛 ValueError。"""
    m = _TIME_RE.match((text or "").strip())
    if not m:
        raise ValueError(f"时间 {text!r} 不是 HH:MM 格式")
    return int(m.group(1)) * 60 + int(m.group(2))


def in_period(train_time: str, frm: str, to: str) -> bool:
    """跨零点安全的时间区间判断。

    in_period('23:30', '22:00', '06:00') -> True
    in_period('12:00', '22:00', '06:00') -> False
    """
    t = to_minutes(train_time)
    a, b = to_minutes(frm), to_minutes(to)
    if a <= b:
        return a <= t <= b
    return t >= a or t <= b


def parse_periods(raw: Optional[str], problems: List[str]) -> Tuple[Tuple[int, int], ...]:
    """解析 DEPART_PERIOD / ARRIVE_PERIOD。

    接受：'22:00-06:00'、'22:00-06:00,08:00-10:00'、'08:00'（单点）
    返回分钟元组序列；空表示不限制。
    """
    text = (raw or "").strip()
    if not text or text in {"-", "*", "all"}:
        return ()
    out: List[Tuple[int, int]] = []
    for chunk in text.replace("~", "-").replace("–", "-").split(","):
        item = chunk.strip()
        if not item:
            continue
        if "-" not in item:
            try:
                mins = to_minutes(item)
            except ValueError as exc:
                problems.append(f"时间区间 {item!r} 非法：{exc}")
                continue
            out.append((mins, mins))
            continue
        left, _, right = item.partition("-")
        try:
            out.append((to_minutes(left), to_minutes(right)))
        except ValueError as exc:
            problems.append(f"时间区间 {item!r} 非法：{exc}")
    return tuple(out)


# --- 账号 -----------------------------------------------------------------


@dataclass
class Account:
    username: str
    password: Optional[Secret] = None
    login_type: str = "qr"
    note: str = ""
    south: str = ""
    options: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.password, str) and not isinstance(self.password, Secret):
            self.password = Secret(self.password)

    def as_dict(self, reveal: bool = False) -> Dict[str, Any]:
        return {
            "username": self.username,
            "password": self.password.reveal() if (reveal and self.password) else ("***" if self.password else None),
            "login_type": self.login_type,
            "note": self.note,
        }


_VALID_LOGIN_TYPES = {"qr", "sso", "password", "phone", "cookie"}


def _accounts_from_env(env: Mapping[str, str], problems: List[str]) -> List[Account]:
    raw_json = _get(env, "USER_ACCOUNTS_JSON")
    accounts: List[Account] = []
    if raw_json:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            problems.append(f"USER_ACCOUNTS_JSON 不是合法 JSON：{exc}")
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            problems.append("USER_ACCOUNTS_JSON 必须是数组，或单个对象")
            return []
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                problems.append(f"USER_ACCOUNTS_JSON[{idx}] 不是对象")
                continue
            username = str(item.get("username", "")).strip()
            if not username:
                problems.append(f"USER_ACCOUNTS_JSON[{idx}] 缺少 username")
                continue
            login_type = str(item.get("login_type", env.get("LOGIN_TYPE", "qr")))
            accounts.append(
                Account(
                    username=username,
                    password=_secret(item.get("password")),
                    login_type=login_type,
                    note=str(item.get("note", "")),
                    south=str(item.get("south", "")),
                    options=item,
                )
            )
        return accounts

    username = _get(env, "USERNAME")
    if not username:
        problems.append(
            "没有配置任何账号：请设置 USER_ACCOUNTS_JSON（推荐，可配多账号），或 USERNAME/PASSWORD/LOGIN_TYPE"
        )
        return []
    login_type = _get(env, "LOGIN_TYPE", "qr") or "qr"
    password = _secret(_get(env, "PASSWORD"))
    if login_type in {"password", "sso"} and not password:
        problems.append(f"LOGIN_TYPE={login_type} 需要提供 PASSWORD（或改用 qr 扫码登录）")
    accounts.append(Account(username=username, password=password, login_type=login_type))
    return accounts


def _validate_accounts(accounts: Sequence[Account], problems: List[str]) -> None:
    for _i, acc in enumerate(accounts):
        if acc.login_type not in _VALID_LOGIN_TYPES:
            problems.append(
                f"账号 {acc.username!r} 的 LOGIN_TYPE={acc.login_type!r} 非法，可选 {sorted(_VALID_LOGIN_TYPES)}"
            )
        if not acc.username.strip():
            problems.append("账号 username 不能为空")


# --- 分组配置 -------------------------------------------------------------


@dataclass(frozen=True)
class RuntimePaths:
    base_dir: Path
    state_dir: Path
    data_dir: Path
    log_dir: Path

    def ensure(self, *, secure: bool = True) -> None:
        for path in (self.state_dir, self.data_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)
        if secure:
            try:
                os.chmod(self.state_dir, 0o700)
            except OSError:  # pragma: no cover - Windows
                pass


@dataclass(frozen=True)
class RedisSettings:
    url: Secret
    required: bool = True

    def display(self) -> str:
        """展示串：保留 host/port/db 便于排障，密码一律打掉。

        不要直接把 URL 丢给 redact()——通用长串规则可能把整条 URL 替换成
        <secret>，那样就完全没法定位连的是哪个 Redis 了。
        """
        raw = self.url.reveal()
        try:
            parts = urlsplit(raw)
        except ValueError:
            return redact(raw)
        # 用户名保留（排障需要）、密码一律打掉
        auth = f"{parts.username}:***@" if parts.username else ""
        netloc = f"{auth}{parts.hostname or '?'}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path or "/0", "", ""))


@dataclass(frozen=True)
class QuerySettings:
    interval_seconds: float
    periods: Tuple[Tuple[int, int], ...] = ()
    arrive_periods: Tuple[Tuple[int, int], ...] = ()
    max_station_pairs: int = 5
    pre_sale_window_minutes: int = 60

    def in_dept_period(self, train_time: str) -> bool:
        if not self.periods:
            return True
        return any(in_period(train_time, f"{a // 60:02d}:{a % 60:02d}", f"{b // 60:02d}:{b % 60:02d}") for a, b in self.periods)


@dataclass(frozen=True)
class NotifySettings:
    adapters: Tuple[str, ...] = ("console",)
    dingtalk_webhook: Optional[Secret] = None
    dingtalk_secret: Optional[Secret] = None
    serverchan_key: Optional[Secret] = None
    bark_url: Optional[Secret] = None
    webhook_url: Optional[Secret] = None


@dataclass(frozen=True)
class LogSettings:
    level: str = "INFO"
    json_format: bool = False
    redact_enabled: bool = True
    redact_extra: Tuple[str, ...] = ()


@dataclass(frozen=True)
class WebSettings:
    bind: str = "127.0.0.1"
    port: int = 8008
    jwt_secret: Optional[Secret] = None
    jwt_ttl_minutes: int = 720
    allowed_ips: Tuple[str, ...] = ()
    basic_auth_user: Optional[str] = None
    basic_auth_password: Optional[Secret] = None
    enabled: bool = True


@dataclass
class Config:
    accounts: List[Account] = field(default_factory=list)
    paths: Optional[RuntimePaths] = None
    redis: Optional[RedisSettings] = None
    query: Optional[QuerySettings] = None
    notify: Optional[NotifySettings] = None
    logs: Optional[LogSettings] = None
    web: Optional[WebSettings] = None
    risk: Dict[str, Any] = field(default_factory=dict)
    enc_key: Optional[Secret] = None
    mode: str = "both"
    raw_env: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # -- 对外安全视图 --------------------------------------------------

    def scrub(self) -> Dict[str, Any]:
        """可安全打印/落盘的配置视图（所有密钥已脱敏）。"""
        return {
            "mode": self.mode,
            "accounts": [a.as_dict(reveal=False) for a in self.accounts],
            "paths": {
                "state_dir": str(self.paths.state_dir) if self.paths else None,
                "data_dir": str(self.paths.data_dir) if self.paths else None,
            },
            "redis": self.redis.display() if self.redis else None,
            "query": {
                "interval_seconds": self.query.interval_seconds,
                "periods": self.query.periods,
                "max_station_pairs": self.query.max_station_pairs,
            }
            if self.query
            else None,
            "notify_adapters": list(self.notify.adapters) if self.notify else [],
            "logs": {
                "level": self.logs.level,
                "json_format": self.logs.json_format,
                "redact_enabled": self.logs.redact_enabled,
            }
            if self.logs
            else None,
            "web": {
                "bind": self.web.bind,
                "port": self.web.port,
                "jwt_secret": "<set>" if self.web.jwt_secret else "<unset>",
                "allowed_ips": list(self.web.allowed_ips),
            }
            if self.web
            else None,
            "risk": dict(self.risk),
            "warnings": list(self.warnings),
        }

    def secrets(self) -> List[str]:
        """所有需要注册进脱敏器的明文。"""
        out: List[str] = []
        for acc in self.accounts:
            if acc.password:
                out.append(acc.password.reveal())
        if self.redis:
            out.append(self.redis.url.reveal())
        if self.notify:
            for value in (
                self.notify.dingtalk_webhook,
                self.notify.dingtalk_secret,
                self.notify.serverchan_key,
                self.notify.bark_url,
                self.notify.webhook_url,
            ):
                if value:
                    out.append(value.reveal())
        if self.web:
            if self.web.jwt_secret:
                out.append(self.web.jwt_secret.reveal())
            if self.web.basic_auth_password:
                out.append(self.web.basic_auth_password.reveal())
        if self.enc_key:
            out.append(self.enc_key.reveal())
        return out

    def redaction_policy(self) -> RedactionPolicy:
        policy = RedactionPolicy(
            extra_patterns=list(self.logs.redact_extra) if self.logs else [],
            literals=self.secrets(),
            enabled=self.logs.redact_enabled if self.logs else True,
        )
        return policy


# --- 加载 -----------------------------------------------------------------

def load_dotenv(path: Path, env: Dict[str, str], *, override: bool = False) -> bool:
    """极简 .env 解析（不引入 python-dotenv）。

    支持: KEY=value / KEY="value" / KEY='value' / # 注释 / export KEY=value
    真实环境变量优先（除非 override=True）。
    """
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读取 %s 失败：%s", path, exc)
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key or ""):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # 去掉行尾注释（仅当 # 前有空白）
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if override or key not in env or env.get(key, "") == "":
            env[key] = value
    return True


def load_legacy_env(path: Path, env: Dict[str, str]) -> bool:
    """兼容旧的 env.py：仅把其中的明文密钥搬进环境变量，其余结构化配置仍由环境变量决定。

    默认关闭（LOAD_LEGACY_ENV=0）。开启时会在日志里告警：明文密钥文件应当删除。
    """
    if not path.is_file():
        return False
    namespace: Dict[str, Any] = {}
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    except Exception as exc:
        logger.warning("解析旧 env.py 失败，已忽略：%s", exc)
        return False
    mapping = {
        "USERNAME": "USERNAME",
        "PASSWORD": "PASSWORD",
        "LOGIN_TYPE": "LOGIN_TYPE",
        "REDIS_URL": "REDIS_URL",
        "JWT_SECRET_KEY": "JWT_SECRET_KEY",
        "DINGTALK_WEBHOOK": "DINGTALK_WEBHOOK",
        "SERVERCHAN_KEY": "SERVERCHAN_KEY",
        "BARK_URL": "BARK_URL",
        "WEBHOOK_URL": "WEBHOOK_URL",
        "AUTO_CODE_ACCOUNT_USER": "AUTO_CODE_ACCOUNT_USER",
        "AUTO_CODE_ACCOUNT_PWD": "AUTO_CODE_ACCOUNT_PWD",
    }
    for src, dst in mapping.items():
        value = namespace.get(src)
        if isinstance(value, str) and value and not env.get(dst):
            env[dst] = value
    if namespace.get("USER") and not env.get("USER_ACCOUNTS_JSON"):
        users = namespace["USER"]
        if isinstance(users, (list, tuple)):
            payload = []
            for item in users:
                if isinstance(item, dict):
                    payload.append(item)
            if payload and not env.get("USER_ACCOUNTS_JSON"):
                env["USER_ACCOUNTS_JSON"] = json.dumps(payload, ensure_ascii=False)
    logger.warning(
        "%s 里存在明文密钥。已读入但请尽快迁移到环境变量并删除该文件（规格书 3.1）", path.name
    )
    return True


def _build_env(env: Optional[Mapping[str, str]] = None, base_dir: Optional[Path] = None) -> Dict[str, str]:
    merged: Dict[str, str] = dict(os.environ if env is None else env)
    root = Path(base_dir or ".").resolve()
    load_dotenv(root / ".env", merged)
    load_dotenv(root / ".env.local", merged)
    if _get(merged, "LOAD_LEGACY_ENV", "0") in _TRUE:
        load_legacy_env(root / "env.py", merged)
    _inject_defaults(merged)
    return merged


def _inject_defaults(env: Dict[str, str]) -> None:
    """把 URL 形式拆出来的片段映射成标准变量名。"""
    if not env.get("DINGTALK_WEBHOOK") and env.get("DINGTALK_URL"):
        env["DINGTALK_WEBHOOK"] = env["DINGTALK_URL"]
    if not env.get("SERVERCHAN_KEY") and env.get("SERVERCHAN_SENDKEY"):
        env["SERVERCHAN_KEY"] = env["SERVERCHAN_SENDKEY"]


def _build_paths(env: Mapping[str, str], base_dir: Path) -> RuntimePaths:
    def _path(key: str, default: str) -> Path:
        raw = _get(env, key, default) or default
        path = Path(raw)
        return path if path.is_absolute() else (base_dir / path)

    return RuntimePaths(
        base_dir=base_dir,
        state_dir=_path("RUNTIME_DIR", "runtime"),
        data_dir=_path("DATA_DIR", "data"),
        log_dir=_path("LOG_DIR", "logs"),
    )


def build_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    base_dir: Optional[Path] = None,
    strict: bool = True,
) -> Config:
    """从环境变量构建 Config。strict=True 时任何校验失败都抛 ConfigError。"""
    root = Path(base_dir or ".").resolve()
    merged = _build_env(env, root)
    problems: List[str] = []
    warnings: List[str] = []

    # Python 版本
    version = sys.version_info
    if version[:2] < MIN_PYTHON:
        problems.append(
            f"Python {version.major}.{version.minor} 已不受支持（最低 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}，"
            f"建议 {RECOMMENDED_PYTHON[0]}.{RECOMMENDED_PYTHON[1]}）——3.6 已 EOL 多年，且卡死所有依赖升级"
        )
    elif version[:2] < RECOMMENDED_PYTHON:
        warnings.append(
            f"当前 Python {version.major}.{version.minor}，建议升级到 {RECOMMENDED_PYTHON[0]}.{RECOMMENDED_PYTHON[1]}"
        )

    accounts = _accounts_from_env(merged, problems)
    _validate_accounts(accounts, problems)

    paths = _build_paths(merged, root)

    redis_url = _get(merged, "REDIS_URL", "redis://127.0.0.1:6379/0")
    if not re.match(r"^rediss?://", redis_url or ""):
        problems.append(f"REDIS_URL={redis_url!r} 必须以 redis:// 或 rediss:// 开头")

    interval = _as_float(
        merged,
        "QUERY_INTERVAL",
        4.0,
        problems,
        minimum=HARD_MIN_QUERY_INTERVAL,
        maximum=3600.0,
    )
    if interval < RECOMMENDED_MIN_QUERY_INTERVAL:
        warnings.append(
            f"QUERY_INTERVAL={interval}s 太激进（规格书 5.1 建议 3~5 秒起步）；"
            "实际请求会带 ±30% 抖动，但被风控命中的概率显著上升"
        )

    query = QuerySettings(
        interval_seconds=interval,
        periods=parse_periods(_get(merged, "DEPART_PERIOD"), problems),
        arrive_periods=parse_periods(_get(merged, "ARRIVE_PERIOD"), problems),
        max_station_pairs=_as_int(merged, "MAX_STATION_PAIRS", 5, problems, minimum=1),
        pre_sale_window_minutes=_as_int(merged, "PRE_SALE_WINDOW_MINUTES", 60, problems, minimum=0),
    )
    if query.max_station_pairs > 10:
        warnings.append(f"MAX_STATION_PAIRS={query.max_station_pairs} 偏大，查询量会成倍增长")

    adapters_raw = _get(merged, "NOTIFY_ADAPTERS", "console") or "console"
    adapters = tuple(a.strip().lower() for a in adapters_raw.split(",") if a.strip())
    notify = NotifySettings(
        adapters=adapters or ("console",),
        dingtalk_webhook=_secret(_get(merged, "DINGTALK_WEBHOOK")),
        dingtalk_secret=_secret(_get(merged, "DINGTALK_SECRET")),
        serverchan_key=_secret(_get(merged, "SERVERCHAN_KEY")),
        bark_url=_secret(_get(merged, "BARK_URL")),
        webhook_url=_secret(_get(merged, "WEBHOOK_URL")),
    )
    _validate_notify(notify, problems)

    logs = LogSettings(
        level=(_get(merged, "LOG_LEVEL", "INFO") or "INFO").upper(),
        json_format=_as_bool(merged, "LOG_JSON", False, problems),
        redact_enabled=_as_bool(merged, "LOG_REDACT", True, problems),
        redact_extra=tuple(
            p for p in (s.strip() for s in (_get(merged, "LOG_REDACT_EXTRA", "") or "").split(";")) if p
        ),
    )
    if logs.level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        problems.append(f"LOG_LEVEL={logs.level!r} 非法，可选 DEBUG/INFO/WARNING/ERROR/CRITICAL")
    if not logs.redact_enabled:
        warnings.append("LOG_REDACT=0：日志脱敏已关闭，Cookie/密码会明文落盘（规格书 3.3 不建议）")

    web = _build_web(merged, problems, warnings)

    risk = {
        "failure_threshold": _as_int(merged, "RISK_FAILURE_THRESHOLD", 3, problems, minimum=1),
        "failure_backoff_base": _as_float(merged, "RISK_FAILURE_BACKOFF_BASE", 3.0, problems, minimum=0.1),
        "failure_backoff_cap": _as_float(merged, "RISK_FAILURE_BACKOFF_CAP", 120.0, problems, minimum=1.0),
        "breaker_base": _as_float(merged, "RISK_BREAKER_BASE", 30.0, problems, minimum=1.0),
        "breaker_cap": _as_float(merged, "RISK_BREAKER_CAP", 1800.0, problems, minimum=1.0),
        "breaker_multiplier": _as_float(merged, "RISK_BREAKER_MULTIPLIER", 2.0, problems, minimum=1.0),
        "soft_risk_window": _as_float(merged, "RISK_SOFT_WINDOW", 300.0, problems, minimum=1.0),
        "soft_risk_threshold": _as_float(merged, "RISK_SOFT_THRESHOLD", 3.0, problems, minimum=1.0),
        "jitter_ratio": _as_float(merged, "RISK_JITTER_RATIO", 0.2, problems, minimum=0.0, maximum=0.9),
    }
    if risk["breaker_cap"] < risk["breaker_base"]:
        problems.append("RISK_BREAKER_CAP 不能小于 RISK_BREAKER_BASE")

    mode = (_get(merged, "CLIENT_MODE", "both") or "both").lower()
    if mode not in {"client", "server", "both"}:
        problems.append(f"CLIENT_MODE={mode!r} 非法，可选 client/server/both")

    enc_key = _secret(_get(merged, "RUNTIME_ENC_KEY"))
    if enc_key and len(enc_key.reveal()) < 16:
        problems.append("RUNTIME_ENC_KEY 至少 16 个字符（用于登录态 AES-GCM 加密）")
    if not enc_key:
        warnings.append(
            "未设置 RUNTIME_ENC_KEY：登录态将无法加密落盘。"
            "仅当你接受 runtime/ 下明文 cookie 时才用 ALLOW_PLAINTEXT_STATE=1 放行"
        )

    config = Config(
        accounts=accounts,
        paths=paths,
        redis=RedisSettings(url=Secret(redis_url or "")),
        query=query,
        notify=notify,
        logs=logs,
        web=web,
        risk=risk,
        enc_key=enc_key,
        mode=mode,
        raw_env=merged,
        warnings=warnings,
    )

    if problems and strict:
        raise ConfigError(problems)
    for w in warnings:
        logger.warning("配置告警：%s", w)
    return config


def _validate_notify(notify: NotifySettings, problems: List[str]) -> None:
    registry = {
        "dingtalk": (notify.dingtalk_webhook, "DINGTALK_WEBHOOK"),
        "serverchan": (notify.serverchan_key, "SERVERCHAN_KEY"),
        "bark": (notify.bark_url, "BARK_URL"),
        "webhook": (notify.webhook_url, "WEBHOOK_URL"),
    }
    for name in notify.adapters:
        if name not in {"console", "memory", "dingtalk", "serverchan", "bark", "webhook"}:
            problems.append(
                f"NOTIFY_ADAPTERS 里有未知适配器 {name!r}，可选 console/dingtalk/serverchan/bark/webhook"
            )
            continue
        entry = registry.get(name)
        if entry and not entry[0]:
            problems.append(f"NOTIFY_ADAPTERS 启用了 {name}，但缺少环境变量 {entry[1]}")


def _build_web(env: Mapping[str, str], problems: List[str], warnings: List[str]) -> WebSettings:
    bind = _get(env, "WEB_BIND", "127.0.0.1") or "127.0.0.1"
    port = _as_int(env, "WEB_PORT", 8008, problems, minimum=1)
    enabled = _as_bool(env, "WEB_ENABLED", True, problems)
    jwt_secret = _secret(_get(env, "JWT_SECRET_KEY"))
    dev_mode = _get(env, "DEV_MODE", "0") in _TRUE

    if enabled and not jwt_secret:
        if dev_mode:
            warnings.append("DEV_MODE=1 且未设置 JWT_SECRET_KEY：Web 接口使用临时随机密钥，重启即失效")
        else:
            problems.append(
                "缺少 JWT_SECRET_KEY（规格书 7：密钥必须来自环境变量，不能有硬编码弱默认值）。"
                "生成方式：python -c \"import secrets;print(secrets.token_urlsafe(48))\""
            )
    if jwt_secret and len(jwt_secret.reveal()) < 32 and not dev_mode:
        problems.append(f"JWT_SECRET_KEY 长度 {len(jwt_secret.reveal())} 太短，至少 32 个字符")

    allowed_raw = _get(env, "WEB_ALLOWED_IPS", "") or ""
    allowed = tuple(ip.strip() for ip in allowed_raw.split(",") if ip.strip())

    if enabled:
        if bind in {"0.0.0.0", "::"} and not allowed:
            problems.append(
                "WEB_BIND=0.0.0.0 但未设置 WEB_ALLOWED_IPS：管理界面不能裸奔公网（规格书 7）。"
                "要么改成 127.0.0.1，要么配置 IP 白名单"
            )
        elif bind not in {"127.0.0.1", "localhost", "::1"} and not allowed:
            warnings.append(f"WEB_BIND={bind} 非本机回环且无 IP 白名单，请确认网络边界")

    basic_user = _get(env, "WEB_BASIC_AUTH_USER")
    basic_pwd = _secret(_get(env, "WEB_BASIC_AUTH_PASSWORD"))
    if basic_user and not basic_pwd:
        problems.append("设置了 WEB_BASIC_AUTH_USER 但缺少 WEB_BASIC_AUTH_PASSWORD")

    return WebSettings(
        bind=bind,
        port=port,
        jwt_secret=jwt_secret,
        jwt_ttl_minutes=_as_int(env, "JWT_TTL_MINUTES", 720, problems, minimum=1),
        allowed_ips=allowed,
        basic_auth_user=basic_user,
        basic_auth_password=basic_pwd,
        enabled=enabled,
    )


def load_config(env: Optional[Mapping[str, str]] = None, *, base_dir: Optional[Path] = None) -> Config:
    """生产入口：一律 strict，配置错了就别启动。"""
    return build_config(env, base_dir=base_dir, strict=True)
