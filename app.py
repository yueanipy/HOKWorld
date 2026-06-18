"""HOKWord 控制台 — Fluent 界面(qfluentwidgets,观感对齐 ok-nte / ok-wuthering-waves)。

左侧导航(首项「独立任务」)+ 右侧 Fluent 任务卡片。默认以管理员启动(游戏提权,
发真实输入需要)。全局 F12 急停。
"""
from __future__ import annotations

import sys
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

from recorder import is_admin, relaunch_as_admin  # noqa: E402

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


class FishingInterface(QWidget):
    _CARD_DESC = "自动完成多轮钓鱼:抛竿 → 上钩啦 → 拉杆 → 收线 → 结算"

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("fishingInterface")
        self._worker: FishWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        # ---- 自动钓鱼 可展开任务卡片(观感对齐 ok-nte「自动音游」)----
        # 折叠态:标题 + 副标题 + 开始/停止 + 下拉箭头;下拉后才显示「循环次数 / 完成后退出」。
        self.card = ExpandGroupSettingCard(FIF.GAME, "自动钓鱼", self._CARD_DESC, self)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始")
        self.stop_btn = PushButton(FIF.PAUSE, "停止")
        self.stop_btn.setEnabled(False)
        self.card.addWidget(self.start_btn)
        self.card.addWidget(self.stop_btn)

        self.count_spin = SpinBox()           # 下拉项 1:循环次数
        self.count_spin.setRange(1, 9999)
        self.count_spin.setValue(500)
        self.count_spin.setFixedWidth(150)
        self.card.addGroup(FIF.SYNC, "循环次数", "目标钓鱼条数,达到后自动停止", self.count_spin)
        self.exit_switch = SwitchButton()     # 下拉项 2:完成后退出
        self.card.addGroup(FIF.POWER_BUTTON, "完成后退出", "完成任务后退出游戏 App", self.exit_switch)
        root.addWidget(self.card)

        # ---- 单行运行状态条(参考 MaaNTE:只显示当前最新一条)----
        # 点「开始」后才显示:已钓数 + 最新日志(如钓鱼失败原因)。
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

        self._caught = 0
        self._last_msg = ""
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)

    def _start(self) -> None:
        if self._worker:
            return
        if not is_admin():
            InfoBar.warning("需要管理员", "请以管理员重启后再开始(游戏提权)。",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
        self._caught = 0
        self._last_msg = "启动中…(游戏需前台;F12 急停)"
        self.status_card.show()
        self._refresh_status()
        self._worker = FishWorker(self.count_spin.value(), self.exit_switch.isChecked())
        self._worker.sig_log.connect(self._append)
        self._worker.sig_count.connect(self._on_count)
        self._worker.sig_done.connect(self._on_done)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_card_content("运行中…")
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
        self._set_card_content(self._CARD_DESC)

    def _set_card_content(self, text: str) -> None:
        try:
            self.card.card.setContent(text)   # 更新卡片副标题(运行态/空闲态)
        except Exception:
            pass

    def _on_count(self, n: int) -> None:
        self._caught = n
        self._refresh_status()

    def _append(self, msg: str) -> None:
        self._last_msg = msg                  # 只保留最新一条
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.status.setText(f"已钓 {self._caught} · {self._last_msg}")

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
        # 顶部标题栏:图标 + 名称 + 版本(对齐截图「ok-nte v1.1.8 China」那一行)
        self.setWindowTitle(f"HOKWord  {APP_VERSION}  ·  王者荣耀世界")
        try:
            self.setWindowIcon(FIF.GAME.icon())
        except Exception:
            pass
        self.resize(1180, 720)

        self.fishing = FishingInterface()
        self.about = AboutInterface()
        self.addSubInterface(self.fishing, FIF.GAME, "独立任务")
        self.addSubInterface(self.about, FIF.INFO, "关于", NavigationItemPosition.BOTTOM)

        # 左侧菜单栏:固定展开 + 收窄,占比贴近 MaaNTE
        self.navigationInterface.setExpandWidth(170)
        self.navigationInterface.setMinimumExpandWidth(0)   # 任何窗口宽度都不自动收起
        self.navigationInterface.setCollapsible(False)
        # 顶部保留汉堡按钮:占住顶部一格,使菜单项下移、对齐参考截图;禁用其折叠点击 → 始终展开
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
        except Exception:
            pass


def build_window() -> MainWindow:
    setTheme(Theme.LIGHT)        # 白色客户端
    setThemeColor("#2dd4a8")
    return MainWindow()


def main() -> int:
    # 默认以管理员启动:非管理员则提权重启自身
    if not is_admin():
        relaunch_as_admin()
        return 0
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    win = build_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
