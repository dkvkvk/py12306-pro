"""日志初始化：控制台 + 文件（带脱敏）。

与参考项目一致的分工：日志写到数据目录的 logs/ 下，程序目录保持干净。
抢票引擎是后台线程、日志量大，所以按大小轮转（默认 2MB × 3）。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Optional

from . import paths
from .redaction import install_everywhere

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup(
    *,
    level: str = "INFO",
    log_file: Optional[Path] = None,
    to_console: bool = True,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
) -> Path:
    """配置根 logger，返回实际写入的日志文件路径。"""
    global _configured
    target = Path(log_file) if log_file else paths.logs_dir() / "py12306.log"
    target.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric)

    # 重复调用时先清掉本模块装的 handler，避免日志重复输出
    for handler in list(root.handlers):
        if getattr(handler, "_py12306", False):
            root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(numeric)
    file_handler._py12306 = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    if to_console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        stream.setLevel(numeric)
        stream._py12306 = True  # type: ignore[attr-defined]
        root.addHandler(stream)

    # 脱敏必须挂在 root 与所有 handler 上（只挂 logger 会漏掉 propagate 的日志）
    install_everywhere()
    _configured = True
    return target


def is_configured() -> bool:
    return _configured


def crash_log(exc_text: str) -> Path:
    """把未捕获异常写到数据目录（打包成窗口程序后没有控制台，这是唯一线索）。"""
    target = paths.logs_dir() / "崩溃日志_%s.txt" % time.strftime("%Y%m%d_%H%M%S")
    try:
        target.write_text(exc_text, encoding="utf-8")
    except OSError:
        pass
    return target
