"""HOKWord 控制台 — Fluent 界面。默认以管理员启动(发真实输入需要),全局 F12 急停。"""
from __future__ import annotations

import sys

# pythonw.exe(无控制台)下 sys.stdout/stderr 为 None,import 期打印(如 qfluentwidgets 横幅)会崩 → 兜个哑流
if sys.stdout is None or sys.stderr is None:
    import os
    _null = open(os.devnull, "w")
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ExpandGroupSettingCard, FluentIcon as FIF,
    FluentWindow, IconWidget, InfoBar, InfoBarPosition, NavigationItemPosition,
    PrimaryPushButton, PushButton, SpinBox, SwitchButton, TitleLabel, setTheme,
    setThemeColor, Theme,
)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

APP_VERSION = "v1.0"   # 显示在顶部标题栏

from recorder import center_window, hide_console, is_admin, relaunch_as_admin  # noqa: E402

try:
    from pynput import keyboard
except Exception:
    keyboard = None


class FishWorker(QThread):
    sig_log = Signal(str)
    sig_count = Signal(int)
    sig_done = Signal()

    def __init__(self, count: int, exit_after: bool) -> None:
        super().__init__()
        self._count = count
        self._exit_after = exit_after
        self.bot = None

    def run(self) -> None:
        # 热重载钓鱼代码:改逻辑后点「开始」即生效,无需重启控制台
        import importlib
        import recorder
        import fishing.matcher
        import fishing.fisher
        for m in (recorder, fishing.matcher, fishing.fisher):
            try:
                importlib.reload(m)
            except Exception:
                pass
        from fishing.fisher import FishingBot
        self.bot = FishingBot(log=self.sig_log.emit, on_count=self.sig_count.emit)
        try:
            self.bot.run(self._count, self._exit_after)
        except Exception as exc:
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
        self.sig_done.emit()

    def stop(self) -> None:
        if self.bot:
            self.bot.stop()


class StoryWorker(QThread):
    """实时剧情跳过线程(热重载 story 代码)。"""
    sig_log = Signal(str)
    sig_count = Signal(int)
    sig_done = Signal()

    def __init__(self, nudge: bool) -> None:
        super().__init__()
        self._nudge = nudge
        self.bot = None

    def run(self) -> None:
        import importlib
        import recorder
        import story.recognizer
        import story.skipper
        for m in (recorder, story.recognizer, story.skipper):
            try:
                importlib.reload(m)
            except Exception:
                pass
        from story.skipper import StorySkipper
        self.bot = StorySkipper(log=self.sig_log.emit, on_count=self.sig_count.emit)
        try:
            self.bot.run(nudge=self._nudge)
        except Exception as exc:
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
        self.sig_done.emit()

    def stop(self) -> None:
        if self.bot:
            self.bot.stop()

    def set_paused(self, on: bool) -> None:
        if self.bot:
            self.bot.set_paused(on)


class FishingInterface(QWidget):
    _CARD_DESC = "自动完成多轮钓鱼:抛竿 → 上钩啦 → 拉杆 → 收线 → 结算"

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("fishingInterface")
        self._worker: FishWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        # 可展开任务卡片:折叠态显示开始/停止,下拉后才显示循环次数 / 完成后退出
        self.card = ExpandGroupSettingCard(FIF.GAME, "自动钓鱼", self._CARD_DESC, self)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始")
        self.stop_btn = PushButton(FIF.PAUSE, "停止")
        self.stop_btn.setEnabled(False)
        self.card.addWidget(self.start_btn)
        self.card.addWidget(self.stop_btn)

        self.count_spin = SpinBox()           # 下拉项 1:循环次数
        self.count_spin.setRange(0, 9999)
        self.count_spin.setValue(0)
        self.count_spin.setFixedWidth(150)
        self.card.addGroup(FIF.SYNC, "循环次数", "目标钓鱼条数(0 = 只钓一次,达到后自动停止)", self.count_spin)
        self.exit_switch = SwitchButton()     # 下拉项 2:完成后退出
        self.card.addGroup(FIF.POWER_BUTTON, "完成后退出", "完成任务后退出游戏 App", self.exit_switch)
        root.addWidget(self.card)

        # 单行运行状态条:点开始后才显示,只显示最新一条
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

    # ---- 自动钓鱼 ----
    def _start(self) -> None:
        if self._worker:
            return
        self._warn_admin()
        self._show_status("自动钓鱼启动中…(游戏需前台;F12 急停)")
        self._worker = FishWorker(self.count_spin.value(), self.exit_switch.isChecked())
        self._worker.sig_log.connect(self._append)
        self._worker.sig_count.connect(lambda n: self._set_card_content(self.card, f"运行中 · 已钓 {n}"))
        self._worker.sig_done.connect(self._on_done)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_card_content(self.card, "运行中…")
        self._worker.start()

    def _stop(self) -> None:
        if self._worker:
            self._append("停止中…")
            self._worker.stop()

    def _on_done(self) -> None:
        if self._worker:
            self._worker.wait(1500)
            self._worker = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_card_content(self.card, self._CARD_DESC)

    # ---- 公共 ----
    def _warn_admin(self) -> None:
        if not is_admin():
            InfoBar.warning("需要管理员", "请以管理员重启后再开始(游戏提权)。",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)

    def _show_status(self, msg: str) -> None:
        self._last_msg = msg
        self.status_card.show()
        self._refresh_status()

    def _set_card_content(self, card, text: str) -> None:
        try:
            card.card.setContent(text)        # 更新卡片副标题(运行态/空闲态)
        except Exception:
            pass

    def _append(self, msg: str) -> None:
        self._last_msg = msg                  # 只保留最新一条
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.status.setText(self._last_msg)

    def emergency_stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._append("F12 急停")


class RealtimeInterface(QWidget):
    """实时检测页:开始/暂停/停止实时读屏,无剧情时不动作,仅进入需触发状态时才处理。"""
    _DESC = "实时读屏,只在进入「可跳过剧情 / 信息面板」等需触发状态时才动作;无剧情时不做任何操作"

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("realtimeInterface")
        self._worker: StoryWorker | None = None
        self._paused = False
        self._last_msg = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        self.card = ExpandGroupSettingCard(FIF.VIDEO, "剧情跳过(实时检测)", self._DESC, self)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始")
        self.pause_btn = PushButton(FIF.PAUSE, "暂停")
        self.stop_btn = PushButton(FIF.CLOSE, "停止")
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.card.addWidget(self.start_btn)
        self.card.addWidget(self.pause_btn)
        self.card.addWidget(self.stop_btn)
        self.nudge_switch = SwitchButton()
        self.card.addGroup(FIF.ROBOT, "鼠标微动唤出控制条",
                           "剧情控制条若会自动隐藏则开启(每秒极小幅移动鼠标唤出)", self.nudge_switch)
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
        if self._worker:
            return
        if not is_admin():
            InfoBar.warning("需要管理员", "请以管理员重启后再开始(游戏提权)。",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
        self._paused = False
        self._show_status("实时检测启动中…(无剧情不动作;游戏需前台;F12 急停)")
        self._worker = StoryWorker(self.nudge_switch.isChecked())
        self._worker.sig_log.connect(self._append)
        self._worker.sig_count.connect(lambda n: self._set_content(f"运行中 · 已跳过 {n}"))
        self._worker.sig_done.connect(self._on_done)
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("暂停")
        self.stop_btn.setEnabled(True)
        self._set_content("运行中…")
        self._worker.start()

    def _toggle_pause(self) -> None:
        if not self._worker:
            return
        self._paused = not self._paused
        self._worker.set_paused(self._paused)
        self.pause_btn.setText("继续" if self._paused else "暂停")
        self._append("已暂停实时检测" if self._paused else "已继续实时检测")
        self._set_content("已暂停" if self._paused else "运行中…")

    def _stop(self) -> None:
        if self._worker:
            self._append("停止中…")
            self._worker.stop()

    def _on_done(self) -> None:
        if self._worker:
            self._worker.wait(1500)
            self._worker = None
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("暂停")
        self.stop_btn.setEnabled(False)
        self._set_content(self._DESC)

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
        if self._worker:
            self._worker.stop()
            self._append("F12 急停")


class AboutInterface(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("aboutInterface")
        lo = QVBoxLayout(self)
        lo.setContentsMargins(28, 22, 28, 22)
        lo.addWidget(TitleLabel("关于"))
        lo.addWidget(BodyLabel("HOKWord — 《王者荣耀世界》黑盒视觉自动化"))
        lo.addWidget(CaptionLabel("仅黑盒视觉 + 标准键鼠;不读内存/不注入/不改封包。"))
        lo.addStretch(1)


class MainWindow(FluentWindow):
    def __init__(self) -> None:
        super().__init__()
        # 顶部标题栏:图标 + 名称 + 版本
        self.setWindowTitle(f"HOKWord  {APP_VERSION}  ·  王者荣耀世界")
        try:
            self.setWindowIcon(FIF.GAME.icon())
        except Exception:
            pass
        self.resize(1180, 720)

        self.realtime = RealtimeInterface()
        self.fishing = FishingInterface()
        self.about = AboutInterface()
        self.addSubInterface(self.realtime, FIF.VIDEO, "实时检测")   # 第一个
        self.addSubInterface(self.fishing, FIF.GAME, "独立任务")
        self.addSubInterface(self.about, FIF.INFO, "关于", NavigationItemPosition.BOTTOM)

        # 左侧菜单栏:固定展开、不自动收起
        self.navigationInterface.setExpandWidth(170)
        self.navigationInterface.setMinimumExpandWidth(0)
        self.navigationInterface.setCollapsible(False)
        # 顶部保留汉堡按钮占一格,使菜单项下移;禁用其折叠点击 → 始终展开
        self.navigationInterface.setMenuButtonVisible(True)
        self.navigationInterface.setReturnButtonVisible(False)
        try:
            self.navigationInterface.panel.menuButton.clicked.disconnect()
        except Exception:
            pass
        try:
            self.navigationInterface.expand(useAni=False)   # 启动即展开到 170
        except Exception:
            pass

        self._hotkey = None
        if keyboard is not None:
            self._hotkey = keyboard.Listener(on_press=self._on_key)
            self._hotkey.start()

    def _on_key(self, key) -> None:
        try:
            if key == keyboard.Key.f12:
                self.fishing.emergency_stop()
                self.realtime.emergency_stop()
        except Exception:
            pass


def build_window() -> MainWindow:
    setTheme(Theme.LIGHT)        # 白色客户端
    setThemeColor("#2dd4a8")
    return MainWindow()


def main() -> int:
    hide_console()              # 只显示 UI,隐藏控制台窗口
    # 默认以管理员启动:非管理员则提权重启自身
    if not is_admin():
        relaunch_as_admin()
        return 0
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    win = build_window()
    center_window(win)          # 居中到当前显示器(任意分辨率/缩放),不再启动在右下角
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
