"""core：与界面无关的业务层（可单独测试，不依赖 Qt / Flask）。

模块划分：
    paths            路径与数据目录管理
    version          软件名称与版本
    logging_setup    日志初始化（含脱敏）
    config           环境变量配置 + 启动校验
    settings         界面可改的设置（JSON 落盘）
    redaction        日志脱敏
    notifier         统一通知接口 + 适配器
    timing           抖动 / 退避纯函数
    risk             风控熔断器 + 特征识别
    metrics          指标库（面板与看板共用）
    integration      把熔断/抖动接进上游查询循环
    runtime_state    登录态加密落盘与清除
    waitlist         候补模式
    selfcheck        自检
    cli              命令行分发（自检 / 候补 / 上游抢票）
"""

from __future__ import annotations

from .version import APP_NAME, APP_NAME_EN, __version__

__all__ = ["APP_NAME", "APP_NAME_EN", "__version__"]
