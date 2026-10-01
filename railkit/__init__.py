"""railkit：py12306 生产化改造的 P0 层（环境 / 风控熔断 / 密钥与脱敏）。

模块划分：
    config        环境变量配置 + 启动自检式校验
    redaction     日志脱敏（统一 redact）
    notifier      统一通知接口 + 适配器（含风控告警）
    timing        抖动 / 退避纯函数
    risk          风控熔断器 + 特征识别
    runtime_state 登录态加密落盘与一键清除
"""

from __future__ import annotations

__version__ = "0.1.0-p0"

__all__ = ["__version__"]
