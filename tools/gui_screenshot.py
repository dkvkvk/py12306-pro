"""离屏截图：给 README 和回归验证用（不联网、不启动引擎）。

用法：
    python tools/gui_screenshot.py [输出路径]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


def seed_demo(store) -> None:
    """灌入仿真指标，让看板有内容可看。"""
    import random
    import time as _time

    random.seed(20261001)
    tasks = [
        "G1234 北京->上海|2026-10-01|北京-上海",
        "G1234 北京->上海|2026-10-02|北京-上海",
        "G88 上海->杭州|2026-10-01|上海-杭州",
    ]
    labels = ["北京->上海 10-01", "北京->上海 10-02", "上海->杭州 10-01"]
    now = _time.time()
    outcomes = ["no_ticket"] * 6 + ["ticket_found", "server_error", "timeout", "risk_control"]
    for index, key in enumerate(tasks):
        for step in range(120):
            outcome = random.choice(outcomes)
            store.record_query(
                key,
                outcome,
                duration_ms=max(90.0, random.gauss(420, 170)),
                station=labels[index],
                label=labels[index],
                ts=now - (120 - step) * 12,
            )
        store.update_task(
            key,
            label=labels[index],
            breaker_state="open" if index == 0 else "closed",
            consecutive_failures=3 if index == 0 else 0,
            open_count=2 if index == 0 else 0,
            next_probe_in=18.0 if index == 0 else 0.0,
            soft_risk_score=1.5 if index == 0 else 0.0,
            last_reason="连续 3 次异常：HTTP 503" if index == 0 else "",
        )
    for index, key in enumerate(tasks[:2]):
        store.record_breaker(
            key,
            "risk_control",
            previous_state="closed",
            state="open",
            wait_seconds=30.0 * (2 ** index),
            reason="HTTP 200 + 您的访问过于频繁",
            label=labels[index],
            ts=now - 60 * (index + 1),
        )


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tools" / "gui-screenshot.png"
    data_dir = Path(os.environ.get("PY12306_DATA_DIR", ROOT / ".gui-shot"))
    os.environ["PY12306_DATA_DIR"] = str(data_dir)

    from core.metrics import MetricsStore, set_store
    from core.paths import metrics_db

    store = MetricsStore(db_path=metrics_db(), window_seconds=7200.0, flush_interval=0.0)
    set_store(store)
    seed_demo(store)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from core.settings import Settings
    from core.monitor import Monitor
    from ui import theme
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    theme.apply_theme(app)

    settings = Settings.load()
    monitor = Monitor(store=store)
    window = MainWindow(settings=settings, monitor=monitor, uplink=None)
    window.refresh()
    window.resize(1180, 780)
    window.show()

    def snap() -> None:
        window.refresh()
        pixmap = window.grab()
        out.parent.mkdir(parents=True, exist_ok=True)
        ok = pixmap.save(str(out), "PNG")
        print("截图:", out, "成功:", ok, "尺寸:", pixmap.width(), "x", pixmap.height())
        # 顺便报告关键控件是否有内容，便于自动校验
        print("卡片数:", len(window.cards.cards), "任务行:", window.task_table.rowCount(), "事件行:", window.event_table.rowCount())
        window.close()
        app.quit()

    QTimer.singleShot(600, snap)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
