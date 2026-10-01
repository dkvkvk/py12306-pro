"""运行期常量（非敏感）。

原来的 env.py 把「结构化配置」和「明文密钥」混在一起，是规格书 3.1 要拆开的对象：
- 本文件只保留非敏感常量，可以安全提交进仓库
- 密钥、账号、token 全部走环境变量，见 .env.example
"""

from __future__ import annotations

VERSION = "0.1.0-p0"

#: 12306 相关地址（公开信息，非密钥）
BASE_URL = "https://kyfw.12306.cn"
LOGIN_URL = BASE_URL + "/otn/login/conf"
QUERY_URL = BASE_URL + "/otn/leftTicket/query"
SUBMIT_ORDER_URL = BASE_URL + "/otn/confirmPassenger/confirmSingleForQueue"

#: 打码平台（密钥在环境变量 AUTO_CODE_ACCOUNT_USER / AUTO_CODE_ACCOUNT_PWD）
AUTO_CODE_PLATFORM = "free"

#: 目录常量（实际路径由 RUNTIME_DIR / DATA_DIR / LOG_DIR 覆盖）
RUNTIME_DIR = "runtime"
DATA_DIR = "data"
LOG_DIR = "logs"

#: 通知事件名（与 railkit.notifier.Event 保持一致）
EVENTS = (
    "TICKET_SUCCESS",
    "NO_TICKET",
    "LOGIN_EXPIRED",
    "CAPTCHA_FAILED",
    "RISK_CONTROL",
    "TICKET_ALL_FAILED",
    "TASK_ERROR",
    "SYSTEM",
)
