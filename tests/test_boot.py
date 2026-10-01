"""启动依赖自检：把「装完却起不来」这类问题挡在 CI 里。

引入原因：干净环境按 requirements-lock.txt 安装后，
requests-html 会 import lxml.html.clean，而 lxml>=5 已把它拆成独立包，
没有它 requests_html 一导入就 ImportError —— 上游整个挂掉，
但当时的测试全绿（因为开发机的 venv 里恰好有旧版残留）。

所以这里显式要求：
1. 上游 Web 入口与查询任务模块能 import；
2. railkit 全部子模块能 import；
3. 面板蓝图能在 Flask 应用上注册（路由存在）；
4. 上游 Config 能在没有 env.py 的环境里构造出来。
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


UPSTREAM_MODULES = [
    "py12306.app",
    "py12306.config",
    "py12306.helpers.api",
    "py12306.helpers.func",
    "py12306.helpers.request",
    "py12306.helpers.notification",
    "py12306.helpers.station",
    "py12306.query.query",
    "py12306.query.job",
    "py12306.order.order",
    "py12306.user.user",
    "py12306.user.job",
    "py12306.log.common_log",
]


class TestUpstreamImports:
    @pytest.mark.parametrize("module", UPSTREAM_MODULES)
    def test_upstream_module_imports(self, module):
        """任何一个导入失败都意味着镜像起来就崩。"""
        importlib.import_module(module)

    def test_requests_html_works(self):
        """锁文件必须包含 lxml_html_clean，否则 requests_html 直接崩。"""
        html_module = importlib.import_module("requests_html")
        assert hasattr(html_module, "HTMLSession")
        importlib.import_module("lxml.html.clean")


class TestRailkitImports:
    def test_all_railkit_submodules_import(self):
        import railkit

        names = [info.name for info in pkgutil.iter_modules(railkit.__path__)]
        assert "metrics" in names and "integration" in names and "waitlist" in names
        for name in names:
            importlib.import_module("railkit." + name)

    def test_public_api_is_callable(self):
        from railkit.config import load_config  # noqa: F401
        from railkit.integration import QueryLoopIntegration  # noqa: F401
        from railkit.metrics import MetricsStore  # noqa: F401
        from railkit.notifier import NotifyHub  # noqa: F401
        from railkit.redaction import redact  # noqa: F401
        from railkit.risk import RiskBreaker, RiskConfig  # noqa: F401
        from railkit.runtime_state import LoginStateStore  # noqa: F401
        from railkit.timing import next_query_delay  # noqa: F401
        from railkit.waitlist import WaitlistRunner  # noqa: F401

        assert RiskBreaker(RiskConfig(), key="boot-check").state == "closed"


class TestPanelBoots:
    def test_blueprint_registers_routes(self):
        from flask import Flask

        from py12306.panel.view import panel

        app = Flask("boot-check")
        app.register_blueprint(panel)
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        for expected in (
            "/panel/",
            "/panel/api/overview",
            "/panel/api/series",
            "/panel/api/health",
            "/panel/api/metrics.prom",
            "/panel/api/logs",
            "/panel/api/logs/stream",
            "/panel/api/actions/notify-test",
            "/panel/api/actions/purge-login-state",
            "/panel/favicon.png",
        ):
            assert expected in rules, "缺少路由 %s" % expected

    def test_ui_assets_exist(self):
        from py12306.panel import view

        assert view.HTML_PATH.is_file()
        assert view.FAVICON_PATH.is_file()

    def test_index_renders(self):
        from flask import Flask

        from py12306.panel.view import panel

        app = Flask("boot-check-ui")
        app.register_blueprint(panel)
        client = app.test_client()
        response = client.get("/panel/")
        assert response.status_code == 200
        assert b"py12306" in response.data


class TestUpstreamConfigBoots:
    def test_config_constructs_without_env_py(self, tmp_path, monkeypatch):
        """没有 env.py 时上游 Config 也必须能构造（否则容器起来就 AttributeError）。"""
        from py12306.config import Config

        # 上游有个后台线程在 watch CONFIG_FILE，测试里必须关掉，否则它会一直 poll 文件
        monkeypatch.setattr(Config, "watch_file_change", lambda self: None)
        monkeypatch.delattr(Config, "__it__", raising=False)
        monkeypatch.setattr(Config, "CONFIG_FILE", str(tmp_path / "env.py"))
        instance = Config()
        assert hasattr(instance, "USER_ACCOUNTS")
        assert hasattr(instance, "QUERY_JOBS")
        assert instance.QUERY_INTERVAL > 0
        assert instance.envs == []

    def test_config_bridge_overrides_upstream(self, tmp_path, monkeypatch):
        """环境变量必须能覆盖上游配置，且 int/0-1 型不会变成真值字符串。"""
        from py12306.config import Config
        from railkit.cli import ConfigBridge

        monkeypatch.setattr(Config, "watch_file_change", lambda self: None)
        monkeypatch.delattr(Config, "__it__", raising=False)
        monkeypatch.setattr(Config, "CONFIG_FILE", str(tmp_path / "env.py"))
        instance = Config()
        bridge = ConfigBridge(
            instance,
            {
                "QUERY_INTERVAL": "6",
                "DINGTALK_ENABLED": "0",
                "SERVERCHAN_ENABLED": "1",
                "USER_ACCOUNTS": '[{"username": "boot"}]',
            },
        )
        applied = bridge.apply()
        assert "QUERY_INTERVAL" in applied
        assert instance.QUERY_INTERVAL == 6
        # 必须是数值 0/1，而不是 "0"/"1" 字符串（字符串 "0" 在 if 里是真值）
        assert instance.DINGTALK_ENABLED == 0
        assert instance.SERVERCHAN_ENABLED == 1
        assert isinstance(instance.USER_ACCOUNTS, list)
        assert instance.USER_ACCOUNTS[0]["username"] == "boot"

    def test_web_refuses_weak_jwt(self, monkeypatch):
        """spec 第 7 条：不能再用硬编码弱密钥；缺失时必须拒绝启动。"""
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        monkeypatch.delenv("DEV_MODE", raising=False)
        import py12306.web.web as web_module

        monkeypatch.delattr(web_module.Web, "__it__", raising=False)
        with pytest.raises(RuntimeError) as exc:
            web_module.Web()
        assert "JWT_SECRET_KEY" in str(exc.value)

    def test_web_accepts_strong_jwt(self, monkeypatch):
        import py12306.web.web as web_module

        monkeypatch.setenv("JWT_SECRET_KEY", "k" * 48)
        monkeypatch.delattr(web_module.Web, "__it__", raising=False)
        instance = web_module.Web()
        assert instance.session.config["JWT_SECRET_KEY"] == "k" * 48

    def test_web_dev_mode_generates_temporary_key(self, monkeypatch):
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        monkeypatch.setenv("DEV_MODE", "1")
        import py12306.web.web as web_module

        monkeypatch.delattr(web_module.Web, "__it__", raising=False)
        instance = web_module.Web()
        generated = instance.session.config["JWT_SECRET_KEY"]
        assert generated != "secret" and len(generated) >= 32

    def test_web_registers_all_upstream_routes(self, monkeypatch):
        """回归：flask_jwt_extended 4.x 的 @jwt_required 不再保留 __name__，
        导致受保护路由的 endpoint 全都退化成 "wrapper"，注册第二个就抛
        'View function mapping is overwriting an existing endpoint function'。
        上游 requirements 写的是 Flask-JWT-Extended==3.15.0，升级到 4.x 后没适配。"""
        monkeypatch.setenv("JWT_SECRET_KEY", "k" * 48)
        import py12306.web.web as web_module

        monkeypatch.delattr(web_module.Web, "__it__", raising=False)
        instance = web_module.Web()
        rules = {rule.rule for rule in instance.session.url_map.iter_rules()}
        for expected in (
            "/login",
            "/users",
            "/user/info",
            "/stat/dashboard",
            "/stat/cluster",
            "/query",
            "/log/output",
            "/app/menus",
            "/app/actions",
            "/panel/",
            "/panel/api/overview",
        ):
            assert expected in rules, "缺少路由 %s（上游 Web 界面会起不来）" % expected

    def test_no_conflicting_endpoints(self, monkeypatch):
        """同一 endpoint 上不能挂两个不同的视图函数，否则 Flask 注册阶段直接抛异常。

        注意：一个视图函数被多个 @route 装饰器复用是合法的（例如 /panel/ 与
        /panel/index.html 指向同一个 index 函数），所以这里按 endpoint 分组比较
        视图函数对象，而不是简单地看 endpoint 是否出现多次。
        """
        monkeypatch.setenv("JWT_SECRET_KEY", "k" * 48)
        import py12306.web.web as web_module

        monkeypatch.delattr(web_module.Web, "__it__", raising=False)
        instance = web_module.Web()
        grouped: dict[str, set] = {}
        for rule in instance.session.url_map.iter_rules():
            view = instance.session.view_functions.get(rule.endpoint)
            grouped.setdefault(rule.endpoint, set()).add(id(view))
        conflicting = {name for name, views in grouped.items() if len(views) > 1}
        assert conflicting == set()

    def test_login_then_access_protected_route(self, monkeypatch):
        """端到端：能登录拿 token，并用它访问受保护路由。

        这条覆盖了 Flask-JWT-Extended 4.x 的两个破坏性变更：
        1) @jwt_required 必须调用（否则请求时 TypeError）；
        2) 受保护路由要显式 endpoint（否则注册阶段 AssertionError）。
        """
        monkeypatch.setenv("JWT_SECRET_KEY", "k" * 48)
        import py12306.web.web as web_module
        from py12306.config import Config

        # 直接改类属性：Web 内部的 login 处理器会用 Config() 现取，不需要自己构造单例
        monkeypatch.setattr(Config, "WEB_USER", {"username": "admin", "password": "pwd123"})
        monkeypatch.delattr(web_module.Web, "__it__", raising=False)

        web = web_module.Web()
        client = web.session.test_client()

        unauthorized = client.get("/stat/dashboard")
        assert unauthorized.status_code in (401, 422)

        login = client.post("/login", json={"username": "admin", "password": "pwd123"})
        assert login.status_code == 200, login.get_data(as_text=True)
        token = login.get_json()["access_token"]
        assert token

        headers = {"Authorization": "Bearer %s" % token}
        authorized = client.get("/stat/dashboard", headers=headers)
        assert authorized.status_code == 200, authorized.get_data(as_text=True)
        payload = authorized.get_json()
        assert "query_job_count" in payload

    def test_panel_index_alias_points_to_same_view(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "k" * 48)
        import py12306.web.web as web_module

        monkeypatch.delattr(web_module.Web, "__it__", raising=False)
        instance = web_module.Web()
        rules = {rule.rule: rule.endpoint for rule in instance.session.url_map.iter_rules()}
        assert rules["/panel/"] == rules["/panel/index.html"] == "panel.index"

    def test_web_instance_can_be_rebuilt(self, monkeypatch):
        """Web 被重建（配置变更/单例复位）时不能因为重复注册蓝图而崩。"""
        monkeypatch.setenv("JWT_SECRET_KEY", "k" * 48)
        import py12306.web.web as web_module

        first = web_module.Web()
        monkeypatch.delattr(web_module.Web, "__it__", raising=False)
        second = web_module.Web()
        assert first is not second
        assert "/panel/" in {rule.rule for rule in second.session.url_map.iter_rules()}
