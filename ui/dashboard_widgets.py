"""看板部件：状态卡、曲线图、任务表、事件表。

只做展示：所有数据都来自 core.monitor.Snapshot，部件本身不碰上游对象。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme

pg.setConfigOptions(antialias=True, background=theme.COLOR_BG, foreground=theme.COLOR_FG_DIM)


def _fmt_number(value: Any, digits: int = 0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return ("%%.%df" % digits) % number


def _fmt_ago(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = float(seconds)
    if seconds < 60:
        return "%.0f 秒前" % seconds
    if seconds < 3600:
        return "%.1f 分钟前" % (seconds / 60)
    return "%.1f 小时前" % (seconds / 3600)


class StatusCard(QFrame):
    """一张指标卡：标题 + 大数字 + 小字说明。"""

    def __init__(self, title: str, value: str = "-", foot: str = "", color: str = "") -> None:
        super().__init__()
        self.setObjectName("statusCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(
            "#statusCard { background: %s; border: 1px solid %s; border-radius: 10px; }"
            % (theme.COLOR_BG_SOFT, theme.COLOR_LINE)
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("color: %s; font-size: 12px;" % theme.COLOR_FG_DIM)
        self.value_label = QLabel(value)
        value_font = QFont(self.value_label.font())
        value_font.setPointSize(value_font.pointSize() + 6)
        value_font.setBold(True)
        self.value_label.setFont(value_font)
        self.foot_label = QLabel(foot)
        self.foot_label.setStyleSheet("color: %s; font-size: 11px;" % theme.COLOR_FG_DIM)

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.foot_label)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        self.value_label.setStyleSheet("color: %s;" % (color or theme.COLOR_FG))

    def update_value(self, value: str, foot: str = "", color: str = "") -> None:
        self.value_label.setText(value)
        if foot is not None:
            self.foot_label.setText(foot)
        if color:
            self.set_color(color)


class StatusCards(QWidget):
    """一排指标卡（内容与网页面板保持一致，两处对照不会困惑）。"""

    FIELDS = (
        ("qpm", "查询 / 分钟", 2),
        ("queries", "累计查询", 0),
        ("tickets", "有票 / 成功", 0),
        ("risk", "风控命中", 0),
        ("latency", "延迟 p50 / p90", 0),
        ("breaker", "熔断中任务", 0),
        ("interval", "基础间隔", 1),
        ("uptime", "运行时长", 0),
    )

    def __init__(self) -> None:
        super().__init__()
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.cards: Dict[str, StatusCard] = {}
        for index, (key, title, _digits) in enumerate(self.FIELDS):
            card = StatusCard(title)
            self.cards[key] = card
            layout.addWidget(card, index // 4, index % 4)

    def update_from(self, snapshot) -> None:
        open_tasks = snapshot.open_tasks
        breaker_color = theme.COLOR_BAD if open_tasks else theme.COLOR_OK
        self.cards["qpm"].update_value(_fmt_number(snapshot.queries_per_minute, 2), "窗口内 %d 次" % snapshot.queries_in_window, theme.COLOR_INFO)
        self.cards["queries"].update_value(_fmt_number(snapshot.queries_total), "共 %d 个任务组合" % snapshot.task_count)
        self.cards["tickets"].update_value(
            "%d / %d" % (snapshot.tickets_total, snapshot.success_total),
            "下单成功 %d" % snapshot.success_total,
            theme.COLOR_OK if snapshot.tickets_total else "",
        )
        self.cards["risk"].update_value(
            _fmt_number(snapshot.risk_control_total),
            "熔断触发 %d 次" % snapshot.circuit_open_total,
            theme.COLOR_BAD if snapshot.risk_control_total else theme.COLOR_OK,
        )
        self.cards["latency"].update_value(
            "%s / %s" % (_fmt_number(snapshot.latency_p50), _fmt_number(snapshot.latency_p90)),
            "p99 %s ms · 最大 %s" % (_fmt_number(snapshot.latency_p99), _fmt_number(snapshot.latency_max)),
        )
        self.cards["breaker"].update_value(
            "%d / %d" % (open_tasks, snapshot.task_count), "退避或熔断中", breaker_color
        )
        self.cards["interval"].update_value("%ss" % _fmt_number(snapshot_interval_seconds(snapshot), 1))
        self.cards["uptime"].update_value(_fmt_duration(snapshot.engine_uptime), snapshot.engine_phase)


def snapshot_interval_seconds(snapshot) -> float:
    """工程间隔不在 Snapshot 里时退回 0；由主窗口把设置值写进 Snapshot 更方便。"""
    return getattr(snapshot, "interval_seconds", 0.0) or 0.0


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0.0, seconds or 0.0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d 小时 %d 分" % (hours, minutes)
    if minutes:
        return "%d 分 %d 秒" % (minutes, secs)
    return "%d 秒" % secs


class MetricsChart(QWidget):
    """上：查询量 / 风控命中 / 有票（柱状，按分钟分桶）；下：延迟 p50/p90（折线）。"""

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.activity = pg.PlotWidget()
        self.activity.setMinimumHeight(150)
        self.activity.showGrid(x=False, y=True, alpha=0.25)
        self.activity.setLabel("left", "次数")
        self.activity.addLegend(offset=(-10, 8), labelTextColor=theme.COLOR_FG_DIM)
        self.bar_queries = pg.BarGraphItem(x=[], height=[], width=0.7, brush=pg.mkBrush(theme.COLOR_INFO), pen=None, name="查询")
        self.bar_risk = pg.BarGraphItem(x=[], height=[], width=0.7, brush=pg.mkBrush(theme.COLOR_BAD), pen=None, name="风控")
        self.bar_tickets = pg.BarGraphItem(x=[], height=[], width=0.7, brush=pg.mkBrush(theme.COLOR_OK), pen=None, name="有票")
        for item in (self.bar_queries, self.bar_risk, self.bar_tickets):
            self.activity.addItem(item)

        self.latency = pg.PlotWidget()
        self.latency.setMinimumHeight(120)
        self.latency.showGrid(x=False, y=True, alpha=0.25)
        self.latency.setLabel("left", "毫秒")
        self.latency.addLegend(offset=(-10, 8), labelTextColor=theme.COLOR_FG_DIM)
        self.curve_p50 = self.latency.plot([], [], pen=pg.mkPen(theme.COLOR_WARN, width=2), name="p50")
        self.curve_p90 = self.latency.plot([], [], pen=pg.mkPen(theme.COLOR_ACCENT, width=2), name="p90")

        layout.addWidget(self.activity)
        layout.addWidget(self.latency)

    def update_from(self, series: Dict[str, Any]) -> None:
        queries = list(series.get("queries") or [])
        if not queries:
            return
        xs = list(range(len(queries)))
        self.bar_queries.setOpts(x=xs, height=queries)
        self.bar_risk.setOpts(x=xs, height=list(series.get("risk_control") or [0] * len(xs)))
        self.bar_tickets.setOpts(x=xs, height=list(series.get("tickets") or [0] * len(xs)))
        latency = list(series.get("latency_ms") or [])
        self.curve_p50.setData(xs, [item.get("p50", 0) for item in latency])
        self.curve_p90.setData(xs, [item.get("p90", 0) for item in latency])
        bucket = series.get("bucket_seconds") or 60
        self.activity.setLabel("bottom", "最近 %d 分钟" % int(len(xs) * bucket / 60))


class TaskTable(QTableWidget):
    """任务组合表：熔断状态、查询次数、连续失败、下次探针、有票率、最近原因。"""

    HEADERS = (
        ("label", "任务组合"),
        ("breaker_state", "熔断状态"),
        ("queries", "查询"),
        ("tickets", "有票"),
        ("risk_hits", "风控"),
        ("consecutive_failures", "连续失败"),
        ("next_probe_in", "下次探针"),
        ("success_rate", "有票率"),
        ("last_query_ago", "上次查询"),
        ("last_outcome", "最近结果"),
        ("last_reason", "最近原因"),
    )

    def __init__(self) -> None:
        super().__init__(0, len(self.HEADERS))
        self.setHorizontalHeaderLabels([title for _, title in self.HEADERS])
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSortingEnabled(True)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(len(self.HEADERS) - 1, QHeaderView.Stretch)

    def update_from(self, tasks: List[Any]) -> None:
        # 排序开着的时候直接改内容会乱序，先关掉
        self.setSortingEnabled(False)
        self.setRowCount(len(tasks))
        state_names = {"closed": "正常", "backoff": "退避中", "open": "已熔断", "half_open": "探针中"}
        for row, task in enumerate(tasks):
            values = (
                task.label or task.task,
                state_names.get(task.breaker_state, task.breaker_state),
                str(task.queries),
                str(task.tickets),
                str(task.risk_hits),
                str(task.consecutive_failures),
                ("%.0f 秒" % task.next_probe_in) if task.next_probe_in else "-",
                "%.1f%%" % task.success_rate,
                _fmt_ago(task.last_query_ago),
                task.last_outcome or "-",
                task.last_reason or "-",
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column != 0:
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 1:
                    item.setForeground(QColor(theme.state_color(task.breaker_state)))
                if column == 10 and task.last_reason:
                    item.setForeground(QColor(theme.COLOR_BAD if task.breaker_state != "closed" else theme.COLOR_FG_DIM))
                self.setItem(row, column, item)
        self.setSortingEnabled(True)


class EventTable(QTableWidget):
    """风控 / 熔断事件表。"""

    HEADERS = ("时间", "任务组合", "分类", "状态", "等待", "原因")

    def __init__(self) -> None:
        super().__init__(0, len(self.HEADERS))
        self.setHorizontalHeaderLabels(list(self.HEADERS))
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(len(self.HEADERS) - 1, QHeaderView.Stretch)

    def update_from(self, events: List[Dict[str, Any]]) -> None:
        import time as _time

        self.setRowCount(len(events))
        for row, event in enumerate(events):
            stamp = event.get("ts")
            when = _time.strftime("%H:%M:%S", _time.localtime(stamp)) if stamp else "-"
            values = (
                when,
                str(event.get("task") or ""),
                str(event.get("category") or ""),
                str(event.get("state") or ""),
                "%.0f 秒" % float(event.get("wait_seconds") or 0.0),
                str(event.get("reason") or ""),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 3:
                    item.setForeground(QColor(theme.state_color(text)))
                self.setItem(row, column, item)
