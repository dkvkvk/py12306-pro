"""py12306 入口（P0 部分）。

当前只实现自检：python main.py -t

自检覆盖：Python 版本、配置校验、密钥卫生、日志脱敏、目录权限、
登录态加密往返、Redis 连通性、通知适配器、风控熔断演练、抖动间隔抽样。

保留一个 app 占位，后续把业务模式接进来即可（run 数量与 -t 同）。

退出码：0 = 通过（允许有告警），1 = 有 FAIL 项。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from railkit.config import build_config
from railkit.redaction import install_everywhere
from railkit.selfcheck import render, run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="py12306（P0：环境现代化 / 风控熔断 / 密钥与脱敏）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "风险提示：本工具违反 12306 服务条款，使用即承担账号被封、订单被取消的风险；\n"
            "          官方候补功能是更稳妥的选择，应优先使用。"
        ),
    )
    parser.add_argument("-t", "--test", action="store_true", help="启动自检并退出（不查票）")
    parser.add_argument("--health-only", action="store_true", help="仅做容器健康检查所需的最小自检")
    parser.add_argument("--offline", action="store_true", help="跳过网络相关检查（不连 Redis）")
    parser.add_argument("--notify-test", action="store_true", help="额外实发一条测试通知")
    parser.add_argument("--json", dest="as_json", action="store_true", help="以 JSON 输出自检结果")
    parser.add_argument("--base-dir", default=None, help="配置根目录（默认当前目录）")
    parser.add_argument("--purge-login-state", action="store_true", help="清除 runtime 下的登录态并退出")
    parser.add_argument("--version", action="store_true", help="打印版本并退出")
    return parser


def purge_login_state(base_dir: Path) -> int:
    """一键清除登录态（规格书 3.2）。"""
    from railkit.runtime_state import LoginStateStore

    try:
        config = build_config(base_dir=base_dir, strict=False)
    except Exception as exc:  # 配置再烂也要能清登录态
        print("配置有问题，但清除登录态仍会继续：%s" % exc, file=sys.stderr)
        config = None

    if config is not None:
        store = LoginStateStore.from_config(config)
    else:
        store = LoginStateStore(base_dir / "runtime" / "user")
    removed = store.purge()
    if not removed:
        print("没有找到需要清除的登录态")
        return 0
    for path in removed:
        print("已删除 %s" % path)
    print("共清除 %d 个登录态文件；下次运行需要重新登录" % len(removed))
    return 0


def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK，中文自检输出会乱码；这里显式切成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None) -> int:
    _force_utf8_console()
    args = build_parser().parse_args(argv)
    base_dir = Path(args.base_dir).resolve() if args.base_dir else Path(os.getcwd()).resolve()

    if args.version:
        from railkit import __version__

        print("railkit %s" % __version__)
        return 0

    if args.purge_login_state:
        return purge_login_state(base_dir)

    if not args.test and not args.health_only:
        # P0 阶段只有自检；业务模式还没接上，明确提示而不是假装在跑
        print(
            "P0 阶段只提供自检：请用 python main.py -t\n"
            "（抢票/候补/Web 属于后续优先级，见 spec 第 11 节）",
            file=sys.stderr,
        )
        return 2

    report = run(
        base_dir=base_dir,
        probe_notifications=args.notify_test,
        skip_network=args.offline,
        health_only=args.health_only,
    )

    # 配置读完后立刻把脱敏装到全局，后续任何日志都走脱敏
    if report.config is not None:
        install_everywhere(report.config.redaction_policy())

    if args.as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
