# -*- coding: utf-8 -*-
import json
import logging
from datetime import timedelta

from flask import Flask, request
from flask_jwt_extended import (
    JWTManager)

from py12306.config import Config
from py12306.helpers.func import *


@singleton
class Web:
    session = None
    jwt = None
    log = None

    def __init__(self):
        self.session = Flask(__name__)
        self.log = logging.getLogger('werkzeug')
        self.log.setLevel(logging.ERROR)

        self.register_blueprint()
        # JWT 密钥必须来自环境变量：硬编码 'secret' 等于管理接口裸奔（spec 第 7 条）
        import os as _os
        import secrets as _secrets

        _jwt_secret = _os.environ.get('JWT_SECRET_KEY', '').strip()
        if not _jwt_secret:
            if _os.environ.get('DEV_MODE', '0').strip() in ('1', 'true', 'yes', 'on'):
                _jwt_secret = _secrets.token_urlsafe(48)
                self.log.warning('DEV_MODE=1 且未设置 JWT_SECRET_KEY，已生成临时密钥（重启即失效）')
            else:
                raise RuntimeError(
                    '缺少 JWT_SECRET_KEY 环境变量，拒绝以硬编码弱密钥启动 Web 服务。'
                    '生成方式：python -c "import secrets;print(secrets.token_urlsafe(48))"'
                )
        self.session.config['JWT_SECRET_KEY'] = _jwt_secret
        _ttl_minutes = int(_os.environ.get('JWT_TTL_MINUTES') or 720)
        self.session.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(minutes=_ttl_minutes)
        self.jwt = JWTManager(self.session)

    def register_blueprint(self):
        from py12306.web.handler.user import user
        from py12306.web.handler.stat import stat
        from py12306.web.handler.app import app
        from py12306.web.handler.query import query
        from py12306.web.handler.log import log
        # 可视化面板：独立路由 /panel，默认仅本机可访问（见 py12306/panel/view.py）
        from py12306.panel.view import panel

        # 幂等注册：Web 实例被重建时（配置变更、测试里复位单例）
        # 重复 register_blueprint 会让 Flask 抛
        # "View function mapping is overwriting an existing endpoint function"，
        # 直接导致 Web 起不来。
        for blueprint in (user, stat, app, query, log, panel):
            if blueprint.name in self.session.blueprints:
                continue
            self.session.register_blueprint(blueprint)

    @classmethod
    def run(cls):
        self = cls()
        self.start()

    def start(self):
        if not Config().WEB_ENABLE or Config().is_slave(): return
        # if Config().IS_DEBUG:
        #     self.run_session()
        # else:
        create_thread_and_run(self, 'run_session', wait=False)

    def run_session(self):
        debug = False
        if is_main_thread():
            debug = Config().IS_DEBUG
        self.session.run(debug=debug, port=Config().WEB_PORT, host='0.0.0.0')


if __name__ == '__main__':
    Web.run()
