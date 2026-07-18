'HOKWord 控制台界面。'
from __future__ import annotations

import sys


if sys.stdout is None or sys.stderr is None:
    import os
    _null = open(os.devnull, "w")
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

from pathlib import Path

from PySide6.QtCore import Qt, QPoint, QSize, QThread, QTimer, Signal
from PySide6.QtGui import QDrag, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QListWidget, QListWidgetItem,
    QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ExpandGroupSettingCard, FluentIcon as FIF,
    ComboBox, FluentWindow, IconWidget, InfoBar, InfoBarPosition, MessageBox,
    IndeterminateProgressRing, MessageBoxBase, NavigationItemPosition, PrimaryPushButton,
    ProgressBar, PushButton,
    PushSettingCard, SettingCard, SingleDirectionScrollArea, SpinBox, StrongBodyLabel,
    SubtitleLabel, SwitchButton, SwitchSettingCard, TextEdit,
    TitleLabel, setTheme, setThemeColor, Theme,
)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import os         # noqa: E402
import threading  # noqa: E402

from winenv import (activate_game_window, allow_foreground_activation, can_auto_activate_game,
                      center_window, find_game_hwnd, hide_console, is_admin, is_foreground,
                      last_input_tick, relaunch_as_admin, set_app_id)  # noqa: E402
from config import cfg  # noqa: E402
from runtime_guard import atomic_write_text, dev_log, input_owner, registry, release_known_keys  # noqa: E402
from version import __version__  # noqa: E402

APP_VERSION = f"v{__version__}"   

try:
    from pynput import keyboard
except Exception:
    keyboard = None

ASSETS = HERE / "assets"




_REALTIME_INIT_LOCK = threading.Lock()


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


class FishWorker(QThread):
    sig_log = Signal(str)
    sig_count = Signal(int)
    sig_done = Signal()

    def __init__(self, count: int, exit_after: bool) -> None:
        super().__init__()
        self._count = count
        self._exit_after = exit_after
        self.bot = None
        self._paused = False
        self._stop_requested = False

    def run(self) -> None:
        try:
            
            import importlib
            import winenv as window_env
            import fishing.matcher
            import fishing.fisher
            for m in (window_env, fishing.matcher, fishing.fisher):
                importlib.reload(m)
            from fishing.fisher import FishingBot
            self.bot = FishingBot(log=self.sig_log.emit, on_count=self.sig_count.emit)
            if self._stop_requested:
                return
            self.bot.set_paused(self._paused)
            with input_owner("自动钓鱼"):
                self.bot.run(self._count, self._exit_after)
        except Exception as exc:
            dev_log("自动钓鱼线程异常,执行保守急停", exc)
            registry.stop_all("自动钓鱼线程异常")
            release_known_keys(self.sig_log.emit)
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
            self.sig_log.emit("已保守急停,详情见 data/logs/hokworld_dev.log")
        finally:
            self.sig_done.emit()

    def stop(self) -> None:
        self._stop_requested = True
        if self.bot:
            self.bot.stop()

    def set_paused(self, on: bool) -> None:
        self._paused = bool(on)
        if self.bot:
            self.bot.set_paused(self._paused)


class StoryWorker(QThread):
    '实时剧情跳过线程(热重载 story 代码)。'
    sig_log = Signal(str)
    sig_count = Signal(int)
    sig_foreground = Signal(bool)
    sig_done = Signal()

    def __init__(self, nudge: bool, monthly_card: bool) -> None:
        super().__init__()
        self._nudge = nudge
        self._monthly_card = monthly_card
        self.bot = None
        self._paused = False
        self._stop_requested = False

    def run(self) -> None:
        try:
            with _REALTIME_INIT_LOCK:
                import importlib
                import daily.recognizer
                import daily.tasks.monthly_card
                import story.recognizer
                import story.skipper
                for m in (daily.recognizer, daily.tasks.monthly_card,
                          story.recognizer, story.skipper):
                    importlib.reload(m)
                from story.skipper import StorySkipper
                self.bot = StorySkipper(log=self.sig_log.emit, on_count=self.sig_count.emit,
                                        on_foreground=self.sig_foreground.emit)
                self.bot.set_paused(self._paused)
                if self._stop_requested:
                    self.bot.stop()
                    return
            with input_owner("实时检测"):
                self.bot.run(nudge=self._nudge, monthly_card=self._monthly_card)
        except Exception as exc:
            dev_log("实时剧情线程异常,执行保守急停", exc)
            registry.stop_all("实时剧情线程异常")
            release_known_keys(self.sig_log.emit)
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
            self.sig_log.emit("已保守急停,详情见 data/logs/hokworld_dev.log")
        finally:
            self.sig_done.emit()

    def stop(self) -> None:
        self._stop_requested = True
        if self.bot:
            self.bot.stop()

    def set_paused(self, on: bool) -> None:
        self._paused = bool(on)
        if self.bot:
            self.bot.set_paused(self._paused)


class GatherWorker(QThread):
    '实时采集线程(经过材料/宝箱/重现按 F;热重载 gather 代码)。'
    sig_log = Signal(str)
    sig_count = Signal(int)
    sig_foreground = Signal(bool)
    sig_done = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.bot = None
        self._paused = False
        self._stop_requested = False

    def run(self) -> None:
        try:
            with _REALTIME_INIT_LOCK:
                import importlib
                import gather.recognizer
                import gather.picker
                for m in (gather.recognizer, gather.picker):
                    importlib.reload(m)
                from gather.picker import GatherPicker
                self.bot = GatherPicker(log=self.sig_log.emit, on_count=self.sig_count.emit,
                                        on_foreground=self.sig_foreground.emit)
                self.bot.set_paused(self._paused)
                if self._stop_requested:
                    self.bot.stop()
                    return
            with input_owner("实时检测"):
                self.bot.run()
        except Exception as exc:
            dev_log("自动采集线程异常,执行保守急停", exc)
            registry.stop_all("自动采集线程异常")
            release_known_keys(self.sig_log.emit)
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
            self.sig_log.emit("已保守急停,详情见 data/logs/hokworld_dev.log")
        finally:
            self.sig_done.emit()

    def stop(self) -> None:
        self._stop_requested = True
        if self.bot:
            self.bot.stop()

    def set_paused(self, on: bool) -> None:
        self._paused = bool(on)
        if self.bot:
            self.bot.set_paused(self._paused)


class LaunchWorker(QThread):
    '自动启动游戏线程(热重载 launcher;不重载 fishing.matcher,保住 OCR 单例不被重置)。'
    sig_log = Signal(str)
    sig_done = Signal(bool)        

    def __init__(self, input_tick_at_start: int | None = None) -> None:
        super().__init__()
        self.bot = None
        self._paused = False
        self._stop_requested = False
        self._input_tick_at_start = input_tick_at_start

    def run(self) -> None:
        ok = False
        phase = "加载"
        try:
            import importlib
            import winenv as window_env
            import capture
            import launcher
            for m in (window_env, capture, launcher):
                importlib.reload(m)
            from launcher import GameLauncher
            self.bot = GameLauncher(
                log=self._log, input_tick_at_start=self._input_tick_at_start)
            self.bot.set_paused(self._paused)
            if self._stop_requested:
                self.bot.stop()
                return
            phase = "运行"
            with input_owner("实时检测"):
                ok = bool(self.bot.run())
        except Exception as exc:
            
            dev_log(f"自动启动游戏{phase}异常", exc)
            release_known_keys(self.sig_log.emit)
            if phase == "加载":
                self.sig_log.emit(
                    f"自动启动模块加载失败,已跳过(不影响实时检测):{type(exc).__name__}")
            else:
                self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
                self.sig_log.emit("自动启动出错,已跳过(不影响实时检测)")
        finally:
            self.sig_done.emit(ok)

    def _log(self, msg: str) -> None:
        '启动器状态同时写 UI 和 dev 日志(便于事后定位每一步走到哪)。'
        dev_log(f"[launcher] {msg}")
        self.sig_log.emit(msg)

    def stop(self) -> None:
        self._stop_requested = True
        if self.bot:
            self.bot.stop()

    def set_paused(self, on: bool) -> None:
        self._paused = bool(on)
        if self.bot:
            self.bot.set_paused(self._paused)


class DailyWorker(QThread):
    '每日任务一条龙线程(热重载 daily 代码,便于调田块坐标/参数后点开始即生效)。'
    sig_log = Signal(str)
    sig_progress = Signal(int, int)   
    sig_done = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.orch = None
        self._paused = False
        self._stop_requested = False

    def run(self) -> None:
        try:
            import importlib
            import sys as _sys
            
            import daily.regions, daily.recognizer, daily.context, daily.navigation
            import daily.base, daily.config, daily.tasks, daily.orchestrator
            import daily.tasks._field
            
            task_mods = [m for n, m in sorted(_sys.modules.items())
                         if n.startswith("daily.tasks.") and n != "daily.tasks._field"]
            for m in (daily.regions, daily.recognizer, daily.context, daily.navigation,
                      daily.base, daily.config, daily.tasks._field,
                      *task_mods, daily.tasks, daily.orchestrator):
                importlib.reload(m)
            from daily.orchestrator import DailyOrchestrator
            self.orch = DailyOrchestrator(
                log=self.sig_log.emit,
                on_progress=lambda d, t: self.sig_progress.emit(d, t))
            self.orch.set_paused(self._paused)
            if self._stop_requested:
                self.orch.stop()
                return
            with input_owner("每日任务"):
                self.orch.run()
        except Exception as exc:
            dev_log("每日任务一条龙线程异常,执行保守急停", exc)
            registry.stop_all("每日任务线程异常")
            release_known_keys(self.sig_log.emit)
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
            self.sig_log.emit("已保守急停,详情见 data/logs/hokworld_dev.log")
        finally:
            self.sig_done.emit()

    def stop(self) -> None:
        self._stop_requested = True
        if self.orch:
            self.orch.stop()

    def set_paused(self, on: bool) -> None:
        self._paused = bool(on)
        if self.orch:
            self.orch.set_paused(self._paused)






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


class FishingInterface(ScrollInterface):
    _CARD_DESC = "自动完成钓鱼"

    def __init__(self) -> None:
        super().__init__("fishingInterface")
        self._worker: FishWorker | None = None
        self._paused = False
        self._caught = 0
        self._resume_realtime = False

        root = self.vbox
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        
        
        self.card = ExpandGroupSettingCard(FIF.GAME, "自动钓鱼", self._CARD_DESC, self)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始")
        self.stop_btn = PushButton(FIF.PAUSE, "停止")
        self.stop_btn.setEnabled(False)
        self.card.addWidget(self.start_btn)
        self.card.addWidget(self.stop_btn)

        self.count_spin = SpinBox()           
        self.count_spin.setRange(0, 9999)
        self.count_spin.setValue(0)
        self.count_spin.setFixedWidth(150)
        self.card.addGroup(FIF.SYNC, "循环次数", "设置需要完成的次数", self.count_spin)
        self.exit_switch = SwitchButton()     
        self.card.addGroup(FIF.POWER_BUTTON, "完成后退出", "结束后退出钓鱼界面", self.exit_switch)
        root.addWidget(self.card)

        
        
        self.status_card = CardWidget()
        sl = QHBoxLayout(self.status_card)
        sl.setContentsMargins(16, 10, 16, 10)
        sl.setSpacing(10)
        self._status_icon = IconWidget(FIF.SYNC, self.status_card)
        self._status_icon.setFixedSize(16, 16)
        self.status = BodyLabel("")
        sl.addWidget(self._status_icon)
        sl.addWidget(self.status, 1)
        self.status_card.hide()
        root.addWidget(self.status_card)
        root.addStretch(1)

        self._last_msg = ""
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)

    
    def _start(self) -> None:
        if self._worker:
            self._toggle_pause()
            return
        self._resume_realtime = _suspend_realtime_for(self, "自动钓鱼")
        ok, reason = registry.start("自动钓鱼")
        if not ok:
            _resume_realtime_for(self, "自动钓鱼", self._resume_realtime)
            self._resume_realtime = False
            InfoBar.warning("任务已在运行", reason, duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        self._warn_admin()
        self._show_status("自动钓鱼启动中…")
        self._worker = FishWorker(self.count_spin.value(), self.exit_switch.isChecked())
        self._worker.sig_log.connect(self._append)
        self._worker.sig_count.connect(self._on_count)
        self._worker.sig_done.connect(self._on_done)
        registry.set_stopper("自动钓鱼", self._worker.stop)
        self._paused = False
        self._caught = 0
        self.start_btn.setText("运行中")
        self.start_btn.setIcon(FIF.PAUSE)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self._set_card_content(self.card, "运行中…")
        _minimize_for_task(self, self._append, after=self._worker.start)

    def _on_count(self, count: int) -> None:
        self._caught = count
        state = "已暂停" if self._paused else "运行中"
        self._set_card_content(self.card, f"{state} · 已钓 {count}")

    def _toggle_pause(self) -> None:
        if not self._worker:
            return
        self._paused = not self._paused
        self._worker.set_paused(self._paused)
        if self._paused:
            self.start_btn.setText("继续")
            self.start_btn.setIcon(FIF.PLAY)
            self._set_card_content(self.card, f"已暂停 · 已钓 {self._caught}")
            self._append(f"已暂停，当前已钓 {self._caught} 条")
        else:
            self.start_btn.setText("运行中")
            self.start_btn.setIcon(FIF.PAUSE)
            self._set_card_content(self.card, f"运行中 · 已钓 {self._caught}")
            self._append(f"已继续，当前已钓 {self._caught} 条")

    def _stop(self) -> None:
        if self._worker:
            self._append("停止中…")
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self._worker.stop()

    def _on_done(self) -> None:
        if self._worker:
            self._worker.wait(1500)
            self._worker = None
        registry.finish("自动钓鱼")
        resume_realtime = self._resume_realtime
        self._resume_realtime = False
        self._paused = False
        self._caught = 0
        self.start_btn.setText("开始")
        self.start_btn.setIcon(FIF.PLAY)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_card_content(self.card, self._CARD_DESC)
        _resume_realtime_for(self, "自动钓鱼", resume_realtime)

    
    def _warn_admin(self) -> None:
        if not is_admin():
            InfoBar.warning("需要管理员", "请以管理员权限重启后再开始。",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)

    def _show_status(self, msg: str) -> None:
        self._last_msg = msg
        self.status_card.show()
        self._refresh_status()

    def _set_card_content(self, card, text: str) -> None:
        try:
            card.card.setContent(text)        
        except Exception:
            pass

    def _append(self, msg: str) -> None:
        self._last_msg = msg                  
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.status.setText(self._last_msg)

    def emergency_stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._append("F12 急停")
        release_known_keys(self._append)


class RealtimeInterface(ScrollInterface):
    '实时检测页(对齐 同类脚本「实时触发」):点开始后实时读屏,自动识别跳过剧情。'
    _DESC = "自动处理剧情并采集经过的材料"

    def __init__(self) -> None:
        super().__init__("realtimeInterface")
        self._worker: StoryWorker | None = None
        self._gather: GatherWorker | None = None
        self._launcher: LaunchWorker | None = None
        self._paused = False
        self._task_pause_owner: str | None = None
        self._auto_paused = False
        self._foreground_states: dict[str, bool] = {}
        self._aborting = False                       
        self._last_msg = ""
        self._auto_minimized = False
        self._launch_input_tick: int | None = None
        self._launch_exclude_hwnd = 0
        self._focus_retry_left = 0
        self._foreground_handoff_done = False

        root = self.vbox
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        self.card = ExpandGroupSettingCard(FIF.VIDEO, "实时检测", self._DESC, self)
        self.action_host = QWidget(self.card)
        action_layout = QHBoxLayout(self.action_host)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(12)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始", self.action_host)
        self.run_indicator = QWidget(self.action_host)
        indicator_layout = QHBoxLayout(self.run_indicator)
        indicator_layout.setContentsMargins(0, 0, 0, 0)
        indicator_layout.setSpacing(7)
        self.run_spinner = IndeterminateProgressRing(self.run_indicator, start=False)
        self.run_spinner.setFixedSize(18, 18)
        self.run_spinner.setStrokeWidth(2)
        self.run_state = CaptionLabel("运行中 · 可暂停", self.run_indicator)
        indicator_layout.addWidget(self.run_spinner)
        indicator_layout.addWidget(self.run_state)
        self.run_indicator.hide()
        self.pause_btn = PrimaryPushButton(FIF.PAUSE, "暂停", self.action_host)
        self.stop_btn = PushButton(FIF.CLOSE, "停止")
        self.pause_btn.setEnabled(False)
        self.pause_btn.hide()
        self.stop_btn.setEnabled(False)
        action_layout.addWidget(self.start_btn)
        action_layout.addWidget(self.run_indicator)
        action_layout.addWidget(self.pause_btn)
        self.card.addWidget(self.action_host)
        self.card.addWidget(self.stop_btn)
        action_index = self.card.card.hBoxLayout.indexOf(self.action_host)
        self._action_gap = self.card.card.hBoxLayout.itemAt(action_index + 1).spacerItem()
        self.nudge_switch = SwitchButton()
        self.nudge_switch.setChecked(False)
        self.nudge_switch.setEnabled(False)          
        self.gather_switch = SwitchButton()
        self.gather_switch.setChecked(True)          
        self.card.addGroup(FIF.SYNC, "经过材料自动采集",
                           "自动采集经过的材料",
                           self.gather_switch)
        
        self.launch_switch = SwitchButton()
        self.launch_switch.setChecked(True)          
        self.card.addGroup(FIF.GAME, "自动启动游戏",
                           "启动检测时自动打开游戏", self.launch_switch)
        root.addWidget(self.card)

        self.status_card = CardWidget()
        sl = QHBoxLayout(self.status_card)
        sl.setContentsMargins(16, 10, 16, 10)
        sl.setSpacing(10)
        self._status_icon = IconWidget(FIF.VIDEO, self.status_card)
        self._status_icon.setFixedSize(16, 16)
        self.status = BodyLabel("")
        sl.addWidget(self._status_icon)
        sl.addWidget(self.status, 1)
        self.status_card.hide()
        root.addWidget(self.status_card)
        root.addStretch(1)

        self.start_btn.clicked.connect(self._start)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn.clicked.connect(self._stop)

    def _start(self) -> None:
        if self._paused and (self._worker or self._launcher or self._gather):
            self._toggle_pause()           
            return
        if self._worker or self._launcher:
            return
        ok, reason = registry.start("实时检测")
        if not ok:
            InfoBar.warning("任务已在运行", reason, duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        if not is_admin():
            InfoBar.warning("需要管理员", "请以管理员权限重启后再开始。",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
        self._paused = False
        self._aborting = False
        self._set_running_ui("启动中 · 可暂停")
        registry.set_stopper("实时检测", self._stop_workers_no_ui)
        if self.launch_switch.isChecked():
            
            self._show_status("自动启动游戏中…")
            self._set_content("自动启动游戏中…")
            
            self._minimize_for_auto_launch(after=self._start_launcher_after_minimize)
        else:
            self._minimize_for_auto_launch(after=self._begin_detection_after_handoff)

    def _minimize_for_auto_launch(self, after=None) -> None:
        '实时检测开始时统一最小化；自动启动结束后再一次性交接游戏前台。'
        self._launch_exclude_hwnd = 0
        self._auto_minimized = True
        self._foreground_handoff_done = False
        self._launch_input_tick = _minimize_for_task(
            self, self._append, handoff=False, after=after)

    def _start_launcher_after_minimize(self) -> None:
        '主程序已经最小化后，单次直接启动 LaunchWorker；失败不补偿重建。'
        if self._aborting:
            self._maybe_reset_ui()
            return
        self._launcher = LaunchWorker(self._launch_input_tick)
        self._launcher.sig_log.connect(self._append)
        self._launcher.sig_done.connect(self._on_launch_then_detect)
        self._launcher.start()

    def _begin_detection_after_handoff(self) -> None:
        '脚本窗口完成最小化后再交接前台并创建实时检测 Worker。'
        if self._aborting:
            self._maybe_reset_ui()
            return
        self._start_game_foreground_handoff()
        self._begin_detection()

    def _on_launch_then_detect(self, ok: bool) -> None:
        if self._launcher:
            bot = getattr(self._launcher, "bot", None)
            self._launch_exclude_hwnd = int(getattr(bot, "_launcher_hwnd", 0) or 0)
            if bool(getattr(bot, "_foreground_handoff_attempted", False)):
                self._foreground_handoff_done = True
            self._launcher.wait(1500)
            self._launcher = None
        if self._aborting:                     
            self._maybe_reset_ui()
            return
        if not ok:                             
            self._append("自动启动未完成，继续实时检测")
        self._start_game_foreground_handoff()
        self._begin_detection()                

    def _start_game_foreground_handoff(self) -> None:
        '启动完成后只尝试一次把正式游戏交到前台。'
        if self._foreground_handoff_done:
            return
        self._focus_retry_left = 1
        self._try_game_foreground()

    def _try_game_foreground(self) -> None:
        if (self._foreground_handoff_done or self._aborting
                or self._focus_retry_left <= 0 or not self._auto_minimized):
            return
        hwnd = find_game_hwnd(
            prefer_foreground=not bool(self._launch_exclude_hwnd),
            exclude_hwnd=self._launch_exclude_hwnd,
        )
        if hwnd and is_foreground(hwnd):
            self._focus_retry_left = 0
            self._foreground_handoff_done = True
            self._append("游戏已在前台，实时检测开始工作")
            return
        if not can_auto_activate_game(self._launch_input_tick):
            self._focus_retry_left = 0
            self._foreground_handoff_done = True
            self._append("检测到用户正在操作其他程序，未强制抢占前台；切回游戏后自动继续")
            return
        self._focus_retry_left = 0
        self._foreground_handoff_done = True
        if hwnd and activate_game_window(hwnd) and is_foreground(hwnd):
            self._append("已将游戏切到前台，实时检测开始工作")
        else:
            self._append("未能将游戏切到前台；本次不再重试，切回游戏后自动继续")

    def _begin_detection(self) -> None:
        self._show_status("实时检测启动中…")
        self._set_running_ui("运行中 · 可暂停")
        self._auto_paused = False
        self._foreground_states = {"story": True}
        self._worker = StoryWorker(
            self.nudge_switch.isChecked(), bool(cfg.get("monthly_card_enabled")))
        self._worker.set_paused(self._paused or bool(self._task_pause_owner))
        self._worker.sig_log.connect(self._append)
        self._worker.sig_foreground.connect(
            lambda active: self._on_foreground_state("story", active))
        self._worker.sig_done.connect(self._on_done)
        self._set_content("运行中…")
        self._worker.start(QThread.Priority.LowestPriority)
        if self.gather_switch.isChecked():
            self._foreground_states["gather"] = True
            self._gather = GatherWorker()
            self._gather.set_paused(self._paused or bool(self._task_pause_owner))
            self._gather.sig_log.connect(self._append)
            self._gather.sig_count.connect(lambda n: self._append(f"已采集 {n} 个材料"))
            self._gather.sig_foreground.connect(
                lambda active: self._on_foreground_state("gather", active))
            self._gather.sig_done.connect(self._on_gather_done)
            self._gather.start(QThread.Priority.LowestPriority)

    def _stop_workers_no_ui(self) -> None:
        if self._launcher:
            self._launcher.stop()
        if self._worker:
            self._worker.stop()
        if self._gather:
            self._gather.stop()

    def _toggle_pause(self) -> None:
        if not (self._worker or self._launcher or self._gather):
            return
        self._paused = not self._paused
        self._apply_worker_pause()
        self._refresh_pause_ui()
        self._append("已暂停" if self._paused else "已继续")
        self._set_content("已暂停" if self._paused or self._auto_paused else "运行中…")

    def _on_foreground_state(self, source: str, active: bool) -> None:
        'Worker 上报游戏前台变化；只暂停/恢复，不执行任何窗口激活。'
        if source not in self._foreground_states:
            return
        self._foreground_states[source] = active
        auto_paused = any(not state for state in self._foreground_states.values())
        if auto_paused == self._auto_paused:
            return
        self._auto_paused = auto_paused
        self._refresh_pause_ui()
        if self._task_pause_owner:
            self._set_content(f"{self._task_pause_owner}运行中 · 实时检测已暂停")
        else:
            self._set_content("游戏不在前台 · 已暂停" if auto_paused
                              else ("已暂停" if self._paused else "运行中…"))

    def _refresh_pause_ui(self) -> None:
        if self._task_pause_owner:
            self._set_paused_ui(f"{self._task_pause_owner}运行中", can_resume=False)
        elif self._auto_paused:
            self._set_paused_ui("游戏不在前台", can_resume=False)
        elif self._paused:
            self._set_paused_ui()
        else:
            self._set_running_ui("运行中 · 可暂停")

    def _apply_worker_pause(self) -> None:
        paused = self._paused or bool(self._task_pause_owner)
        if self._launcher:
            self._launcher.set_paused(paused)
        if self._worker:
            self._worker.set_paused(paused)
        if self._gather:
            self._gather.set_paused(paused)

    def suspend_for_task(self, owner: str) -> bool:
        '让实时检测静默让位；线程保留，任务结束后只恢复识别，不重新启动游戏。'
        if registry.active() != "实时检测":
            return False
        if not (self._worker or self._gather or self._launcher):
            return False
        if not registry.suspend("实时检测"):
            return False
        
        
        self._task_pause_owner = owner
        self._apply_worker_pause()
        self._append(f"{owner}启动，实时检测已自动暂停")
        self._refresh_pause_ui()
        self._set_content(f"{owner}运行中 · 实时检测已暂停")
        return True

    def resume_after_task(self, owner: str) -> bool:
        '恢复被 owner 暂停的现有线程，不走自动启动游戏流程。'
        if self._task_pause_owner != owner:
            return False
        if not (self._worker or self._gather or self._launcher):
            self._task_pause_owner = None
            return False
        self._task_pause_owner = None
        self._apply_worker_pause()  
        if not registry.resume("实时检测"):
            self._task_pause_owner = owner
            self._apply_worker_pause()
            return False
        self._refresh_pause_ui()
        self._set_content("已暂停" if self._paused else "运行中…")
        self._append(f"{owner}结束，实时检测已自动恢复" if not self._paused
                     else f"{owner}结束，实时检测保持用户暂停状态")
        return True

    def _stop(self) -> None:
        self._aborting = True
        self._append("停止中…")
        self.run_spinner.start()
        self.run_state.setText("停止中…")
        self.pause_btn.setEnabled(False)
        if self._launcher:
            self._launcher.stop()
        if self._worker:
            self._worker.stop()
        if self._gather:
            self._gather.stop()

    def _on_done(self) -> None:
        if self._worker:
            self._worker.wait(1500)
            self._worker = None
        self._foreground_states.pop("story", None)
        if self._gather:                  
            self._gather.stop()
        self._maybe_reset_ui()

    def _on_gather_done(self) -> None:
        if self._gather:
            self._gather.wait(1500)
            self._gather = None
        self._foreground_states.pop("gather", None)
        self._maybe_reset_ui()

    def _maybe_reset_ui(self) -> None:
        if self._worker or self._gather or self._launcher:   
            return
        registry.finish("实时检测")
        self._aborting = False
        self._focus_retry_left = 0
        self._auto_paused = False
        self._task_pause_owner = None
        self._foreground_states.clear()
        self._restore_after_failed_auto_launch()
        self._reset_run_ui()
        self._set_content(self._DESC)

    def _restore_after_failed_auto_launch(self) -> None:
        '任务结束只清理交接状态；窗口保持最小化，是否恢复由用户决定。'
        self._auto_minimized = False

    def _set_running_ui(self, text: str) -> None:
        '运行态用旋转圆圈代替变灰的开始按钮，并明确提示可以暂停。'
        self._set_action_shift(True)
        self.start_btn.hide()
        self.run_state.setText(text)
        self.run_indicator.show()
        self.run_spinner.start()
        self.pause_btn.setText("运行中")
        self.pause_btn.setIcon(FIF.PAUSE)
        self.pause_btn.setEnabled(True)
        self.pause_btn.show()
        self.stop_btn.setEnabled(True)

    def _set_paused_ui(self, text: str = "已暂停", can_resume: bool = True) -> None:
        self._set_action_shift(True)
        self.run_spinner.stop()
        self.run_state.setText(text)
        self.pause_btn.setText("已暂停")
        self.pause_btn.setIcon(FIF.PLAY)
        self.pause_btn.setEnabled(can_resume)
        self.pause_btn.show()
        self.stop_btn.setEnabled(True)

    def _reset_run_ui(self) -> None:
        self._set_action_shift(False)
        self.run_spinner.stop()
        self.run_indicator.hide()
        self.start_btn.setText("开始")
        self.start_btn.setIcon(FIF.PLAY)
        self.start_btn.setEnabled(True)
        self.start_btn.show()
        self.pause_btn.setText("暂停")
        self.pause_btn.setIcon(FIF.PAUSE)
        self.pause_btn.setEnabled(False)
        self.pause_btn.hide()
        self.stop_btn.setEnabled(False)

    def _set_action_shift(self, shifted: bool) -> None:
        '不交换按钮顺序，仅让运行/暂停操作区向右偏移 8 px。'
        self._action_gap.changeSize(11 if shifted else 19, 0)
        self.card.card.hBoxLayout.invalidate()

    def _set_content(self, text: str) -> None:
        try:
            self.card.card.setContent(text)
        except Exception:
            pass

    def _show_status(self, msg: str) -> None:
        self._last_msg = msg
        self.status_card.show()
        self.status.setText(msg)

    def _append(self, msg: str) -> None:
        self._last_msg = msg
        self.status.setText(msg)

    def emergency_stop(self) -> None:
        self._aborting = True                  
        stopped = False
        for w in (self._worker, self._gather, self._launcher):
            if w:
                w.stop()
                stopped = True
        if stopped:
            self._append("F12 急停")
        release_known_keys(self._append)


class _TaskRow(CardWidget):
    '一条龙里的一个任务行(参考绝区零一条龙):拖拽手柄 + 开关 + 名称 + 右侧留白(将来放。'

    def __init__(self, task_id: str, name: str, enabled: bool, parent=None) -> None:
        super().__init__(parent)
        self.task_id = task_id
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(10)
        self.handle = CaptionLabel("⠿")             
        self.handle.setToolTip("拖动调整执行顺序")
        self.switch = SwitchButton()
        self.switch.setChecked(enabled)
        self.label = BodyLabel(name)
        lay.addWidget(self.handle)
        lay.addWidget(self.switch)
        lay.addWidget(self.label, 1)
        
        self.actions = QWidget()
        self.actions_lay = QHBoxLayout(self.actions)
        self.actions_lay.setContentsMargins(0, 0, 0, 0)
        self.actions_lay.setSpacing(6)
        lay.addWidget(self.actions)


class _ReorderList(QListWidget):
    '可整行拖动排序的列表(InternalMove);拖放完成后发 orderChanged。'
    orderChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._press_pos = None   
        self._reflow_pending = False

    def resizeEvent(self, event) -> None:
        '让 setItemWidget() 创建的任务方框始终跟随列表视口宽度。'
        super().resizeEvent(event)
        self.schedule_item_reflow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.schedule_item_reflow()

    def schedule_item_reflow(self) -> None:
        if self._reflow_pending:
            return
        self._reflow_pending = True
        QTimer.singleShot(0, self._reflow_item_widgets)

    def _reflow_item_widgets(self) -> None:
        self._reflow_pending = False
        width = max(1, self.viewport().width())
        for index in range(self.count()):
            item = self.item(index)
            widget = self.itemWidget(item)
            if widget is None:
                continue
            height = max(item.sizeHint().height(), widget.sizeHint().height())
            
            
            
            widget.setMaximumWidth(width)
            widget.resize(width, height)
            widget.updateGeometry()
        self.doItemsLayout()

    def mousePressEvent(self, e) -> None:
        try:
            self._press_pos = e.position().toPoint()
        except Exception:
            self._press_pos = e.pos()
        super().mousePressEvent(e)

    def startDrag(self, supportedActions) -> None:
        item = self.currentItem()
        widget = self.itemWidget(item) if item is not None else None
        if widget is None:
            super().startDrag(supportedActions)
            return
        try:
            pm = widget.grab()
            
            rect = self.visualItemRect(item)
            if self._press_pos is not None:
                hot = self._press_pos - rect.topLeft()
                hot.setX(max(0, min(pm.width() - 1, hot.x())))
                hot.setY(max(0, min(pm.height() - 1, hot.y())))
            else:
                hot = QPoint(24, pm.height() // 2)
            mime = self.model().mimeData([self.indexFromItem(item)])
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.setPixmap(pm)
            drag.setHotSpot(hot)
            drag.exec(supportedActions, Qt.MoveAction)
        except Exception as exc:
            dev_log("拖动影像生成失败,回退默认", exc)
            super().startDrag(supportedActions)

    def dropEvent(self, e) -> None:
        super().dropEvent(e)
        self.orderChanged.emit()


class DailyInterface(ScrollInterface):
    '每日任务一条龙(UI 参考绝区零一条龙:任务卡可开关、可上下调序)。'
    _DESC = "按顺序自动完成每日任务"

    def __init__(self) -> None:
        super().__init__("dailyInterface")
        self._worker: DailyWorker | None = None
        self._paused = False
        self._resume_realtime = False
        self._last_msg = ""
        from daily.config import DailyConfig, TASK_REGISTRY
        self._DailyConfig = DailyConfig
        self._TASK_REGISTRY = TASK_REGISTRY
        self.config = DailyConfig()

        root = self.vbox
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        
        self.card = ExpandGroupSettingCard(FIF.CALENDAR, "每日任务一条龙", self._DESC, self)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始")
        self.pause_btn = PushButton(FIF.PAUSE, "暂停")
        self.stop_btn = PushButton(FIF.CLOSE, "停止")
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.card.addWidget(self.start_btn)
        self.card.addWidget(self.pause_btn)
        self.card.addWidget(self.stop_btn)
        root.addWidget(self.card)

        
        
        self._daily_option_widgets: list[QWidget] = []
        self.farm_route_combo = ComboBox()
        self.farm_route_combo.addItems(["第二列", "第五列"])
        farm_route = self.config.param("farm", "route", "第二列")
        self.farm_route_combo.setCurrentText(
            farm_route if farm_route in ("第二列", "第五列") else "第二列")
        self.farm_route_combo.setMinimumWidth(96)
        self.farm_seed_combo = self._blank_daily_combo("种子")

        farm_options = QWidget()
        farm_options_lay = QHBoxLayout(farm_options)
        farm_options_lay.setContentsMargins(0, 0, 0, 0)
        farm_options_lay.setSpacing(8)
        farm_options_lay.addWidget(CaptionLabel("路线"))
        farm_options_lay.addWidget(self.farm_route_combo)
        farm_options_lay.addWidget(CaptionLabel("种子"))
        farm_options_lay.addWidget(self.farm_seed_combo)
        self.card.addGroup(FIF.LEAF, "农贸作物", "选择行进路线与种植种子", farm_options)

        self.incubator_seed_combo = self._blank_daily_combo("种子")
        self.card.addGroup(
            FIF.TILES, "培养箱", "选择需要种植的种子", self.incubator_seed_combo)

        self.dispatch_pet_combo = self._blank_daily_combo("宠物")
        self.card.addGroup(
            FIF.ROBOT, "宠物派遣", "选择参与派遣的宠物", self.dispatch_pet_combo)

        self.friends_target_combo = self._blank_daily_combo("好友")
        self.card.addGroup(
            FIF.PEOPLE, "好友家浇水", "选择浇水目标", self.friends_target_combo)

        self.alchemy_material_combo = self._blank_daily_combo("材料")
        self.card.addGroup(
            FIF.CALORIES, "制药", "选择制作材料", self.alchemy_material_combo)

        self.cooking_material_combo = self._blank_daily_combo("材料")
        self.card.addGroup(
            FIF.CAFE, "烹饪", "选择制作材料", self.cooking_material_combo)

        self._daily_option_widgets.extend([
            self.farm_route_combo,
            self.farm_seed_combo,
            self.incubator_seed_combo,
            self.dispatch_pet_combo,
            self.friends_target_combo,
            self.alchemy_material_combo,
            self.cooking_material_combo,
        ])
        self.farm_route_combo.currentTextChanged.connect(self._on_farm_route_changed)

        
        self.list_box = CardWidget()
        self.list_lay = QVBoxLayout(self.list_box)
        self.list_lay.setContentsMargins(12, 12, 12, 12)
        self.list_lay.setSpacing(8)
        title = StrongBodyLabel("任务")
        self.list_lay.addWidget(title)
        self.task_list = _ReorderList()
        self.task_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.task_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.task_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_list.setFrameShape(QListWidget.NoFrame)
        self.task_list.setStyleSheet("QListWidget{background:transparent;}")
        self.task_list.orderChanged.connect(self._on_reorder)
        self.list_lay.addWidget(self.task_list)
        root.addWidget(self.list_box)
        self._rows: list[_TaskRow] = []
        self._rebuild_rows()

        
        self.progress = ProgressBar()
        self.progress.setValue(0)
        self.progress.hide()
        root.addWidget(self.progress)
        self.status_card = CardWidget()
        sl = QHBoxLayout(self.status_card)
        sl.setContentsMargins(16, 10, 16, 10)
        sl.setSpacing(10)
        self._status_icon = IconWidget(FIF.CALENDAR, self.status_card)
        self._status_icon.setFixedSize(16, 16)
        self.status = BodyLabel("")
        sl.addWidget(self._status_icon)
        sl.addWidget(self.status, 1)
        self.status_card.hide()
        root.addWidget(self.status_card)
        root.addStretch(1)

        self.start_btn.clicked.connect(self._start)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn.clicked.connect(self._stop)

    @staticmethod
    def _blank_daily_combo(kind: str) -> ComboBox:
        '创建待接数据源的空白选择框，不提前虚构种子、材料或好友选项。'
        combo = ComboBox()
        combo.addItem("")
        combo.setCurrentIndex(0)
        combo.setMinimumWidth(116)
        combo.setToolTip(f"{kind}选项将在后续功能中加入")
        return combo

    
    def _rebuild_rows(self) -> None:
        self.task_list.clear()
        self._rows = []
        for tid in self.config.order:
            row = _TaskRow(tid, self._TASK_REGISTRY.get(tid, tid), self.config.is_enabled(tid))
            row.switch.checkedChanged.connect(lambda on, t=tid: self._on_toggle(t, on))
            item = QListWidgetItem(self.task_list)
            item.setData(Qt.UserRole, tid)                 
            item.setSizeHint(QSize(0, row.sizeHint().height() + 8))
            self.task_list.addItem(item)
            self.task_list.setItemWidget(item, row)
            self._rows.append(row)
        
        h = sum(self.task_list.sizeHintForRow(i) for i in range(self.task_list.count())) + 8
        self.task_list.setFixedHeight(max(1, h))
        self.task_list.schedule_item_reflow()

    def _on_reorder(self) -> None:
        '拖放完成 → 读列表新顺序存盘 → 重建(确保行控件与新序一致)。'
        new_order = [self.task_list.item(i).data(Qt.UserRole) for i in range(self.task_list.count())]
        self.config.set_order(new_order)
        self._rebuild_rows()

    def _on_toggle(self, task_id: str, on: bool) -> None:
        self.config.set_enabled(task_id, on)

    def _on_farm_route_changed(self, route: str) -> None:
        if route in ("第二列", "第五列"):
            self.config.set_param("farm", "route", route)

    def _set_rows_enabled(self, on: bool) -> None:
        
        self.task_list.setDragDropMode(
            QAbstractItemView.InternalMove if on else QAbstractItemView.NoDragDrop)
        for r in self._rows:
            r.switch.setEnabled(on)
        for widget in self._daily_option_widgets:
            widget.setEnabled(on)

    
    def _start(self) -> None:
        if self._paused and self._worker:
            self._toggle_pause()
            return
        if self._worker:
            return
        if not self.config.run_list():
            InfoBar.warning("没有启用的任务", "先在下方勾选要跑的每日任务", duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        self._resume_realtime = _suspend_realtime_for(self, "每日任务")
        ok, reason = registry.start("每日任务")
        if not ok:
            _resume_realtime_for(self, "每日任务", self._resume_realtime)
            self._resume_realtime = False
            InfoBar.warning("任务已在运行", reason, duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        if not is_admin():
            InfoBar.warning("需要管理员", "请以管理员权限重启后再开始。",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
        self._paused = False
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("暂停")
        self.stop_btn.setEnabled(True)
        self._set_rows_enabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self._show_status("每日任务一条龙启动中…")
        self._worker = DailyWorker()
        self._worker.sig_log.connect(self._append)
        self._worker.sig_progress.connect(self._on_progress)
        self._worker.sig_done.connect(self._on_done)
        registry.set_stopper("每日任务", self._worker.stop)
        _minimize_for_task(self, self._append, after=self._worker.start)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)

    def _toggle_pause(self) -> None:
        if not self._worker:
            return
        self._paused = not self._paused
        self._worker.set_paused(self._paused)
        self.pause_btn.setText("继续" if self._paused else "暂停")
        self.start_btn.setEnabled(self._paused)
        self._append("已暂停" if self._paused else "已继续")

    def _stop(self) -> None:
        if self._worker:
            self._append("停止中…")
            self._worker.stop()

    def _on_done(self) -> None:
        if self._worker:
            self._worker.wait(1500)
            self._worker = None
        registry.finish("每日任务")
        resume_realtime = self._resume_realtime
        self._resume_realtime = False
        self._paused = False
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("暂停")
        self.stop_btn.setEnabled(False)
        self.progress.hide()
        self._set_rows_enabled(True)
        _resume_realtime_for(self, "每日任务", resume_realtime)

    def emergency_stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._append("F12 急停")
        release_known_keys(self._append)

    
    def _show_status(self, msg: str) -> None:
        self._last_msg = msg
        self.status_card.show()
        self._set_status_text(msg)

    def _append(self, msg: str) -> None:
        self._last_msg = msg
        self._set_status_text(msg)

    def _set_status_text(self, msg: str) -> None:
        '仅最终总结允许随可用宽度换行；运行过程日志继续保持单行。'
        is_summary = msg.strip().startswith("每日任务一条龙:完成")
        self.status.setWordWrap(is_summary)
        self.status.setText(msg)
        self.status.updateGeometry()
        layout = self.status_card.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
        self.status_card.updateGeometry()
        self._refresh_responsive_layout()


class ListEditDialog(MessageBoxBase):
    '点开才出现的名单编辑弹窗(编辑一个 .txt:一行一个, 注释)。'

    def __init__(self, file, title, tip, placeholder, parent=None) -> None:
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(title, self)
        cap = CaptionLabel(tip, self)
        cap.setWordWrap(True)
        self.edit = TextEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFixedSize(460, 300)
        try:
            self.edit.setPlainText(file.read_text(encoding="utf-8"))
        except Exception:
            pass
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(cap)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(520)

    def text(self) -> str:
        return self.edit.toPlainText()


class SettingsInterface(ScrollInterface):
    '设置(简洁卡片版,对齐实时检测):每项两行 = 标题 + 说明。'

    def __init__(self) -> None:
        super().__init__("settingsInterface")

        root = self.vbox
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        root.addWidget(TitleLabel("设置"))

        
        self.whitelist_card = PushSettingCard(
            "编辑名单", FIF.ADD, "采集白名单", "优先采集名单中的目标")
        self.whitelist_card.clicked.connect(self._edit_whitelist)
        root.addWidget(self.whitelist_card)

        
        self.blacklist_card = PushSettingCard(
            "编辑名单", FIF.BROOM, "采集碰撞名单", "遇到名单中的目标时停止采集")
        self.blacklist_card.clicked.connect(self._edit_blacklist)
        root.addWidget(self.blacklist_card)

        
        self.jitter_card = SwitchSettingCard(
            FIF.ROBOT, "时序抖动", "随机调整操作间隔")
        self.jitter_card.setChecked(bool(cfg.get("timing_jitter")))
        self.jitter_card.checkedChanged.connect(lambda on: cfg.set("timing_jitter", bool(on)))
        root.addWidget(self.jitter_card)

        
        self.monthly_card = SwitchSettingCard(
            FIF.CALENDAR, "月卡奖励", "自动领取每日奖励")
        self.monthly_card.setChecked(bool(cfg.get("monthly_card_enabled")))
        self.monthly_card.checkedChanged.connect(
            lambda on: cfg.set("monthly_card_enabled", bool(on)))
        root.addWidget(self.monthly_card)

        
        if is_admin():
            self.status_card = SettingCard(
                FIF.UPDATE, "运行状态", "显示权限状态并支持 F12 急停")
        else:
            self.status_card = PushSettingCard(
                "以管理员重启", FIF.UPDATE, "运行状态",
                "显示权限状态并支持 F12 急停")
            self.status_card.clicked.connect(self._relaunch_admin)
        root.addWidget(self.status_card)

        root.addStretch(1)

    
    def _edit_whitelist(self) -> None:
        from gather.recognizer import whitelist_file
        self._edit_list(
            whitelist_file(), "采集白名单", "一行一个",
            "一行一个,例如:\n某稀有材料\n某宝箱", "白名单")

    def _edit_blacklist(self) -> None:
        from gather.recognizer import blacklist_file
        self._edit_list(
            blacklist_file(), "采集碰撞名单", "一行一个",
            "一行一个,例如:\n某不想采的交互", "碰撞名单")

    def _edit_list(self, file, title, tip, placeholder, label) -> None:
        dlg = ListEditDialog(file, title, tip, placeholder, self.window())
        if not dlg.exec():
            return
        text = dlg.text()
        try:
            atomic_write_text(file, text, encoding="utf-8")
        except Exception as e:
            dev_log(f"名单保存失败: {file}", e)
            InfoBar.error("保存失败", str(e), duration=4000,
                          position=InfoBarPosition.TOP, parent=self)
            return
        n = len([ln for ln in text.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")])
        InfoBar.success("已保存", f"{label}已写入 {n} 条",
                        duration=3000, position=InfoBarPosition.TOP, parent=self)

    def _relaunch_admin(self) -> None:
        try:
            relaunch_as_admin()
        except Exception as e:
            InfoBar.error("无法提权重启", str(e), duration=4000,
                          position=InfoBarPosition.TOP, parent=self)

class AboutInterface(ScrollInterface):
    def __init__(self) -> None:
        super().__init__("aboutInterface")
        lo = self.vbox
        lo.setContentsMargins(28, 22, 28, 22)
        lo.addWidget(TitleLabel("关于"))
        lo.addWidget(BodyLabel("HOKWorld — 《王者荣耀世界》黑盒视觉自动化"))
        lo.addWidget(CaptionLabel("仅黑盒视觉 + 标准键鼠;不读内存/不注入/不改封包。"))
        lo.addStretch(1)


class MainWindow(FluentWindow):
    
    emergencyStopRequested = Signal()

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
        self.daily = DailyInterface()
        self.fishing = FishingInterface()
        self.settings = SettingsInterface()
        self.about = AboutInterface()
        self.addSubInterface(self.realtime, _nav_icon("realtime.png", FIF.VIDEO), "实时检测")
        self.addSubInterface(self.daily, _nav_icon("daily.png", FIF.CALENDAR), "每日任务")
        self.addSubInterface(self.fishing, _nav_icon("task.png", FIF.GAME), "独立任务")
        self.addSubInterface(self.settings, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.about, FIF.INFO, "关于", NavigationItemPosition.BOTTOM)

        
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
        self._closing = False
        self._close_ready = False
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self._poll_close_ready)
        self.emergencyStopRequested.connect(self._handle_emergency_stop)
        if keyboard is not None:
            self._hotkey = keyboard.Listener(on_press=self._on_key)
            self._hotkey.start()


    def _on_key(self, key) -> None:
        'pynput 线程回调:立即停动作,但绝不直接读写 Qt 控件。'
        try:
            if key == keyboard.Key.f12:
                registry.stop_all("F12 急停")
                self.emergencyStopRequested.emit()
        except Exception as exc:
            dev_log("F12 急停处理失败", exc)

    def _handle_emergency_stop(self) -> None:
        'GUI 主线程槽:只在这里更新各页面的急停状态。'
        self.fishing.emergency_stop()
        self.realtime.emergency_stop()
        self.daily.emergency_stop()

    def _background_workers(self) -> list[QThread]:
        '当前仍由主窗口持有的全部 QThread,用于关闭前统一等待。'
        workers = [
            self.fishing._worker,
            self.realtime._launcher,
            self.realtime._worker,
            self.realtime._gather,
            self.daily._worker,
        ]
        
        return list(dict.fromkeys(w for w in workers if w is not None))

    def _begin_close(self) -> None:
        '只执行一次的协作式停机:停输入、停监听、取消下载,不强杀线程。'
        dev_log("主窗口关闭:开始统一停止后台线程")
        registry.stop_all("主窗口关闭")
        self.realtime._aborting = True
        self.realtime._stop_workers_no_ui()
        if self.fishing._worker:
            self.fishing._worker.stop()
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


def _present_main_window(win: MainWindow) -> None:
    '程序启动完成后把主窗口显示到前台一次；此后不再重试或抢前台。'
    if not win.isVisible() or win.isMinimized():
        win.showNormal()
    if win.isActiveWindow():
        dev_log("主窗口启动前台显示:Qt 已激活")
        return
    try:
        win.raise_()  
    except Exception as exc:
        dev_log("Qt 主窗口启动置顶失败", exc)
    try:
        import ctypes
        hwnd = int(win.winId())
        user32 = ctypes.windll.user32
        user32.ShowWindowAsync(ctypes.c_void_p(hwnd), 9 if win.isMinimized() else 5)
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(0), 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0040)
        ok = bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
        dev_log(f"主窗口启动前台显示:{'成功' if ok else '系统拒绝'} hwnd={hwnd}")
    except Exception as exc:
        dev_log("Windows 主窗口启动激活失败", exc)


def main() -> int:
    hide_console()              
    
    if not is_admin():
        try:
            relaunch_as_admin()
        except Exception as exc:
            dev_log("主程序提权重启失败", exc)
            raise
        return 0
    dev_log(f"主程序启动:pid={os.getpid()} admin=True python={sys.executable}")
    set_app_id()                
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(_nav_icon("app.png", QIcon()))   
    win = build_window()
    center_window(win)          
    win.showNormal()
    dev_log(f"主窗口已创建:hwnd={int(win.winId())}")
    
    QTimer.singleShot(0, lambda: _present_main_window(win))
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        dev_log("主程序启动致命异常", exc)
        raise
