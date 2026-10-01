"""入口引导与候补命令测试（core.cli）。全部离线。"""

from __future__ import annotations

import json

import pytest

from core import cli
from core.metrics import MetricsStore, set_store


WAITLIST_ENV = {
    "WAITLIST_JSON": json.dumps(
        {
            "left_date": "2026-10-01",
            "left_station": "北京",
            "arrive_station": "上海",
            "train_numbers": ["G1"],
            "seat_types": ["O"],
            "passengers": [
                {
                    "passenger_name": "张三",
                    "passenger_id_no": "11010119900307721X",
                    "passenger_id_type_code": "1",
                    "passenger_type": "1",
                }
            ],
        }
    )
}


@pytest.fixture()
def isolated_store(tmp_path):
    store = MetricsStore(db_path=tmp_path / "m.sqlite3", window_seconds=3600.0, flush_interval=0.0)
    set_store(store)
    yield store
    store.close()


class TestDispatch:
    def test_version(self, capsys):
        assert cli.main(["--version"]) == 0
        assert "core" in capsys.readouterr().out

    def test_no_args_prints_usage(self, capsys):
        assert cli.main([]) == 0
        out = capsys.readouterr().out
        assert "python app.py" in out
        assert "候补" in out

    def test_usage_mentions_risk(self, capsys):
        cli.main([])
        assert "风险提示" in capsys.readouterr().out

    def test_usage_tells_user_gui_is_default(self, capsys):
        """没有命令行抢票入口，帮助里必须说清楚默认是开窗口。"""
        cli.main([])
        out = capsys.readouterr().out
        assert "启动桌面窗口" in out
        assert "run_upstream" not in out

    def test_unknown_argument_fails_loudly(self, capsys):
        """以前未知参数会走已删除的上游入口（必然 NameError），现在必须明确报错。"""
        assert cli.main(["--不存在的参数"]) == 2
        err = capsys.readouterr().err
        assert "无法识别的参数" in err
        assert "启动桌面窗口" in err


class TestWaitlistCommand:
    def test_missing_config_tells_user_what_to_do(self, monkeypatch, capsys):
        monkeypatch.delenv("WAITLIST_JSON", raising=False)
        assert cli.cmd_waitlist([]) == 2
        err = capsys.readouterr().err
        assert "WAITLIST_JSON" in err
        assert "--simulate" in err

    def test_bad_config_reports_error(self, monkeypatch, capsys):
        monkeypatch.setenv("WAITLIST_JSON", json.dumps({"left_date": "2026-10-01"}))
        assert cli.cmd_waitlist([]) == 2
        assert "缺少必填字段" in capsys.readouterr().err

    def test_dry_run_prints_form_and_endpoints(self, monkeypatch, capsys):
        for key, value in WAITLIST_ENV.items():
            monkeypatch.setenv(key, value)
        assert cli.cmd_waitlist(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "passengerTicketStr" in out
        assert "11010119900307721X" in out
        assert "kyfw.12306.cn" in out
        assert "未经实测" in out

    def test_simulate_runs_full_state_machine(self, monkeypatch, capsys, isolated_store):
        for key, value in WAITLIST_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("NOTIFY_ADAPTERS", "console")
        assert cli.cmd_waitlist(["--simulate"]) == 0
        out = capsys.readouterr().out
        assert "[模拟模式]" in out
        assert "最终状态：fulfilled" in out
        assert "确认支付" in out

    def test_simulate_records_metrics_for_panel(self, monkeypatch, isolated_store):
        for key, value in WAITLIST_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("NOTIFY_ADAPTERS", "console")
        cli.cmd_waitlist(["--simulate"])
        tasks = isolated_store.tasks()
        assert len(tasks) == 1
        assert tasks[0]["label"].startswith("候补")
        assert tasks[0]["outcome_counts"].get("success") == 1
        events = isolated_store.recent_breaker_events()
        assert any("候补状态" in item["reason"] for item in events)

    def test_simulate_is_offline(self, monkeypatch, capsys):
        """模拟模式绝不能构造上游会话（那会打网络）。"""
        for key, value in WAITLIST_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("NOTIFY_ADAPTERS", "console")

        def explode():
            raise AssertionError("模拟模式不应该构造上游请求会话")

        monkeypatch.setattr(cli, "_waitlist_session", explode)
        assert cli.cmd_waitlist(["--simulate"]) == 0


class TestConfigBridge:
    def test_aliases_cover_upstream_keys(self):
        assert cli.ConfigBridge.ALIASES["REDIS_HOST"] == "REDIS_HOST"
        assert "QUERY_JOBS" in cli.ConfigBridge.JSON_KEYS

    def test_apply_is_idempotent(self, tmp_path, monkeypatch):
        from py12306.config import Config

        monkeypatch.setattr(Config, "CONFIG_FILE", str(tmp_path / "env.py"))
        instance = Config()
        bridge = cli.ConfigBridge(instance, {"QUERY_INTERVAL": "5"})
        first = bridge.apply()
        assert "QUERY_INTERVAL" in first
        # 第二次不应再改动（值已相同）
        second = cli.ConfigBridge(instance, {"QUERY_INTERVAL": "5"}).apply()
        assert "QUERY_INTERVAL" not in second

    def test_bad_json_is_ignored_not_crash(self, tmp_path, monkeypatch):
        from py12306.config import Config

        monkeypatch.setattr(Config, "CONFIG_FILE", str(tmp_path / "env.py"))
        instance = Config()
        applied = cli.ConfigBridge(instance, {"QUERY_JOBS": "{not json"}).apply()
        assert "QUERY_JOBS" not in applied


class TestEndpointSummary:
    def test_summary_lists_all_three(self):
        summary = cli._waitlist_endpoint_summary()
        assert "submit=" in summary and "query=" in summary and "cancel=" in summary

    def test_env_override_applies(self, monkeypatch):
        monkeypatch.setenv("WAITLIST_QUERY_URL", "https://example.test/q")
        assert "https://example.test/q" in cli._waitlist_endpoint_summary()
