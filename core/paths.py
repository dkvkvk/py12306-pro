"""路径与数据目录统一管理。

与参考项目（host_app）同样的约定：
- 程序目录只放代码与样例配置，**不写用户数据**（装到 Program Files 时不可写）；
- 所有用户数据（配置、日志、指标库、登录态）放在一个数据根目录下；
- 测试或特殊场景可用环境变量 PY12306_DATA_DIR 重定向。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 数据根目录覆盖（测试用）
ENV_DATA_DIR = "PY12306_DATA_DIR"
#: 数据目录名
DATA_DIR_NAME = "py12306"

_cached: Path | None = None


def project_root() -> Path:
    """项目根目录（源码运行时是仓库根；打包后是 exe 所在目录）。"""
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource(rel: str) -> Path:
    """打包后仍可访问的随程序分发的资源（图标、样例配置、上游前端产物）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        bundled = Path(base) / rel
        if bundled.exists():
            return bundled
    return project_root() / rel


def _default_data_root() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    documents = Path.home() / "Documents"
    base = documents if documents.is_dir() else Path.home()
    return base / DATA_DIR_NAME


def data_root(*, create: bool = True) -> Path:
    """数据根目录（带进程内缓存）。"""
    global _cached
    if _cached is None:
        _cached = _default_data_root()
    if create:
        _cached.mkdir(parents=True, exist_ok=True)
    return _cached


def logs_dir(*, create: bool = True) -> Path:
    path = data_root(create=create) / "logs"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def metrics_dir(*, create: bool = True) -> Path:
    path = data_root(create=create) / "metrics"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def metrics_db() -> Path:
    """指标库（SQLite）路径。"""
    return metrics_dir() / "metrics.sqlite3"


def config_file() -> Path:
    """本程序自己的配置文件（settings.json，不含密钥）。"""
    return data_root() / "settings.json"


def env_file() -> Path:
    """密钥/账号所在的 .env 文件。"""
    return data_root() / ".env"


def runtime_dir(*, create: bool = True) -> Path:
    path = data_root(create=create) / "runtime"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def login_state_dir(*, create: bool = True) -> Path:
    """登录态目录（敏感：能直接下单的 cookie，加密落盘）。"""
    path = runtime_dir(create=create) / "user"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def reset_cache() -> None:
    """测试用：清掉缓存，让环境变量重新生效。"""
    global _cached
    _cached = None


def find_env_file(*hints: str) -> Path | None:
    """按优先级找一个可用的 .env（数据目录 > 传入候选 > 项目根）。"""
    candidates: list[Path] = [env_file()]
    candidates.extend(Path(h) for h in hints if h)
    candidates.append(project_root() / ".env")
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None
