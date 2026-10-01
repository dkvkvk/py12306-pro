"""core.settings 与 core.paths 的测试（不需要 Qt）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core import paths  # noqa: E402
from core.settings import Settings  # noqa: E402


def _isolate(tmp: Path) -> Path:
    os.environ[paths.ENV_DATA_DIR] = str(tmp)
    paths.reset_cache()
    return tmp


def test_paths_respect_env_override(tmp_path):
    _isolate(tmp_path)
    assert paths.data_root() == tmp_path
    assert paths.logs_dir().is_dir()
    assert paths.metrics_db().parent.is_dir()
    assert paths.login_state_dir().is_dir()


def test_paths_project_root_is_repo_root():
    root = paths.project_root()
    assert (root / "app.py").is_file()
    assert (root / "core").is_dir()


def test_settings_defaults_and_roundtrip(tmp_path):
    _isolate(tmp_path)
    settings = Settings.load()
    assert settings.query_interval >= 1.0
    assert settings.notify_adapters == ["console"]

    settings.query_interval = 5.5
    settings.notify_adapters = ["console", "bark"]
    path = settings.save()
    assert path.is_file()

    again = Settings.load()
    assert again.query_interval == 5.5
    assert "bark" in again.notify_adapters


def test_settings_env_overrides_json(tmp_path, monkeypatch):
    _isolate(tmp_path)
    Settings().save()
    monkeypatch.setenv("QUERY_INTERVAL", "9")
    monkeypatch.setenv("PANEL_PORT", "9999")
    settings = Settings.load()
    assert settings.query_interval == 9.0
    assert settings.panel_port == 9999


def test_settings_clamps_out_of_range(tmp_path):
    _isolate(tmp_path)
    settings = Settings.from_dict(
        {"query_interval": 0.05, "panel_port": 99999, "log_level": "NOPE", "risk_jitter_ratio": 5}
    )
    assert settings.query_interval >= 1.0
    assert settings.panel_port <= 65535
    assert settings.log_level == "INFO"
    assert settings.risk_jitter_ratio <= 0.9


def test_settings_ignores_unknown_fields(tmp_path):
    _isolate(tmp_path)
    settings = Settings.from_dict({"不存在": 1, "query_interval": 3})
    assert settings.query_interval == 3.0


def test_settings_breaker_cap_not_below_base(tmp_path):
    _isolate(tmp_path)
    settings = Settings.from_dict({"risk_breaker_base": 100, "risk_breaker_cap": 10})
    assert settings.risk_breaker_cap >= settings.risk_breaker_base


def test_settings_to_env_is_json_free(tmp_path):
    _isolate(tmp_path)
    env = Settings().to_env()
    assert env["QUERY_INTERVAL"]
    assert "NOTIFY_ADAPTERS" in env
    # 不能把密钥写进设置
    assert not any("PASSWORD" in key or "KEY" in key for key in env)
