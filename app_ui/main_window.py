'主窗口导航和页面生命周期管理。'
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QEvent, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from qfluentwidgets import (
    FluentIcon as FIF, FluentWindow, InfoBadge, NavigationItemPosition,
    Theme, setTheme, setThemeColor,
)

from config import cfg
from game_exit_monitor import GameExitMonitor
from route_script.controller import RouteScriptInterface
from route_script.store import RouteStore
from runtime_guard import dev_log, registry, release_known_keys
from version import __version__

try:
    from pynput import keyboard
except Exception:
    keyboard = None

from .about import AboutInterface
from .combat import CombatInterface
from .daily import DailyInterface
from .independent import FishingInterface
from .realtime import RealtimeInterface
from .settings import SettingsInterface
from .shared import _nav_icon

APP_VERSION = f"v{__version__}"


def _is_alt_key(key) -> bool:
    '判断 pynput 的左右 Alt 与 AltGr。'
    if keyboard is None:
        return False
    values = tuple(
        value for value in (
            getattr(keyboard.Key, "alt", None),
            getattr(keyboard.Key, "alt_l", None),
            getattr(keyboard.Key, "alt_r", None),
            getattr(keyboard.Key, "alt_gr", None),
        ) if value is not None)
    return key in values

class MainWindow(FluentWindow):

    emergencyStopRequested = Signal()
    combatStartRequested = Signal()
    routeRecordStartRequested = Signal()
    routePlayStartRequested = Signal()
    routeRecordFinishRequested = Signal()

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle(f"HOKWorld  {APP_VERSION}  ·  王者荣耀世界")
        self.setWindowIcon(_nav_icon("app.png", QIcon()))
        try:
            self.titleBar.iconLabel.hide()
        except Exception:
            pass
        self.resize(1180, 720)

        self.realtime = RealtimeInterface()
        self.combat = CombatInterface()
        self.daily = DailyInterface()
        self.route_store = RouteStore()
        self.route_scripts = RouteScriptInterface(store=self.route_store)
        self.fishing = FishingInterface(route_store=self.route_store)
        self.fishing.bind_route_interface(self.route_scripts)
        self.settings = SettingsInterface()
        self.about = AboutInterface()
        self.addSubInterface(self.realtime, _nav_icon("realtime.png", FIF.VIDEO), "实时检测")
        self.addSubInterface(self.daily, _nav_icon("daily.png", FIF.CALENDAR), "每日任务")
        self.addSubInterface(self.fishing, _nav_icon("task.png", FIF.GAME), "独立任务")
        self.addSubInterface(self.combat, _nav_icon("combat.png", FIF.GAME), "自动战斗")
        self.addSubInterface(self.route_scripts, FIF.ROBOT, "自定义路线")
        self._settings_nav_item = self.addSubInterface(
            self.settings, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.about, FIF.INFO, "关于", NavigationItemPosition.BOTTOM)

        self.settings.autoCloseScriptChanged.connect(
            self._on_auto_close_script_changed)


        self.navigationInterface.setExpandWidth(170)
        self.navigationInterface.setMinimumExpandWidth(0)
        self.navigationInterface.setCollapsible(True)
        self.navigationInterface.setMenuButtonVisible(True)
        self.navigationInterface.setReturnButtonVisible(False)
        try:
            self.navigationInterface.expand(useAni=False)
        except Exception:
            pass

        self._hotkey = None
        self._combat_hotkeys_enabled = threading.Event()
        self._route_hotkeys_enabled = threading.Event()
        self._route_recording_active = threading.Event()
        self._last_f11_at = 0.0
        self._last_f10_at = 0.0
        self._alt_pressed = threading.Event()
        self._closing = False
        self._close_ready = False
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self._poll_close_ready)
        self._game_exit_monitor = GameExitMonitor(
            enabled=bool(cfg.get("close_script_when_game_exits")),
            log=lambda message: dev_log(f"[game exit] {message}"),
        )
        self._game_exit_timer = QTimer(self)
        self._game_exit_timer.setInterval(500)
        self._game_exit_timer.timeout.connect(self._poll_game_exit)
        self._game_exit_timer.start()
        self.emergencyStopRequested.connect(self._handle_emergency_stop)
        self.combatStartRequested.connect(self.combat.start_from_hotkey)
        self.routeRecordStartRequested.connect(
            self.route_scripts.request_record_from_hotkey)
        self.routePlayStartRequested.connect(
            self.route_scripts.request_play_from_hotkey)
        self.routeRecordFinishRequested.connect(
            self.fishing.finish_route_recording_from_hotkey)
        self.route_scripts.recordingStateChanged.connect(
            self._set_route_recording_active)
        self.stackedWidget.currentChanged.connect(self._sync_hotkey_scopes)
        self._sync_hotkey_scopes()
        if keyboard is not None:
            self._hotkey = keyboard.Listener(
                on_press=self._on_key, on_release=self._on_key_release)
            self._hotkey.start()


    def _on_auto_close_script_changed(self, enabled: bool) -> None:
        '设置变化后立即启用或解除游戏进程监控。'
        self._game_exit_monitor.set_enabled(bool(enabled))

    def _poll_game_exit(self) -> None:
        '游戏进程退出时关闭程序；自动浇水持有生命周期期间不适用。'
        if self._closing:
            return
        suppressed = registry.active() == "自动浇水"
        if self._game_exit_monitor.poll(suppressed=suppressed):
            dev_log("检测到游戏进程退出:自动关闭脚本优先于实时任务复位")
            self.close()

    def _on_key(self, key) -> None:
        'pynput 线程回调:立即停动作,但绝不直接读写 Qt 控件。'
        try:
            if _is_alt_key(key):
                self._alt_pressed.set()
                return
            if (key == keyboard.Key.f10
                    and self._alt_pressed.is_set()
                    and self._route_hotkeys_enabled.is_set()):
                now = time.monotonic()
                if now - self._last_f10_at >= 0.40:
                    self._last_f10_at = now
                    self.routeRecordStartRequested.emit()
                return
            if key == keyboard.Key.f11:
                route_enabled = self._route_hotkeys_enabled.is_set()
                combat_enabled = self._combat_hotkeys_enabled.is_set()
                if not route_enabled and not combat_enabled:
                    return
                now = time.monotonic()
                if now - self._last_f11_at >= 0.40:
                    self._last_f11_at = now
                    if route_enabled:
                        self.routePlayStartRequested.emit()
                    elif combat_enabled:
                        self.combatStartRequested.emit()
                return
            if key == keyboard.Key.f12:
                if self._route_recording_active.is_set():
                    self.routeRecordFinishRequested.emit()
                    return

                self.fishing.mark_route_emergency()
                registry.stop_all("F12 急停")
                self.emergencyStopRequested.emit()
        except Exception as exc:
            dev_log("全局热键处理失败", exc)

    def _on_key_release(self, key) -> None:
        '释放 Alt 后关闭组合键状态。'
        try:
            if _is_alt_key(key):
                self._alt_pressed.clear()
        except Exception as exc:
            dev_log("全局热键释放处理失败", exc)

    def _sync_hotkey_scopes(self, *_args) -> None:
        '按当前菜单限制Alt+F10录制及F11路线或战斗启动范围。'
        if self.stackedWidget.currentWidget() is self.combat:
            self._combat_hotkeys_enabled.set()
        else:
            self._combat_hotkeys_enabled.clear()
        if self.stackedWidget.currentWidget() is self.route_scripts:
            self._route_hotkeys_enabled.set()
        else:
            self._route_hotkeys_enabled.clear()

    def _set_route_recording_active(self, active: bool) -> None:
        '同步纯线程事件，供全局热键回调安全判断。'
        if active:
            self._route_recording_active.set()
        else:
            self._route_recording_active.clear()

    def _handle_emergency_stop(self) -> None:
        'GUI 主线程槽:只在这里更新各页面的急停状态。'
        self.fishing.emergency_stop()
        self.realtime.emergency_stop()
        self.combat.emergency_stop()
        self.daily.emergency_stop()

    def _background_workers(self) -> list[QThread]:
        '当前仍由主窗口持有的全部 QThread,用于关闭前统一等待。'
        workers = [
            self.fishing._worker,
            self.fishing._water_worker,
            *self.fishing.route_workers(),
            self.realtime._launcher,
            self.realtime._worker,
            self.realtime._gather,
            self.combat._worker,
            self.daily._worker,
        ]

        return list(dict.fromkeys(w for w in workers if w is not None))

    def _begin_close(self) -> None:
        '只执行一次的协作式停机:停输入、停监听、取消下载,不强杀线程。'
        dev_log("主窗口关闭:开始统一停止后台线程")
        self._game_exit_timer.stop()
        self._game_exit_monitor.close()
        for page in (self.fishing, self.realtime, self.combat, self.daily):
            page._status_batch.shutdown()
        self.fishing.shutdown_route_views()
        self.fishing.mark_route_emergency()
        registry.stop_all("主窗口关闭")
        self.realtime._aborting = True
        self.realtime._stop_workers_no_ui()
        if self.fishing._worker:
            self.fishing._worker.stop()
        if self.fishing._water_worker:
            self.fishing._water_worker.stop()
        self.fishing.stop_route_workers(emergency=True)
        if self.combat._worker:
            self.combat._worker.stop()
        if self.daily._worker:
            self.daily._worker.stop()
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception as exc:
                dev_log("停止 F12 全局监听失败", exc)
            self._hotkey = None
        release_known_keys()

    def _poll_close_ready(self) -> None:
        '等待 QThread 自然退出;窗口已隐藏,不会阻塞 GUI 或强杀持有资源的线程。'
        if any(worker.isRunning() for worker in self._background_workers()):
            return
        self._close_timer.stop()
        self._close_ready = True
        dev_log("主窗口关闭:后台线程已全部结束")
        QTimer.singleShot(0, self._finish_close)

    def _finish_close(self) -> None:
        '接受最终关闭并显式退出 Qt 事件循环，避免无窗口 pythonw 进程残留。'
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event) -> None:
        if self._close_ready:
            event.accept()
            super().closeEvent(event)
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self.hide()
            self._begin_close()
            self._close_timer.start()
        self._poll_close_ready()


def build_window() -> MainWindow:
    setTheme(Theme.LIGHT)
    setThemeColor("#2dd4a8")
    return MainWindow()
