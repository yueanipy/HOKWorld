"""HOKWorld 控制台 — Fluent 界面。默认以管理员启动(发真实输入需要),全局 F12 急停。"""
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
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ExpandGroupSettingCard, FluentIcon as FIF,
    FluentWindow, HyperlinkButton, IconWidget, InfoBar, InfoBarPosition, MessageBoxBase,
    NavigationItemPosition, PrimaryPushButton, PushButton, PushSettingCard, SettingCard,
    SingleDirectionScrollArea, SpinBox, SubtitleLabel, SwitchButton, SwitchSettingCard,
    TextEdit, TitleLabel, setTheme, setThemeColor, Theme,
)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import applog  # noqa: E402
from config import cfg  # noqa: E402
from paths import resource_path  # noqa: E402
from runtime_guard import dev_log, registry, release_known_keys  # noqa: E402
from version import APP_DISPLAY, GITHUB_OWNER, GITHUB_REPO, __version__  # noqa: E402
from winenv import center_window, hide_console, is_admin, relaunch_as_admin, set_app_id  # noqa: E402

APP_VERSION = f"v{__version__}"

try:
    from pynput import keyboard
except Exception:
    keyboard = None

ASSETS = resource_path("assets")


def _nav_icon(name, fallback):
    """assets/ 下有同名 png 就用,否则回退内置图标。"""
    p = ASSETS / name
    return QIcon(str(p)) if p.exists() else fallback


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
        # 热重载钓鱼代码:改逻辑后点开始即生效,无需重启控制台
        import importlib
        import winenv
        import fishing.matcher
        import fishing.fisher
        for m in (winenv, fishing.matcher, fishing.fisher):
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
        import winenv
        import capture
        import story.recognizer
        import story.skipper
        for m in (winenv, capture, story.recognizer, story.skipper):
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


class GatherWorker(QThread):
    """实时自动采集线程(热重载 gather 代码)。"""
    sig_log = Signal(str)
    sig_count = Signal(int)
    sig_done = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.bot = None

    def run(self) -> None:
        import importlib
        import winenv
        import capture
        import gather.recognizer
        import gather.picker
        for m in (winenv, capture, gather.recognizer, gather.picker):
            try:
                importlib.reload(m)
            except Exception:
                pass
        from gather.picker import GatherPicker
        self.bot = GatherPicker(log=self.sig_log.emit, on_count=self.sig_count.emit)
        try:
            self.bot.run()
        except Exception as exc:
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
        self.sig_done.emit()

    def stop(self) -> None:
        if self.bot:
            self.bot.stop()

    def set_paused(self, on: bool) -> None:
        if self.bot:
            self.bot.set_paused(on)


class LaunchWorker(QThread):
    """自动启动游戏线程(热重载 launcher;**不**重载 fishing.matcher,保住 OCR 单例不被重置)。"""
    sig_log = Signal(str)
    sig_done = Signal(bool)        # 是否成功进入游戏(已点「开始游戏」)

    def __init__(self) -> None:
        super().__init__()
        self.bot = None

    def run(self) -> None:
        import importlib
        try:
            import winenv
            import capture
            import launcher
            for m in (winenv, capture, launcher):
                try:
                    importlib.reload(m)
                except Exception:
                    pass
            from launcher import GameLauncher
        except Exception as exc:
            dev_log("加载 launcher 失败,跳过自动启动", exc)
            self.sig_log.emit(f"自动启动模块加载失败,已跳过(不影响实时检测):{type(exc).__name__}")
            self.sig_done.emit(False)
            return
        self.bot = GameLauncher(log=self._log)
        ok = False
        try:
            ok = bool(self.bot.run())
        except Exception as exc:
            dev_log("自动启动游戏线程异常", exc)
            release_known_keys(self.sig_log.emit)
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
            self.sig_log.emit("自动启动出错,已跳过(不影响实时检测)")
        self.sig_done.emit(ok)

    def _log(self, msg: str) -> None:
        dev_log(f"[launcher] {msg}")
        self.sig_log.emit(msg)

    def stop(self) -> None:
        if self.bot:
            self.bot.stop()

    def set_paused(self, on: bool) -> None:
        if self.bot:
            self.bot.set_paused(on)


class ScrollInterface(QWidget):
    """可滚动页面基类:子类把控件加到 self.vbox(置于垂直滚动视图中)。
    - 内容未超出视口 → 不显示滚动条,滚轮也不滚(不会把页面/窗口拉长);
    - 内容超出可视范围 → 自动出现细滚动条,可向下滑;悬停/按住拖动时滚动条变粗(参考 MaaNTE);
    - 窗口放大到能容纳(如全屏)→ 滚动条自动隐藏。
    """

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


class FishingInterface(ScrollInterface):
    _CARD_DESC = "自动完成多轮钓鱼:抛竿 → 上钩啦 → 拉杆 → 收线 → 结算"

    def __init__(self) -> None:
        super().__init__("fishingInterface")
        self._worker: FishWorker | None = None

        root = self.vbox
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        # 可展开任务卡片:折叠态显示开始/停止,下拉后才显示循环次数 / 完成后退出
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
        self.card.addGroup(FIF.SYNC, "循环次数", "目标钓鱼条数(0 = 只钓一次,达到后自动停止)", self.count_spin)
        self.exit_switch = SwitchButton()
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


class RealtimeInterface(ScrollInterface):
    """实时检测页(对齐 ok-nte「实时触发」):点开始后实时读屏,自动识别跳过剧情;
    可选开启「经过材料自动采集」,与剧情跳过同时跑。无触发状态时不动作。"""
    _DESC = "点开始后实时读屏:自动识别跳过剧情;可选开「经过材料自动采集」一起跑。无触发状态时不动作"

    def __init__(self) -> None:
        super().__init__("realtimeInterface")
        self._worker: StoryWorker | None = None
        self._gather: GatherWorker | None = None
        self._launcher: LaunchWorker | None = None
        self._paused = False
        self._aborting = False                       # 用户在"自动启动"阶段点停止 → 别再续接实时检测
        self._last_msg = ""

        root = self.vbox
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        self.card = ExpandGroupSettingCard(FIF.VIDEO, "实时检测(剧情跳过 + 自动采集)", self._DESC, self)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始")
        self.pause_btn = PushButton(FIF.PAUSE, "暂停")
        self.stop_btn = PushButton(FIF.CLOSE, "停止")
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.card.addWidget(self.start_btn)
        self.card.addWidget(self.pause_btn)
        self.card.addWidget(self.stop_btn)
        self.nudge_switch = SwitchButton()
        self.nudge_switch.setChecked(False)
        self.nudge_switch.setEnabled(False)          # 已废弃:代码不再微动鼠标,避免用户误开导致误解
        self.gather_switch = SwitchButton()
        self.gather_switch.setChecked(True)          # 默认开启:点开始即跳剧情 + 自动采集
        self.card.addGroup(FIF.SYNC, "经过材料自动采集(F)",
                           "经过材料/宝箱/重现自动按 F(按图标识别;NPC/商店/对话/组队不动)。"
                           "误采的交互可在「设置 · 采集碰撞名单」里加一行排除",
                           self.gather_switch)
        # 自动启动游戏(副栏开关):开启后点「开始」先自动启动游戏再实时检测(详细流程见 docs/开发文档.md)
        self.launch_switch = SwitchButton()
        self.launch_switch.setChecked(True)          # 默认开:点「开始」即自动启动游戏(已在游戏则跳过)
        self.card.addGroup(FIF.GAME, "自动启动游戏",
                           "点击「开始」自动启动游戏", self.launch_switch)
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
            self._toggle_pause()           # 暂停中点「开始」= 继续(等同「继续」按钮)
            return
        if self._worker or self._launcher:
            return
        ok, reason = registry.start("实时检测")
        if not ok:
            InfoBar.warning("任务已在运行", reason, duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        if not is_admin():
            InfoBar.warning("需要管理员", "请以管理员重启后再开始(游戏提权)。",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
        self._paused = False
        self._aborting = False
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("暂停")
        self.stop_btn.setEnabled(True)
        registry.set_stopper("实时检测", self._stop_workers_no_ui)
        if self.launch_switch.isChecked():
            # 先按当前界面状态自动启动游戏;进游戏 / 已在游戏后再开始实时检测
            self._show_status("自动启动游戏中…(到游戏后自动开始实时检测;仅前台时动作;F12 急停)")
            self._set_content("自动启动游戏中…")
            self._launcher = LaunchWorker()
            self._launcher.sig_log.connect(self._append)
            self._launcher.sig_done.connect(self._on_launch_then_detect)
            self._launcher.start()
        else:
            self._begin_detection()

    def _on_launch_then_detect(self, ok: bool) -> None:
        if self._launcher:
            self._launcher.wait(1500)
            self._launcher = None
        if self._aborting:                     # ③ 仅"用户主动停 / F12"才不续接;其余一律接实时检测
            self._maybe_reset_ui()
            return
        if not ok:                             # 启动超时/失败也接检测(best-effort 前置;没进游戏则检测自报收尾)
            self._append("自动启动未完成,仍尝试开始实时检测(无游戏则自动收尾)")
        self._begin_detection()                # 启动完成 / 已在游戏 / 启动失败 → 都开始实时检测,启动只是前置不阻断

    def _begin_detection(self) -> None:
        self._show_status("实时检测启动中…(无剧情不动作;游戏需前台;F12 急停)")
        self._worker = StoryWorker(self.nudge_switch.isChecked())
        self._worker.sig_log.connect(self._append)
        self._worker.sig_done.connect(self._on_done)
        self._set_content("运行中…")
        self._worker.start()
        if self.gather_switch.isChecked():
            self._gather = GatherWorker()
            self._gather.sig_log.connect(self._append)
            self._gather.sig_count.connect(lambda n: self._append(f"已采集 {n} 个材料"))
            self._gather.sig_done.connect(self._on_gather_done)
            self._gather.start()

    def _stop_workers_no_ui(self) -> None:
        if self._launcher:
            self._launcher.stop()
        if self._worker:
            self._worker.stop()
        if self._gather:
            self._gather.stop()

    def _toggle_pause(self) -> None:
        if not (self._worker or self._launcher):
            return
        self._paused = not self._paused
        if self._launcher:
            self._launcher.set_paused(self._paused)
        if self._worker:
            self._worker.set_paused(self._paused)
        if self._gather:
            self._gather.set_paused(self._paused)
        self.pause_btn.setText("继续" if self._paused else "暂停")
        self.start_btn.setEnabled(self._paused)   # 暂停时「开始」可按(=继续);运行时不可按
        self._append("已暂停" if self._paused else "已继续")
        self._set_content("已暂停" if self._paused else "运行中…")

    def _stop(self) -> None:
        self._aborting = True
        self._append("停止中…")
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
        if self._gather:                  # 剧情线程结束 → 采集线程一并收尾
            self._gather.stop()
        self._maybe_reset_ui()

    def _on_gather_done(self) -> None:
        if self._gather:
            self._gather.wait(1500)
            self._gather = None
        self._maybe_reset_ui()

    def _maybe_reset_ui(self) -> None:
        if self._worker or self._gather or self._launcher:   # 启动/剧情/采集全部结束才复位按钮
            return
        registry.finish("实时检测")
        self._aborting = False
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
        self._aborting = True                  # 急停若发生在"自动启动"阶段,别再续接实时检测
        stopped = False
        for w in (self._worker, self._gather, self._launcher):
            if w:
                w.stop()
                stopped = True
        if stopped:
            self._append("F12 急停")
        release_known_keys(self._append)


class ListEditDialog(MessageBoxBase):
    """点开才出现的名单编辑弹窗(编辑一个 .txt:一行一个,# 注释)。白名单/碰撞名单共用。"""

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
    """设置(简洁卡片版):采集白名单(强制采)/ 碰撞名单(跳过)点开才编辑;时序抖动;运行状态。
    名单编辑的是「用户数据目录」里的文件(随更新/换机保留),不是安装目录的模板。
    """

    def __init__(self) -> None:
        super().__init__("settingsInterface")

        root = self.vbox
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        root.addWidget(TitleLabel("设置"))

        # 采集白名单 —— 强制采(优先级最高);点开才编辑
        self.whitelist_card = PushSettingCard(
            "编辑名单", FIF.ADD, "采集白名单(强制采)",
            "写在这里的识别到就一定采(盖过碰撞名单);也能强制采图标不是手型的东西")
        self.whitelist_card.clicked.connect(self._edit_whitelist)
        root.addWidget(self.whitelist_card)

        # 采集碰撞名单 —— 跳过;点开才编辑
        self.blacklist_card = PushSettingCard(
            "编辑名单", FIF.BROOM, "采集碰撞名单(跳过)",
            "不想自动采的(如渡石/滑索/冲云翼),点开编辑;采集时会跳过名单里的提示")
        self.blacklist_card.clicked.connect(self._edit_blacklist)
        root.addWidget(self.blacklist_card)

        # 时序抖动 —— 开关
        self.jitter_card = SwitchSettingCard(
            FIF.ROBOT, "时序抖动", "光标移动时添加细微随机手颤,更拟人(默认关闭)")
        self.jitter_card.setChecked(bool(cfg.get("timing_jitter")))
        self.jitter_card.checkedChanged.connect(lambda on: cfg.set("timing_jitter", bool(on)))
        root.addWidget(self.jitter_card)

        # 运行状态 —— 管理员 + 急停;非管理员时右侧给「以管理员重启」
        if is_admin():
            self.status_card = SettingCard(
                FIF.UPDATE, "运行状态", "管理员运行:是 · 可向游戏发送键鼠 · 急停热键 F12")
        else:
            self.status_card = PushSettingCard(
                "以管理员重启", FIF.UPDATE, "运行状态",
                "管理员运行:否 · 合成键鼠会被拦截(识别到却按不动) · 急停热键 F12")
            self.status_card.clicked.connect(self._relaunch_admin)
        root.addWidget(self.status_card)

        root.addStretch(1)

    # ---- 名单编辑(点开;编辑的是用户数据目录里的文件)----
    def _edit_whitelist(self) -> None:
        from gather.recognizer import whitelist_file
        self._edit_list(
            whitelist_file(), "采集白名单(强制采)",
            "写「一定要采的」——一行一个,识别到就一定采(优先级最高,能盖过碰撞名单;# 开头是说明)。",
            "一行一个,例如:\n某稀有材料\n某宝箱", "白名单")

    def _edit_blacklist(self) -> None:
        from gather.recognizer import blacklist_file
        self._edit_list(
            blacklist_file(), "采集碰撞名单(跳过)",
            "填「额外想跳过的」——一行一个(渡石/滑索/冲云翼等已内置);提示里出现这些字就跳过。",
            "一行一个,例如:\n某不想采的交互", "碰撞名单")

    def _edit_list(self, file, title, tip, placeholder, label) -> None:
        dlg = ListEditDialog(file, title, tip, placeholder, self.window())
        if not dlg.exec():
            return
        text = dlg.text()
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text, encoding="utf-8")
        except Exception as e:
            InfoBar.error("保存失败", str(e), duration=4000,
                          position=InfoBarPosition.TOP, parent=self)
            return
        n = len([ln for ln in text.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")])
        InfoBar.success("已保存", f"{label}已写入({n} 条),下次「开始」采集时生效。",
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
        lo.addWidget(BodyLabel(f"{APP_DISPLAY}  ·  {APP_VERSION}"))
        lo.addWidget(BodyLabel("HOKWorld — 《王者荣耀世界》黑盒视觉自动化"))
        lo.addWidget(CaptionLabel("仅黑盒视觉 + 标准键鼠;不读内存/不注入/不改封包。"))
        lo.addWidget(CaptionLabel("配置 / 日志 / 采集名单存在程序目录下的 data\\(随程序、不进 Windows 用户目录)。"))
        lo.addWidget(HyperlinkButton(
            f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}", "项目主页 / 反馈问题"))
        lo.addStretch(1)


class MainWindow(FluentWindow):
    def __init__(self) -> None:
        super().__init__()
        # 顶部标题栏:名称 + 版本。标题栏左侧不放图标,标题文字左移补到图标原位
        self.setWindowTitle(f"HOKWorld  {APP_VERSION}  ·  王者荣耀世界")
        self.setWindowIcon(_nav_icon("app.png", QIcon()))   # 任务栏/exe 图标
        try:
            self.titleBar.iconLabel.hide()                   # 标题栏不显示图标,标题随之左移补位
        except Exception:
            pass
        self.resize(1180, 720)

        self.realtime = RealtimeInterface()
        self.fishing = FishingInterface()
        self.settings = SettingsInterface()
        self.about = AboutInterface()
        self.addSubInterface(self.realtime, _nav_icon("realtime.png", FIF.VIDEO), "实时检测")
        self.addSubInterface(self.fishing, _nav_icon("task.png", FIF.GAME), "独立任务")
        self.addSubInterface(self.settings, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.about, FIF.INFO, "关于", NavigationItemPosition.BOTTOM)

        # 左侧菜单栏:默认展开;窗口缩放不自动变化;点汉堡(三线)才手动折叠成仅图标
        self.navigationInterface.setExpandWidth(170)
        self.navigationInterface.setMinimumExpandWidth(0)   # 窗口变窄也不自动折叠
        self.navigationInterface.setCollapsible(True)        # 允许汉堡手动折叠到仅图标
        self.navigationInterface.setMenuButtonVisible(True)  # 汉堡可见 → 变宽也不自动展开,手动状态保持
        self.navigationInterface.setReturnButtonVisible(False)
        try:
            self.navigationInterface.expand(useAni=False)
        except Exception:
            pass

        self._hotkey = None
        if keyboard is not None:
            self._hotkey = keyboard.Listener(on_press=self._on_key)
            self._hotkey.start()


    def _on_key(self, key) -> None:
        try:
            if key == keyboard.Key.f12:
                registry.stop_all("F12 急停")
                self.fishing.emergency_stop()
                self.realtime.emergency_stop()
        except Exception:
            pass


def build_window() -> MainWindow:
    setTheme(Theme.LIGHT)
    setThemeColor("#2dd4a8")
    return MainWindow()


def main() -> int:
    hide_console()              # 只显示 UI,隐藏控制台窗口
    # 默认以管理员启动:非管理员则提权重启自身
    if not is_admin():
        relaunch_as_admin()
        return 0
    set_app_id()                # 任务栏/Alt+Tab 用本程序图标(app.png)而非 python 宿主图标
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(_nav_icon("app.png", QIcon()))   # 任务栏/Alt+Tab/标题栏统一用 app.png
    win = build_window()
    center_window(win)          # 居中到当前显示器(任意分辨率/缩放)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
