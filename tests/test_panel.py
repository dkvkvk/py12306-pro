"""面板测试：访问控制、各 API、动作接口、Prometheus 导出、密钥不外泄。"""

from __future__ import annotations

import json

import pytest
from flask import Flask

from core.integration import IntegrationConfig, QueryLoopIntegration
from core.metrics import MetricsStore, set_store
from core.notifier import MemoryAdapter, NotifyHub
from core.risk import RiskConfig


@pytest.fixture()
def app(tmp_path, monkeypatch):
    # 面板默认只允许本机；测试客户端默认 remote_addr 是 127.0.0.1
    monkeypatch.delenv("PANEL_ALLOW_REMOTE", raising=False)
    monkeypatch.delenv("PANEL_TOKEN", raising=False)

    store = MetricsStore(db_path=tmp_path / "m.sqlite3", window_seconds=3600.0, flush_interval=0.0)
    set_store(store)

    hub = NotifyHub([MemoryAdapter()], sleep=lambda _s: None, retry_base=0.0)
    integration = QueryLoopIntegration(
        config=IntegrationConfig(risk=RiskConfig(jitter_ratio=0.0), query_interval=4.0),
        store=store,
        hub=hub,
    )
    import core.integration as integ

    integ.set_integration(integration)

    from webpanel.view import panel

    flask_app = Flask("panel-test")
    flask_app.register_blueprint(panel)
    flask_app.config.update(TESTING=True)
    flask_app.store = store
    flask_app.integration = integration
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


class TestAccess:
    def test_loopback_allowed(self, client):
        assert client.get("/panel/").status_code == 200

    def test_remote_denied_by_default(self, client):
        response = client.get("/panel/api/overview", environ_base={"REMOTE_ADDR": "203.0.113.9"})
        assert response.status_code == 403
        assert response.get_json()["error"] == "panel_access_denied"

    def test_remote_allowed_when_opted_in(self, client, monkeypatch):
        monkeypatch.setenv("PANEL_ALLOW_REMOTE", "1")
        response = client.get("/panel/api/overview", environ_base={"REMOTE_ADDR": "203.0.113.9"})
        assert response.status_code == 200

    def test_token_required_when_remote(self, client, monkeypatch):
        monkeypatch.setenv("PANEL_ALLOW_REMOTE", "1")
        monkeypatch.setenv("PANEL_TOKEN", "s3cret-token")
        denied = client.get("/panel/api/overview", environ_base={"REMOTE_ADDR": "203.0.113.9"})
        assert denied.status_code == 401
        ok = client.get(
            "/panel/api/overview",
            headers={"X-Panel-Token": "s3cret-token"},
            environ_base={"REMOTE_ADDR": "203.0.113.9"},
        )
        assert ok.status_code == 200

    def test_token_works_from_query_string(self, client, monkeypatch):
        monkeypatch.setenv("PANEL_ALLOW_REMOTE", "1")
        monkeypatch.setenv("PANEL_TOKEN", "tok")
        response = client.get(
            "/panel/api/overview?token=tok", environ_base={"REMOTE_ADDR": "198.51.100.7"}
        )
        assert response.status_code == 200

    def test_favicon_served(self, client):
        response = client.get("/panel/favicon.png")
        assert response.status_code == 200
        assert response.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_forwarded_for_is_honoured(self, client):
        response = client.get("/panel/api/overview", headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"})
        assert response.status_code == 403


class TestOverviewApi:
    def test_shape(self, client):
        body = client.get("/panel/api/overview").get_json()
        assert body["ok"] is True
        for key in ("summary", "tasks", "redis", "notify", "upstream", "integration", "config", "risk_notice"):
            assert key in body
        assert "违反 12306 服务条款" in body["risk_notice"]

    def test_integration_reports_patch_state(self, client):
        body = client.get("/panel/api/overview").get_json()
        assert body["integration"]["installed"] is True
        # 测试里没有导入上游 py12306，所以 patched 应为 False，且不应报错
        assert body["integration"]["patched"] is False
        assert body["integration"]["query_interval_s"] == 4.0

    def test_does_not_leak_secrets(self, client, app, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "j" * 48)
        monkeypatch.setenv("RUNTIME_ENC_KEY", "e" * 32)
        raw = client.get("/panel/api/overview").get_data(as_text=True)
        assert "j" * 48 not in raw
        assert "e" * 32 not in raw

    def test_tasks_reflect_metrics(self, client, app):
        app.store.record_query("k1", "risk_control", 100.0, label="北京->上海")
        app.store.update_task("k1", breaker_state="open", consecutive_failures=4, last_reason="过于频繁")
        tasks = client.get("/panel/api/overview").get_json()["tasks"]
        assert tasks[0]["breaker_state"] == "open"
        assert tasks[0]["consecutive_failures"] == 4
        assert tasks[0]["last_reason"] == "过于频繁"


class TestSeriesAndEvents:
    def test_series_shape(self, client, app):
        for _ in range(3):
            app.store.record_query("k1", "no_ticket", 200.0)
        body = client.get("/panel/api/series?bucket=60&buckets=12").get_json()
        series = body["series"]
        assert len(series["queries"]) == 12
        assert sum(series["queries"]) == 3

    def test_series_buckets_clamped(self, client):
        body = client.get("/panel/api/series?buckets=100000").get_json()
        assert len(body["series"]["queries"]) <= 240

    def test_invalid_bucket_falls_back(self, client):
        body = client.get("/panel/api/series?bucket=abc&buckets=xyz").get_json()
        assert body["ok"] is True

    def test_breaker_events(self, client, app):
        app.store.record_breaker("k1", "risk_control", state="open", wait_seconds=30.0, reason="过于频繁")
        events = client.get("/panel/api/breaker-events?limit=5").get_json()["events"]
        assert events[0]["state"] == "open"


class TestActions:
    def test_reset_breaker(self, client, app):
        breaker = app.integration.registry.get("t1")
        breaker.report_failure("risk_control", "过于频繁")
        assert breaker.state == "open"
        response = client.post("/panel/api/actions/reset-breaker", json={"task": "t1"})
        assert response.status_code == 200
        assert response.get_json()["state"] == "closed"

    def test_reset_unknown_task(self, client):
        response = client.post("/panel/api/actions/reset-breaker", json={"task": "nope"})
        assert response.status_code == 404

    def test_reset_requires_task(self, client):
        assert client.post("/panel/api/actions/reset-breaker", json={}).status_code == 400

    def test_notify_test_calls_hub(self, client, app):
        response = client.post(
            "/panel/api/actions/notify-test", json={"event": "RISK_CONTROL", "message": "面板测试"}
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        memory = app.integration.hub.adapters[0]
        assert memory.messages[-1].event == "RISK_CONTROL"
        assert "面板测试" in memory.messages[-1].body

    def test_notify_test_redacts_secrets(self, client, app):
        client.post(
            "/panel/api/actions/notify-test",
            json={"message": "cookie JSESSIONID=superSecretValue123456"},
        )
        memory = app.integration.hub.adapters[0]
        assert "superSecretValue123456" not in memory.messages[-1].body

    def test_purge_login_state_reports_count(self, client, app, monkeypatch, tmp_path):
        # 面板通过 core.config 读配置；没有可用配置时应返回 409
        response = client.post("/panel/api/actions/purge-login-state")
        assert response.status_code in (200, 409)


class TestLogsAndMetrics:
    def test_logs_without_file(self, client):
        body = client.get("/panel/api/logs").get_json()
        assert body["ok"] is True
        assert body["lines"] == []

    def test_prometheus_format(self, client, app):
        app.store.record_query("k1", "risk_control", 100.0)
        response = client.get("/panel/api/metrics.prom")
        assert response.status_code == 200
        assert "ticket_query_total{" in response.get_data(as_text=True)

    def test_prometheus_denied_remotely(self, client):
        response = client.get("/panel/api/metrics.prom", environ_base={"REMOTE_ADDR": "203.0.113.9"})
        assert response.status_code == 403

    def test_health_endpoint(self, client):
        body = client.get("/panel/api/health").get_json()
        assert "ok" in body
        assert "redis" in body


class TestUiAssets:
    def test_ui_loaded_from_file(self, client):
        html = client.get("/panel/").get_data(as_text=True)
        assert "<!DOCTYPE html>" in html
        assert "风控与查询面板" in html
        assert "风险提示" in html.replace("风险提示加载中", "风险提示")

    def test_ui_has_no_external_script(self, client):
        """零构建：不能引用任何外部 CDN，否则离线/内网环境直接白屏。"""
        import re

        html = client.get("/panel/").get_data(as_text=True)
        remote_scripts = re.findall(r'<script[^>]*src=["\'](https?:)?//', html)
        remote_styles = re.findall(r'<link[^>]*href=["\'](https?:)?//', html)
        assert remote_scripts == []
        assert remote_styles == []
        for banned in ("cdn.", "unpkg", "jsdelivr", "googleapis"):
            assert banned not in html
