"""上游抢票入口（原 main.py 的内容，仅重命名以便 railkit.cli 接管命令行）。

保持原样，不要在这里加 railkit 的逻辑：装配动作在 railkit/cli.py:setup() 里，
这样「上游业务代码」和「生产化改造层」边界清晰、可分别测试。
"""

from __future__ import annotations


def upstream_main() -> int:
    import sys

    from py12306.app import App, Const, Config
    from py12306.helpers.cdn import Cdn
    from py12306.helpers.func import sleep, stay_second
    from py12306.log.common_log import CommonLog
    from py12306.query.query import Query
    from py12306.user.user import User
    from py12306.web.web import Web

    def test():
        """功能检查：账号、座位、乘客、通知等"""
        Const.IS_TEST = True
        Config.OUT_PUT_LOG_TO_FILE_ENABLED = False
        if "--test-notification" in sys.argv or "-n" in sys.argv:
            Const.IS_TEST_NOTIFICATION = True

    def load_argvs():
        if "--test" in sys.argv or "-t" in sys.argv:
            test()
        config_index = None
        if "--config" in sys.argv:
            config_index = sys.argv.index("--config")
        if "-c" in sys.argv:
            config_index = sys.argv.index("-c")
        if config_index:
            Config.CONFIG_FILE = sys.argv[config_index + 1 : config_index + 2].pop()

    load_argvs()
    CommonLog.print_welcome()
    App.run()
    CommonLog.print_configs()
    App.did_start()

    App.run_check()
    Query.check_before_run()

    Web.run()
    Cdn.run()
    User.run()
    Query.run()
    if not Const.IS_TEST:
        while True:
            sleep(10000)
    else:
        if Config().is_cluster_enabled():
            stay_second(5)  # 等待接受完集群通知
    CommonLog.print_test_complete()
    return 0
