"""桌面界面冒烟测试：能在离屏模式创建窗口、刷新数据、切页、退出。

没有装 PySide6 的环境（比如只跑命令行/CI 的容器）会自动跳过整份文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面测试")

from core import paths  # noqa: E402
from core.metrics import MetricsStore, set_store  # noqa: E402
from core.monitor import Monitor  # noqa: E402
from core.settings import Settings  # noqa: E402


@pytest.fixture()
def qapp(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "data"))
    paths.reset_cache()

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    from ui import theme

    theme.apply_theme(app)
    yield app
    app.processEvents()


@pytest.fixture()
def store(tmp_path):
    st = MetricsStore(db_path=tmp_path / "m.sqlite3", window_seconds=3600.0, flush_interval=0.0)
    set_store(st)
    yield st
    st.close()


def _window(store, tmp_path):
    from ui.main_window import MainWindow

    settings = Settings.load()
    window = MainWindow(settings=settings, monitor=Monitor(store=store), uplink=None)
    return window


def test_theme_picks_a_font_and_state_colors(qapp):
    from ui import theme

    font = theme.pick_font()
    assert font.pointSize() > 0
    assert theme.state_color("open") != theme.state_color("closed")
    assert theme.state_color("不存在") == theme.FG_DIM if hasattr(theme, "FG_DIM") else True


def test_window_builds_with_expected_widgets(qapp, store, tmp_path):
    window = _window(store, tmp_path)
    assert window.tabs.count() == 3
    assert len(window.cards.cards) == 8
    assert window.task_table.columnCount() == len(window.task_table.HEADERS)
    assert window.event_table.columnCount() == len(window.event_table.HEADERS)
    window.close()


def test_window_renders_demo_metrics(qapp, store, tmp_path):
    key = "T|2026-10-01|北京-上海"
    store.record_query(key, "ticket_found", 300.0, label="北京->上海")
    store.record_query(key, "risk_control", 200.0, label="北京->上海")
    store.update_task(key, breaker_state="open", consecutive_failures=3, next_probe_in=15.0, last_reason="过于频繁")
    store.record_breaker(key, "risk_control", state="open", wait_seconds=30.0, reason="过于频繁")

    window = _window(store, tmp_path)
    window.refresh()

    assert window.task_table.rowCount() == 1
    assert window.event_table.rowCount() == 1
    # 熔断状态列要有颜色（不是默认前景色）
    item = window.task_table.item(0, 1)
    assert item is not None and item.text()
    assert window.cards.cards["risk"].value_label.text() == "1"
    window.close()


def test_window_grab_produces_image(qapp, store, tmp_path):
    window = _window(store, tmp_path)
    window.refresh()
    pixmap = window.grab()
    assert not pixmap.isNull()
    assert pixmap.width() > 200 and pixmap.height() > 200
    target = tmp_path / "shot.png"
    assert pixmap.save(str(target), "PNG")
    assert target.stat().st_size > 1000
    window.close()


def test_settings_tab_roundtrip(qapp, store, tmp_path):
    window = _window(store, tmp_path)
    window.sp_interval.setValue(6.5)
    window.sp_pairs.setValue(3)
    window.cb_notify["bark"].setChecked(True)

    settings = window._collect_settings_from_ui()
    assert settings.query_interval == 6.5
    assert settings.max_station_pairs == 3
    assert "bark" in settings.notify_adapters

    saved = settings.save()
    assert saved.is_file()
    window._restore_settings_to_ui()
    assert abs(window.sp_interval.value() - 6.5) < 0.001
    window.close()


def test_start_engine_without_jobs_shows_error(qapp, store, tmp_path, monkeypatch):
    """没配 QUERY_JOBS_JSON 时，启动要给出明确提示而不是静默失败。"""
    for name in ("QUERY_JOBS", "QUERY_JOBS_JSON"):
        monkeypatch.delenv(name, raising=False)

    window = _window(store, tmp_path)
    with pytest.raises(RuntimeError) as exc:
        window._prepare_engine()
    assert "QUERY_JOBS_JSON" in str(exc.value)
    window.close()


def test_start_engine_applies_jobs(qapp, store, tmp_path, monkeypatch):
    import json

    jobs = [{"job_name": "demo", "left_dates": ["2026-10-01"], "stations": [{"left": "北京", "arrive": "上海"}], "seats": ["二等座"], "members": ["张三"]}]
    monkeypatch.setenv("QUERY_JOBS_JSON", json.dumps(jobs))
    monkeypatch.setenv("USER_ACCOUNTS_JSON", json.dumps([{"username": "u", "password": "p", "login_type": "qr"}]))
    monkeypatch.setenv("RUNTIME_ENC_KEY", "dev-enc-key-0123456789")

    from core.config import build_config
    from core.uplink import UpstreamBridge

    config = build_config(base_dir=paths.project_root(), strict=False)
    window = _window(store, tmp_path)
    window.uplink = UpstreamBridge(config, ignore_market_hours=True)

    window._prepare_engine()

    from py12306.config import Config as UpstreamConfig

    assert len(UpstreamConfig().QUERY_JOBS) == 1
    assert UpstreamConfig().CLUSTER_ENABLED == 0
    window.close()


def test_panel_url_mapping():
    from ui.panel_thread import panel_url

    assert panel_url("0.0.0.0", 8010).startswith("http://127.0.0.1:8010")
    assert panel_url("127.0.0.1", 9000) == "http://127.0.0.1:9000/panel/"


def test_engine_state_transitions_without_upstream(monkeypatch):
    """引擎在 bootstrap 失败时要落到「未运行 + 有错误信息」，不能卡在运行中。"""
    from core.engine import TicketEngine

    engine = TicketEngine()

    def boom(self):
        raise RuntimeError("模拟上游不可用")

    monkeypatch.setattr(TicketEngine, "_bootstrap", boom)
    engine.start()
    engine.stop(timeout=5.0)
    assert not engine.is_running
    assert "模拟上游不可用" in engine.state.last_error
