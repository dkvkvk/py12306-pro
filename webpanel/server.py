"""Web 面板的应用工厂。

桌面程序（ui/panel_thread.py）与命令行（app.py serve）都走这里，
保证两处启动的面板行为完全一致。
"""

from __future__ import annotations

from typing import Optional

from flask import Flask


def build_app(*, name: str = "py12306-panel") -> Flask:
    """组装 Flask 应用（注册面板蓝图）。"""
    from .view import panel

    app = Flask(name)
    app.register_blueprint(panel)
    app.config["JSON_AS_ASCII"] = False
    return app


def run(host: str = "127.0.0.1", port: int = 8010, *, debug: bool = False, threaded: bool = True) -> None:
    build_app().run(host=host, port=port, debug=debug, threaded=threaded)
