"""py12306 抢票助手 —— 程序入口。

用法:
    python app.py                 启动桌面窗口（默认）
    python app.py -t / --test     命令行自检，结果同时写到 数据目录/logs/selfcheck.txt
    python app.py serve           只启动 Web 面板（不开窗口）
    python app.py waitlist        候补模式（官方渠道）
    python app.py -h              查看全部参数

打包成 exe 后同样支持这些参数；--selfcheck 会在跑完后自动退出，
方便在无界面的环境里验证程序能否正常起来。
"""

from __future__ import annotations

import os
import sys
import time
import traceback

# 保证从任意工作目录双击/调用都能 import 到 core / ui
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import logging_setup, paths
from core.version import APP_NAME, __version__


def _install_crash_handler() -> None:
    """打包成窗口程序后没有控制台，出错必须可见：写日志文件 + 弹窗。"""

    def handler(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log_path = logging_setup.crash_log(
            "%s v%s\n时间: %s\n\n%s" % (APP_NAME, __version__, time.strftime("%Y-%m-%d %H:%M:%S"), text)
        )
        try:
            from PySide6.QtWidgets import QMessageBox

            box = QMessageBox()
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle("程序出错了")
            box.setText(
                "%s 遇到问题需要关闭。\n\n错误信息已保存到:\n%s\n\n"
                "如需帮助，请把该文件夹里的「崩溃日志」发给开发者。" % (APP_NAME, paths.logs_dir())
            )
            box.setDetailedText(text)
            box.exec()
        except Exception:
            # 连 Qt 都起不来时，至少把信息打到控制台
            print(text, file=sys.stderr)
        _ = log_path

    sys.excepthook = handler


def _run_gui(argv: list[str]) -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from core.monitor import Monitor
    from core.settings import Settings
    from core.uplink import UpstreamBridge
    from ui import theme
    from ui.main_window import MainWindow

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("py12306-pro")
    theme.apply_theme(app)

    icon = paths.resource("packaging/app_icon.ico")
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))

    settings = Settings.load()

    # 先把环境变量里的设置读进来（命令行/服务场景优先）
    from core.cli import load_env_file

    load_env_file(paths.project_root())

    logging_setup.setup(level=settings.log_level, to_console=True)

    uplink = None
    core_config = None
    try:
        from core.config import build_config

        core_config = build_config(base_dir=paths.project_root(), strict=False)
        uplink = UpstreamBridge(core_config, ignore_market_hours=True)
    except Exception as exc:
        # 配置不全不阻塞开窗：界面上会提示去补 .env
        print("配置未就绪（仍会打开窗口）：%s" % exc, file=sys.stderr)

    monitor = Monitor(uplink=uplink)
    window = MainWindow(settings=settings, monitor=monitor, uplink=uplink)
    window.show()

    selfcheck = "--selfcheck" in argv
    if selfcheck or settings.auto_start_engine:
        try:
            window._prepare_engine()
            window.engine.start()
        except Exception as exc:
            print("自动启动失败：%s" % exc, file=sys.stderr)

    if selfcheck:
        seconds = 5.0
        for index, item in enumerate(argv):
            if item == "--selfcheck" and index + 1 < len(argv):
                try:
                    seconds = max(2.0, float(argv[index + 1]))
                except ValueError:
                    pass
                break

        def report_and_quit() -> None:
            snapshot = monitor.snapshot(buckets=5)
            # 判定标准：窗口起来了 + 刷新循环在跑（任务多少取决于有没有配置）
            ok = snapshot.ts > 0
            line = "SELFCHECK %s version=%s tasks=%d engine_running=%s\n" % (
                "OK" if ok else "FAIL",
                __version__,
                snapshot.task_count,
                snapshot.engine_running,
            )
            print(line.strip())
            try:
                (paths.logs_dir() / "selfcheck.txt").write_text(line, encoding="utf-8")
            except OSError:
                pass
            window.close()
            app.exit(0 if ok else 3)

        from PySide6.QtCore import QTimer

        QTimer.singleShot(int(seconds * 1000), report_and_quit)

    return app.exec()


def main(argv: list[str] | None = None) -> int:
    _install_crash_handler()
    argv = list(sys.argv if argv is None else argv)

    # 无界面场景先处理掉，避免白起一个 Qt
    if any(a in ("-t", "--test", "serve", "waitlist") for a in argv[1:]):
        from core.cli import main as cli_main

        return cli_main(argv[1:])
    if any(a in ("-h", "--help") for a in argv[1:]) and not any(
        a in ("--selfcheck",) for a in argv[1:]
    ):
        print(__doc__)
        return 0
    if "--version" in argv[1:]:
        print("%s (v%s)" % (APP_NAME, __version__))
        return 0

    return _run_gui(argv)


if __name__ == "__main__":
    sys.exit(main())
