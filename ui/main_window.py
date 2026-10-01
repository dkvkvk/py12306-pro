"""主窗口：运行看板 / 风控事件 / 设置。

线程模型：
- 引擎（上游抢票循环）跑在 core.engine 的后台线程里；
- 界面只做两件事：QTimer 定时从 core.monitor 取快照刷新控件、把用户操作转成引擎调用。
所以界面永远不会被网络或抢票速度拖住。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor, QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QUrl

from core import paths
from core.engine import TicketEngine
from core.monitor import Monitor
from core.settings import Settings

from . import theme
from .dashboard_widgets import EventTable, MetricsChart, StatusCards, TaskTable

RISK_NOTICE = (
    "本工具违反 12306 服务条款，使用即承担账号被封、订单被取消的风险；"
    "12306 风控会识别高频请求，做了退避也无法保证不被封。官方候补功能是更稳妥的选择。"
)


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        monitor: Optional[Monitor] = None,
        engine: Optional[TicketEngine] = None,
        uplink: Any = None,
    ) -> None:
        super().__init__()
        self.settings = settings or Settings.load()
        self.uplink = uplink
        self.monitor = monitor or Monitor(engine=None, uplink=uplink)
        self.engine = engine or TicketEngine(
            on_state=self._on_engine_state,
            on_log=self._on_engine_log,
        )
        self.monitor.bind_engine(self.engine)

        self.setWindowTitle("py12306 抢票助手")
        self.resize(1180, 760)
        self._last_log_size = 0
        self._log_buffer: List[str] = []

        self._build_ui()
        self._restore_settings_to_ui()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()

        self.log_timer = QTimer(self)
        self.log_timer.setInterval(1000)
        self.log_timer.timeout.connect(self._tail_log_file)
        self.log_timer.start()

        self.refresh()

    # -- 界面搭建 ------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        layout.addWidget(self._build_toolbar())
        layout.addWidget(self._build_notice())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_dashboard_tab(), "运行看板")
        self.tabs.addTab(self._build_events_tab(), "风控事件")
        self.tabs.addTab(self._build_settings_tab(), "设置")
        layout.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.btn_start = QPushButton("开始抢票")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_stop.setEnabled(False)
        self.btn_reset = QPushButton("解除全部熔断")
        self.btn_reset.clicked.connect(self.on_reset_breakers)
        self.btn_selfcheck = QPushButton("自检")
        self.btn_selfcheck.clicked.connect(self.on_selfcheck)
        self.btn_panel = QPushButton("启动 Web 面板")
        self.btn_panel.clicked.connect(self.on_toggle_panel)
        self.btn_open_logs = QPushButton("打开日志目录")
        self.btn_open_logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir()))))

        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addWidget(self.btn_reset)
        row.addWidget(self.btn_selfcheck)
        row.addWidget(self.btn_panel)
        row.addWidget(self.btn_open_logs)
        row.addStretch(1)
        self.phase_label = QLabel("空闲")
        self.phase_label.setStyleSheet("color: %s;" % theme.COLOR_FG_DIM)
        row.addWidget(self.phase_label)
        return bar

    def _build_notice(self) -> QWidget:
        label = QLabel(RISK_NOTICE)
        label.setWordWrap(True)
        label.setStyleSheet(
            "background: rgba(210,90,20,.10); border: 1px solid #6e3b1f; color: #ffd7b5;"
            "border-radius: 8px; padding: 8px 10px; font-size: 12px;"
        )
        return label

    def _build_dashboard_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.cards = StatusCards()
        layout.addWidget(self.cards)

        splitter = QSplitter(Qt.Vertical)
        self.chart = MetricsChart()
        splitter.addWidget(self.chart)

        table_box = QGroupBox("任务组合（每个「任务 × 日期 × 车站」独立熔断与抖动）")
        table_layout = QVBoxLayout(table_box)
        self.task_table = TaskTable()
        table_layout.addWidget(self.task_table)
        splitter.addWidget(table_box)
        splitter.setSizes([320, 260])
        layout.addWidget(splitter, 1)

        self.empty_hint = QLabel(
            "还没有查询记录。填好 .env（账号/任务）后点「开始抢票」，这里会出现每个任务组合的状态。"
        )
        self.empty_hint.setStyleSheet("color: %s;" % theme.COLOR_FG_DIM)
        layout.addWidget(self.empty_hint)
        return page

    def _build_events_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        events_box = QGroupBox("风控 / 熔断事件（最近的退避与熔断记录）")
        events_layout = QVBoxLayout(events_box)
        self.event_table = EventTable()
        events_layout.addWidget(self.event_table)

        log_box = QGroupBox("实时日志（引擎与上游输出）")
        log_layout = QVBoxLayout(log_box)
        controls = QHBoxLayout()
        self.log_follow = QCheckBox("自动滚动")
        self.log_follow.setChecked(True)
        self.log_follow.stateChanged.connect(lambda: None)
        clear_btn = QPushButton("清屏")
        clear_btn.clicked.connect(lambda: (self.log_view.clear(), self._log_buffer.clear()))
        controls.addWidget(self.log_follow)
        controls.addWidget(clear_btn)
        controls.addStretch(1)
        self.log_path_label = QLabel("")
        self.log_path_label.setStyleSheet("color: %s;" % theme.COLOR_FG_DIM)
        controls.addWidget(self.log_path_label)
        log_layout.addLayout(controls)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        font = self.log_view.font()
        font.setFamilies(["Consolas", "Cascadia Mono", "monospace"])
        font.setPointSize(9)
        self.log_view.setFont(font)
        self.log_view.setMaximumBlockCount(4000)
        log_layout.addWidget(self.log_view)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(events_box)
        splitter.addWidget(log_box)
        splitter.setSizes([260, 400])
        layout.addWidget(splitter)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # 查询节奏
        pace = QGroupBox("查询节奏（越大越不容易触发风控，越小越激进）")
        pace_form = QFormLayout(pace)
        self.sp_interval = QDoubleSpinBox()
        self.sp_interval.setRange(1.0, 3600.0)
        self.sp_interval.setSingleStep(0.5)
        self.sp_interval.setSuffix(" 秒")
        self.sp_pairs = QSpinBox()
        self.sp_pairs.setRange(1, 10)
        self.sp_pairs.setSuffix(" 组")
        self.sp_pairs.setToolTip("多车站组合上限，超过会成倍放大查询量")
        pace_form.addRow("基础间隔", self.sp_interval)
        pace_form.addRow("车站组合上限", self.sp_pairs)

        # 风控
        risk = QGroupBox("风控熔断（连续异常退避、命中风控熔断）")
        risk_form = QFormLayout(risk)
        self.sp_threshold = QSpinBox()
        self.sp_threshold.setRange(1, 50)
        self.sp_threshold.setSuffix(" 次")
        self.sp_breaker_base = QSpinBox()
        self.sp_breaker_base.setRange(1, 3600)
        self.sp_breaker_base.setSuffix(" 秒")
        self.sp_breaker_cap = QSpinBox()
        self.sp_breaker_cap.setRange(1, 7200)
        self.sp_breaker_cap.setSuffix(" 秒")
        self.sp_jitter = QDoubleSpinBox()
        self.sp_jitter.setRange(0.0, 0.9)
        self.sp_jitter.setSingleStep(0.05)
        risk_form.addRow("连续失败阈值", self.sp_threshold)
        risk_form.addRow("熔断起点", self.sp_breaker_base)
        risk_form.addRow("熔断上限", self.sp_breaker_cap)
        risk_form.addRow("抖动比例", self.sp_jitter)

        # 告警
        notify = QGroupBox("告警渠道（console 永远可用；其它需要在 .env 里配置密钥）")
        notify_layout = QVBoxLayout(notify)
        self.cb_notify: Dict[str, QCheckBox] = {}
        adapters = ("console", "dingtalk", "serverchan", "bark", "webhook")
        for name in adapters:
            box = QCheckBox(name)
            self.cb_notify[name] = box
            notify_layout.addWidget(box)
        self.cb_risk_notify = QCheckBox("命中风控时告警")
        self.cb_login_notify = QCheckBox("登录态失效时告警")
        notify_layout.addWidget(self.cb_risk_notify)
        notify_layout.addWidget(self.cb_login_notify)

        # 面板与运行
        misc = QGroupBox("Web 面板与运行")
        misc_form = QFormLayout(misc)
        self.cb_panel_enabled = QCheckBox("启用 Web 面板（局域网可看）")
        self.ed_panel_bind = QLineEdit()
        self.sp_panel_port = QSpinBox()
        self.sp_panel_port.setRange(1, 65535)
        self.cb_ignore_hours = QCheckBox("忽略 12306 维护时段（凌晨也能试）")
        self.cb_debug = QCheckBox("调试模式（跳过部分校验，仅排障用）")
        self.cb_autostart = QCheckBox("启动程序后自动开始抢票")
        misc_form.addRow(self.cb_panel_enabled)
        misc_form.addRow("监听地址", self.ed_panel_bind)
        misc_form.addRow("端口", self.sp_panel_port)
        misc_form.addRow(self.cb_ignore_hours)
        misc_form.addRow(self.cb_debug)
        misc_form.addRow(self.cb_autostart)

        # 环境信息 + 操作
        env = QGroupBox("环境与账号（密钥只从 .env 读，界面不回显明文）")
        env_layout = QVBoxLayout(env)
        self.env_label = QLabel("-")
        self.env_label.setWordWrap(True)
        self.env_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        env_layout.addWidget(self.env_label)
        buttons = QHBoxLayout()
        save_btn = QPushButton("保存设置")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self.on_save_settings)
        purge_btn = QPushButton("清除登录态")
        purge_btn.setObjectName("danger")
        purge_btn.clicked.connect(self.on_purge_login)
        reload_btn = QPushButton("重新加载 .env")
        reload_btn.clicked.connect(self.on_reload_env)
        buttons.addWidget(save_btn)
        buttons.addWidget(reload_btn)
        buttons.addWidget(purge_btn)
        buttons.addStretch(1)
        env_layout.addLayout(buttons)

        layout.addWidget(pace)
        layout.addWidget(risk)
        layout.addWidget(notify)
        layout.addWidget(misc)
        layout.addWidget(env)
        layout.addStretch(1)
        return page

    # -- 设置 <-> 界面 -------------------------------------------------

    def _restore_settings_to_ui(self) -> None:
        s = self.settings
        self.sp_interval.setValue(float(s.query_interval))
        self.sp_pairs.setValue(int(s.max_station_pairs))
        self.sp_threshold.setValue(int(s.risk_failure_threshold))
        self.sp_breaker_base.setValue(int(s.risk_breaker_base))
        self.sp_breaker_cap.setValue(int(s.risk_breaker_cap))
        self.sp_jitter.setValue(float(s.risk_jitter_ratio))
        for name, box in self.cb_notify.items():
            box.setChecked(name in (s.notify_adapters or []))
        self.cb_risk_notify.setChecked(bool(s.notify_on_risk_control))
        self.cb_login_notify.setChecked(bool(s.notify_on_login_expired))
        self.cb_panel_enabled.setChecked(bool(s.panel_enabled))
        self.ed_panel_bind.setText(s.panel_bind)
        self.sp_panel_port.setValue(int(s.panel_port))
        self.cb_ignore_hours.setChecked(bool(getattr(s, "ignore_market_hours", True)))
        self.cb_debug.setChecked(bool(s.is_debug))
        self.cb_autostart.setChecked(bool(s.auto_start_engine))

    def _collect_settings_from_ui(self) -> Settings:
        s = self.settings
        s.query_interval = float(self.sp_interval.value())
        s.max_station_pairs = int(self.sp_pairs.value())
        s.risk_failure_threshold = int(self.sp_threshold.value())
        s.risk_breaker_base = float(self.sp_breaker_base.value())
        s.risk_breaker_cap = float(self.sp_breaker_cap.value())
        s.risk_jitter_ratio = float(self.sp_jitter.value())
        s.notify_adapters = [name for name, box in self.cb_notify.items() if box.isChecked()] or ["console"]
        s.notify_on_risk_control = self.cb_risk_notify.isChecked()
        s.notify_on_login_expired = self.cb_login_notify.isChecked()
        s.panel_enabled = self.cb_panel_enabled.isChecked()
        s.panel_bind = self.ed_panel_bind.text().strip() or "127.0.0.1"
        s.panel_port = int(self.sp_panel_port.value())
        s.is_debug = self.cb_debug.isChecked()
        s.auto_start_engine = self.cb_autostart.isChecked()
        s.validate()
        return s

    # -- 刷新 ----------------------------------------------------------

    def refresh(self) -> None:
        snapshot = self.monitor.snapshot(buckets=60, bucket_seconds=60.0)
        setattr(snapshot, "interval_seconds", float(self.settings.query_interval))
        self.cards.update_from(snapshot)
        self.task_table.update_from(snapshot.tasks)
        self.event_table.update_from(snapshot.breaker_events)
        self.chart.update_from(snapshot.series)

        self.phase_label.setText("%s · 已跑 %d 趟" % (snapshot.engine_phase, snapshot.engine_passes))
        self.empty_hint.setVisible(not snapshot.tasks)
        self.btn_stop.setEnabled(snapshot.engine_running)
        self.btn_start.setEnabled(not snapshot.engine_running)

        self.log_path_label.setText(snapshot.log_file or "")
        info = self.uplink.describe() if self.uplink is not None else {}
        states = info.get("login_states") or []
        self.env_label.setText(
            "数据目录：%s\n账号：%d 个　告警渠道：%s\n登录态文件：%d 个　指标库：%s"
            % (
                paths.data_root(),
                info.get("accounts", 0),
                ", ".join(info.get("notify_adapters") or []) or "-",
                len(states),
                snapshot.metrics_db or "-",
            )
        )

    # -- 引擎回调（可能来自后台线程：用 statusBar 只在主线程安全，所以这里只缓存）--

    def _on_engine_state(self, state) -> None:
        pass  # 界面靠 1 秒轮询刷新，不在跨线程回调里碰控件

    def _on_engine_log(self, message: str) -> None:
        self._log_buffer.append("[引擎] %s" % message)

    def _tail_log_file(self) -> None:
        """把上游写的日志文件增量读进界面（比任何 hook 都可靠）。"""
        lines: List[str] = []
        if self._log_buffer:
            lines.extend(self._log_buffer)
            self._log_buffer.clear()
        path = Path(self.monitor._uplink.describe().get("log_file")) if self.monitor._uplink else None
        if path and path.is_file():
            try:
                size = path.stat().st_size
                if size < self._last_log_size:  # 轮转
                    self._last_log_size = 0
                if size > self._last_log_size:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(self._last_log_size)
                        chunk = handle.read()
                        self._last_log_size = handle.tell()
                    lines.extend(chunk.splitlines())
            except OSError:
                pass
        for line in lines:
            if line.strip():
                self._append_log(line)

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)
        if self.log_follow.isChecked():
            self.log_view.moveCursor(QTextCursor.End)

    # -- 操作 ----------------------------------------------------------

    @Slot()
    def on_start(self) -> None:
        self.settings = self._collect_settings_from_ui()
        try:
            self.settings.save()
        except OSError as exc:
            QMessageBox.warning(self, "保存设置失败", str(exc))
        try:
            self._prepare_engine()
        except Exception as exc:  # 配置没填好等情况
            QMessageBox.critical(
                self,
                "无法启动",
                "%s\n\n请检查数据目录里的 .env（账号、QUERY_JOBS_JSON、JWT_SECRET_KEY 等）。\n数据目录：%s"
                % (exc, paths.data_root()),
            )
            return
        self.engine.start()
        self.statusBar().showMessage("引擎已启动", 5000)

    @Slot()
    def on_stop(self) -> None:
        self.engine.stop(timeout=0.1)
        self.statusBar().showMessage("已请求停止（等当前这一趟查询结束）", 5000)

    def _prepare_engine(self) -> None:
        """注入任务、把 core 配置推给上游。配置不全时抛异常，界面上会提示。"""
        from core.uplink import jobs_from_env

        # 先报用户能自己修的问题（任务没配对），再报内部状态问题
        jobs = jobs_from_env(dict(os_environ()))
        if not jobs:
            example = (
                '[{"job_name":"北京到上海","left_dates":["2026-10-01"],'
                '"stations":[{"left":"北京","arrive":"上海"}],'
                '"seats":["二等座"],"members":["张三"]}]'
            )
            raise RuntimeError(
                "还没有配置查询任务：请在 .env 里设置 QUERY_JOBS_JSON。\n"
                "格式示例：" + example + "\n"
                "账号密钥同样在 .env 里（USER_ACCOUNTS_JSON / JWT_SECRET_KEY / RUNTIME_ENC_KEY）。"
            )
        if self.uplink is None:
            raise RuntimeError("配置还没加载成功：请检查 .env 里的账号与密钥，然后点「重新加载 .env」")
        self.uplink.query_jobs = jobs
        self.uplink.apply()

    @Slot()
    def on_reset_breakers(self) -> None:
        from core.integration import get_integration

        integration = get_integration()
        if integration is None:
            self.statusBar().showMessage("引擎还没启动，没有可解除的熔断", 4000)
            return
        with integration._lock:
            breakers = list(integration.registry._breakers.values())
        for breaker in breakers:
            breaker.reset("manual reset from GUI")
        self.statusBar().showMessage("已解除 %d 个任务组合的熔断" % len(breakers), 5000)

    @Slot()
    def on_selfcheck(self) -> None:
        from core.selfcheck import render, run

        report = run(base_dir=paths.project_root(), skip_network=True)
        text = render(report, color=False)
        box = QMessageBox(self)
        box.setWindowTitle("自检结果（共 %d 项）" % len(report.results))
        box.setText("通过 %d/%d" % (sum(1 for r in report.results if not r.failed), len(report.results)))
        box.setDetailedText(text)
        box.setIcon(QMessageBox.Information if report.exit_code == 0 else QMessageBox.Warning)
        box.exec()

    @Slot()
    def on_toggle_panel(self) -> None:
        from .panel_thread import PanelThread, panel_url

        if getattr(self, "_panel_thread", None) is not None and self._panel_thread.isRunning():
            self._panel_thread.stop()
            self._panel_thread = None
            self.btn_panel.setText("启动 Web 面板")
            self.statusBar().showMessage("Web 面板已停止", 5000)
            return

        settings = self._collect_settings_from_ui()
        thread = PanelThread(settings.panel_bind, settings.panel_port)
        thread.start()
        self._panel_thread = thread
        url = panel_url(settings.panel_bind, settings.panel_port)
        self.btn_panel.setText("停止 Web 面板")
        QDesktopServices.openUrl(QUrl(url))
        self.statusBar().showMessage("Web 面板已启动：%s" % url, 8000)

    @Slot()
    def on_save_settings(self) -> None:
        self.settings = self._collect_settings_from_ui()
        try:
            path = self.settings.save()
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.statusBar().showMessage("设置已保存到 %s" % path, 5000)

    @Slot()
    def on_reload_env(self) -> None:
        from core.cli import load_env_file

        load_env_file(paths.project_root())
        self.statusBar().showMessage("已重新读取 .env", 4000)
        self.refresh()

    @Slot()
    def on_purge_login(self) -> None:
        if QMessageBox.question(
            self, "确认", "确定清除登录态？下次运行需要重新登录。"
        ) != QMessageBox.Yes:
            return
        count = self.monitor.purge_login_state()
        QMessageBox.information(self, "完成", "已清除 %d 个登录态文件" % count)
        self.refresh()

    # -- 关闭 ----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self.engine.is_running:
            answer = QMessageBox.question(
                self, "退出", "抢票引擎还在运行，确定退出吗？"
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.engine.stop(timeout=3.0)
        thread = getattr(self, "_panel_thread", None)
        if thread is not None and thread.isRunning():
            thread.stop()
        event.accept()


def os_environ() -> Dict[str, str]:
    import os

    return dict(os.environ)
