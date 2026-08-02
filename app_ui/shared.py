'主界面共享布局与前台交接组件。'
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import SingleDirectionScrollArea

from winenv import (
    activate_game_window, allow_foreground_activation, can_auto_activate_game,
    find_game_hwnd, is_foreground, last_input_tick,
)
from runtime_guard import dev_log

ASSETS = Path(__file__).resolve().parents[1] / "assets"

class _LatestStatusBatcher:
    '合并高频状态消息，只在 GUI 线程定时显示最新一条。'

    INTERVAL_MS = 150

    def __init__(self, parent: QWidget, apply) -> None:
        self._apply = apply
        self._pending: str | None = None
        self._closed = False
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._flush)

    def push(self, message: str) -> None:
        '容量固定为一；新状态覆盖尚未显示的旧状态。'
        if self._closed:
            return
        self._pending = str(message)
        if not self._timer.isActive():
            self._timer.start()

    def show_now(self, message: str) -> None:
        '清除旧消息并立即显示关键状态。'
        if self._closed:
            return
        self._timer.stop()
        self._pending = None
        self._apply(str(message))

    def shutdown(self) -> None:
        '关闭窗口时停止刷新并丢弃未显示消息。'
        self._closed = True
        self._timer.stop()
        self._pending = None

    def _flush(self) -> None:
        if self._closed or self._pending is None:
            return
        message = self._pending
        self._pending = None
        self._apply(message)


def _nav_icon(name, fallback):
    '左侧菜单图标:assets/ 下有同名 png 就用,否则回退内置图标。'
    p = ASSETS / name
    return QIcon(str(p)) if p.exists() else fallback


def _handoff_game_foreground_once(input_tick: int | None, log=None,
                                  exclude_hwnd: int = 0) -> bool:
    '只尝试一次把正式游戏交到前台；用户已切走时绝不抢回。'
    emit = log if callable(log) else dev_log
    hwnd = find_game_hwnd(
        prefer_foreground=not bool(exclude_hwnd),
        exclude_hwnd=exclude_hwnd,
    )
    if hwnd and is_foreground(hwnd):
        emit("游戏已在前台，任务开始工作")
        return True
    if not can_auto_activate_game(input_tick):
        emit("检测到用户正在操作其他程序，未抢占前台；切回游戏后任务自动继续")
        return False
    if hwnd and activate_game_window(hwnd) and is_foreground(hwnd):
        emit("已将游戏切到前台，任务开始工作")
        return True
    emit("未能将游戏切到前台；本次不再重试，切回游戏后任务自动继续")
    return False


def _minimize_for_task(owner, log=None, *, handoff: bool = True,
                       exclude_hwnd: int = 0, after=None) -> int | None:
    '任务开始统一最小化主窗口，并可在同一 GUI tick 内一次性交接游戏前台。'
    main_window = owner.window()
    if main_window is None:
        if callable(after):
            after()
        return None
    input_tick = last_input_tick()
    allow_foreground_activation()

    def finish_handoff() -> None:
        if handoff:
            _handoff_game_foreground_once(input_tick, log, exclude_hwnd)

        if callable(after):
            try:
                after()
            except Exception as exc:
                dev_log("最小化后的任务启动回调异常", exc)

    def apply() -> None:
        main_window.showMinimized()
        if callable(log):
            log("主程序已自动最小化")

        QTimer.singleShot(60, finish_handoff)


    QTimer.singleShot(0, apply)
    return input_tick


class ScrollInterface(QWidget):
    '可滚动页面基类:子类把控件加到 self.vbox(置于垂直滚动视图中)。'

    def __init__(self, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = SingleDirectionScrollArea(self, orient=Qt.Vertical)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.enableTransparentBackground()
        self.view = QWidget(self.scroll)
        self.view.setObjectName("scrollView")
        self.view.setStyleSheet("#scrollView{background:transparent;}")
        self.scroll.setWidget(self.view)
        outer.addWidget(self.scroll)
        self.vbox = QVBoxLayout(self.view)

    def resizeEvent(self, event) -> None:
        '窗口尺寸或运行态控件变化后，强制刷新滚动页内所有任务卡的横向布局。'
        super().resizeEvent(event)


        QTimer.singleShot(0, self._refresh_responsive_layout)

    def _refresh_responsive_layout(self) -> None:
        if not hasattr(self, "view"):
            return
        self.view.updateGeometry()
        self.vbox.invalidate()
        self.vbox.activate()
        for index in range(self.vbox.count()):
            widget = self.vbox.itemAt(index).widget()
            if widget is not None:
                widget.updateGeometry()


def _suspend_realtime_for(page: QWidget, owner: str) -> bool:
    realtime = getattr(page.window(), "realtime", None)
    return bool(realtime and realtime.suspend_for_task(owner))


def _resume_realtime_for(page: QWidget, owner: str, suspended: bool) -> None:
    if not suspended:
        return
    realtime = getattr(page.window(), "realtime", None)
    if realtime:
        realtime.resume_after_task(owner)


