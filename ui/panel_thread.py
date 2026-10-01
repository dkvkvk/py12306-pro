"""Web 面板的后台线程：桌面程序里点一下就能起面板，不影响界面。"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def panel_url(bind: str, port: int) -> str:
    host = "127.0.0.1" if bind in ("0.0.0.0", "::", "") else bind
    return "http://%s:%d/panel/" % (host, port)


class PanelThread(threading.Thread):
    """在后台线程里跑 Flask 面板（werkzeug 支持 threaded=True）。"""

    def __init__(self, bind: str = "127.0.0.1", port: int = 8010) -> None:
        super().__init__(name="py12306-panel", daemon=True)
        self.bind = bind or "127.0.0.1"
        self.port = int(port)
        self._server = None
        self.error: Optional[BaseException] = None

    def run(self) -> None:  # pragma: no cover - 需要网络端口，测试里单独验证
        try:
            from webpanel.server import build_app
            from werkzeug.serving import make_server

            app = build_app()
            self._server = make_server(self.bind, self.port, app, threaded=True)
            logger.info("Web 面板监听 %s:%d", self.bind, self.port)
            self._server.serve_forever()
        except Exception as exc:
            self.error = exc
            logger.exception("Web 面板启动失败：%s", exc)

    def stop(self) -> None:
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
        self._server = None
