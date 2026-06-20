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

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ExpandGroupSettingCard, FluentIcon as FIF,
    FluentWindow, HyperlinkButton, IconWidget, InfoBar, InfoBarPosition, MessageBox,
    NavigationItemPosition, PrimaryPushButton, ProgressBar, PushButton, SpinBox,
    StrongBodyLabel, SwitchButton, TitleLabel, setTheme, setThemeColor, Theme,
)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import os         # noqa: E402
import threading  # noqa: E402

import applog  # noqa: E402
import updater  # noqa: E402
from config import cfg  # noqa: E402
from paths import resource_path, updates_dir  # noqa: E402
from version import APP_DISPLAY, GITHUB_OWNER, GITHUB_REPO, __version__  # noqa: E402
from winenv import center_window, hide_console, is_admin, relaunch_as_admin  # noqa: E402

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
        import story.recognizer
        import story.skipper
        for m in (winenv, story.recognizer, story.skipper):
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


class RealtimeInterface(QWidget):
    """实时检测页:开始/暂停/停止实时读屏,无剧情时不动作,仅进入需触发状态时才处理。"""
    _DESC = "实时读屏,只在进入「可跳过剧情」等需触发状态时才动作;无剧情时不做任何操作"

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
        self.card.addGroup(FIF.ROBOT, "鼠标微动唤出控制条（不稳定）",
                           "剧情控制条若会自动隐藏则开启(沉浸式剧情里平滑微动鼠标唤出)", self.nudge_switch)
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


class UpdateCheckWorker(QThread):
    """后台查最新 Release(不阻塞 UI)。"""
    sig_result = Signal(object)   # ReleaseInfo
    sig_error = Signal(str)

    def run(self) -> None:
        try:
            self.sig_result.emit(updater.fetch_latest())
        except Exception as exc:
            self.sig_error.emit(f"{type(exc).__name__}: {exc}")


class UpdateDownloadWorker(QThread):
    """后台下载安装器(发布附 .sha256 时才做 SHA-256 校验),可取消。"""
    sig_progress = Signal(int, int)   # done, total(bytes)
    sig_done = Signal(str)            # 本地安装器路径
    sig_error = Signal(str)
    sig_cancel = Signal()

    def __init__(self, info) -> None:
        super().__init__()
        self.info = info
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            name = self.info.installer_name or f"HOKWorldScript-{self.info.version}-Setup.exe"
            dest = updates_dir() / name
            updater.download(self.info.installer_url, dest,
                             progress=lambda d, t: self.sig_progress.emit(d, t),
                             cancel=self._cancel)
            sha = updater.fetch_sha256(self.info)   # 发布附了 .sha256 才校验,否则跳过
            if sha and not updater.verify_sha256(dest, sha):
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.sig_error.emit("SHA-256 校验失败,已删除下载文件(继续使用当前版本)")
                return
            self.sig_done.emit(str(dest))
        except updater.CancelledError:
            self.sig_cancel.emit()
        except Exception as exc:
            self.sig_error.emit(f"下载失败:{type(exc).__name__}: {exc}")


class SettingsInterface(QWidget):
    """设置:安全(演练 / 真实输入 / 时序抖动)+ 在线更新(检查 / 下载 / 校验 / 跳过)。"""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("settingsInterface")
        self._check_worker: UpdateCheckWorker | None = None
        self._dl_worker: UpdateDownloadWorker | None = None
        self._pending = None        # 待安装的 ReleaseInfo
        self._manual = False        # 本次检查是否手动触发(手动才弹提示)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        root.addWidget(TitleLabel("设置"))

        # —— 安全 ——
        sec = CardWidget()
        sv = QVBoxLayout(sec)
        sv.setContentsMargins(18, 14, 18, 14)
        sv.setSpacing(8)
        sv.addWidget(StrongBodyLabel("安全"))
        self.dry_sw = self._switch_row(
            sv, "演练模式", "只识别不发送任何键鼠(推荐先用此模式确认识别正常)",
            "dry_run", self._on_dry)
        self.real_sw = self._switch_row(
            sv, "真实输入", "允许向游戏发送键鼠;请在合法授权下使用,后果自负",
            "real_input", self._on_real)
        self.jitter_sw = self._switch_row(
            sv, "时序抖动", "光标移动时添加细微随机抖动(默认关闭)",
            "timing_jitter", lambda on: cfg.set("timing_jitter", bool(on)))
        self.safety_state = CaptionLabel("")
        sv.addWidget(self.safety_state)
        root.addWidget(sec)
        self._refresh_safety()

        # —— 在线更新 ——
        upd = CardWidget()
        uv = QVBoxLayout(upd)
        uv.setContentsMargins(18, 14, 18, 14)
        uv.setSpacing(8)
        uv.addWidget(StrongBodyLabel("在线更新"))

        top = QHBoxLayout()
        top.addWidget(BodyLabel(f"当前版本 {APP_VERSION}"))
        top.addStretch(1)
        top.addWidget(HyperlinkButton(
            f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}", "项目主页"))
        uv.addLayout(top)

        self.startup_sw = self._switch_row(
            uv, "启动时检查更新", "每次启动后台静默检查新版本",
            "check_update_on_startup",
            lambda on: cfg.set("check_update_on_startup", bool(on)))

        self.update_status = CaptionLabel("")
        uv.addWidget(self.update_status)

        self.notes_label = BodyLabel("")
        self.notes_label.setWordWrap(True)
        self.notes_label.hide()
        uv.addWidget(self.notes_label)

        btn_row = QHBoxLayout()
        self.check_btn = PushButton(FIF.SYNC, "检查更新")
        self.check_btn.clicked.connect(lambda: self.check_update(manual=True))
        btn_row.addWidget(self.check_btn)
        btn_row.addStretch(1)
        uv.addLayout(btn_row)

        # 发现新版本后才显示:立即更新 / 跳过此版本
        self.action_widget = QWidget()
        aw = QHBoxLayout(self.action_widget)
        aw.setContentsMargins(0, 0, 0, 0)
        self.update_btn = PrimaryPushButton(FIF.DOWNLOAD, "立即更新")
        self.update_btn.clicked.connect(self._start_download)
        self.skip_btn = PushButton("跳过此版本")
        self.skip_btn.clicked.connect(self._skip_version)
        aw.addWidget(self.update_btn)
        aw.addWidget(self.skip_btn)
        aw.addStretch(1)
        self.action_widget.hide()
        uv.addWidget(self.action_widget)

        # 下载中才显示:进度条 + 取消
        self.progress_widget = QWidget()
        pw = QHBoxLayout(self.progress_widget)
        pw.setContentsMargins(0, 0, 0, 0)
        self.progress = ProgressBar()
        self.progress_label = CaptionLabel("")
        self.cancel_btn = PushButton("取消")
        self.cancel_btn.clicked.connect(self._cancel_download)
        pw.addWidget(self.progress, 1)
        pw.addWidget(self.progress_label)
        pw.addWidget(self.cancel_btn)
        self.progress_widget.hide()
        uv.addWidget(self.progress_widget)

        root.addWidget(upd)
        root.addStretch(1)

    # ---- 安全 ----
    def _switch_row(self, parent, title, desc, key, on_toggle):
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(BodyLabel(title))
        col.addWidget(CaptionLabel(desc))
        sw = SwitchButton()
        sw.setChecked(bool(cfg.get(key)))
        sw.checkedChanged.connect(on_toggle)
        row.addLayout(col, 1)
        row.addWidget(sw, 0, Qt.AlignVCenter)
        parent.addLayout(row)
        return sw

    def _on_dry(self, on: bool) -> None:
        cfg.set("dry_run", bool(on))
        self._refresh_safety()

    def _on_real(self, on: bool) -> None:
        cfg.set("real_input", bool(on))
        if on and not cfg.get("dry_run"):
            InfoBar.warning("真实输入已启用", "下次「开始」起会真实操作游戏,请确认在合法授权下使用。",
                            duration=5000, position=InfoBarPosition.TOP, parent=self)
        self._refresh_safety()

    def _refresh_safety(self) -> None:
        armed = cfg.inputs_armed()
        self.safety_state.setText(
            "当前:真实输入已启用(下次「开始」起会操作游戏)" if armed
            else "当前:演练模式(只识别,不发送任何输入);需同时『真实输入开 + 演练模式关』才会操作游戏")

    # ---- 更新 ----
    def _set_update_status(self, text: str) -> None:
        self.update_status.setText(text)

    def check_update(self, manual: bool) -> None:
        if self._check_worker or self._dl_worker:
            return
        self._manual = manual
        self.check_btn.setEnabled(False)
        if manual:
            self._set_update_status("正在检查更新…")
        self._check_worker = UpdateCheckWorker()
        self._check_worker.sig_result.connect(self._on_check_result)
        self._check_worker.sig_error.connect(self._on_check_error)
        self._check_worker.finished.connect(self._check_done)
        self._check_worker.start()

    def _check_done(self) -> None:
        self._check_worker = None
        self.check_btn.setEnabled(True)

    def _on_check_error(self, msg: str) -> None:
        applog.log(f"更新检查失败:{msg}")
        if not self._manual:
            return
        if "404" in msg:
            InfoBar.info("暂无发布版本", "GitHub 上还没有正式 Release。",
                         duration=4000, position=InfoBarPosition.TOP, parent=self)
        else:
            InfoBar.warning("检查更新失败", msg, duration=5000,
                            position=InfoBarPosition.TOP, parent=self)
        self._set_update_status("检查更新失败")

    def _on_check_result(self, info) -> None:
        if not info.version or not updater.is_newer(info.version):
            applog.log(f"已是最新版本 {APP_VERSION}(最新 {info.version or '?'})")
            self._set_update_status(f"已是最新版本 {APP_VERSION}")
            if self._manual:
                InfoBar.success("已是最新", f"当前 {APP_VERSION} 已是最新版本。",
                                duration=4000, position=InfoBarPosition.TOP, parent=self)
            return
        if not info.has_installer:
            applog.log(f"发现 v{info.version} 但无安装包资源")
            if self._manual:
                InfoBar.warning("无可用安装包", f"v{info.version} 未附带安装器。",
                                duration=5000, position=InfoBarPosition.TOP, parent=self)
            return
        if (not self._manual) and info.version == (cfg.get("skip_version") or ""):
            applog.log(f"发现新版本 v{info.version},但已被用户跳过")
            return
        self._present_update(info)

    def _present_update(self, info) -> None:
        self._pending = info
        notes = (info.notes or "").strip() or "(无更新说明)"
        if len(notes) > 800:
            notes = notes[:800] + " …"
        self.notes_label.setText(f"发现新版本 v{info.version}\n\n{notes}")
        self.notes_label.show()
        self.action_widget.show()
        self.progress_widget.hide()
        self._set_update_status(f"发现新版本 v{info.version}")
        applog.log(f"发现新版本 v{info.version}")

    def _start_download(self) -> None:
        if not self._pending or self._dl_worker:
            return
        self.action_widget.hide()
        self.progress.setValue(0)
        self.progress_label.setText("")
        self.progress_widget.show()
        self._set_update_status("正在下载更新…")
        applog.log(f"开始下载更新 v{self._pending.version}")
        self._dl_worker = UpdateDownloadWorker(self._pending)
        self._dl_worker.sig_progress.connect(self._on_progress)
        self._dl_worker.sig_done.connect(self._on_download_done)
        self._dl_worker.sig_error.connect(self._on_download_error)
        self._dl_worker.sig_cancel.connect(self._on_download_cancel)
        self._dl_worker.finished.connect(self._dl_done)
        self._dl_worker.start()

    def _dl_done(self) -> None:
        self._dl_worker = None

    def _on_progress(self, done: int, total: int) -> None:
        mb = 1024 * 1024
        if total > 0:
            pct = int(done * 100 / total)
            self.progress.setValue(pct)
            self.progress_label.setText(f"{done // mb}/{total // mb} MB ({pct}%)")
        else:
            self.progress_label.setText(f"{done // mb} MB")

    def _cancel_download(self) -> None:
        if self._dl_worker:
            self._dl_worker.cancel()
            self._set_update_status("正在取消下载…")

    def _on_download_cancel(self) -> None:
        self.progress_widget.hide()
        self.action_widget.show()
        self._set_update_status("已取消下载(继续使用当前版本)")
        applog.log("用户取消了更新下载")

    def _on_download_error(self, msg: str) -> None:
        self.progress_widget.hide()
        self.action_widget.show()
        self._set_update_status(msg)
        applog.log(f"更新失败:{msg}")
        InfoBar.warning("更新失败", msg, duration=6000,
                        position=InfoBarPosition.TOP, parent=self)

    def _on_download_done(self, path: str) -> None:
        self.progress_widget.hide()
        applog.log(f"更新已下载:{path}")
        box = MessageBox("下载完成",
                         "安装器已下载完成。\n现在关闭程序并运行安装器升级?\n"
                         "(用户配置与日志保存在 %LOCALAPPDATA%\\HOKWorldScript,升级不会丢失)",
                         self.window())
        box.yesButton.setText("立即安装并退出")
        box.cancelButton.setText("稍后")
        if box.exec():
            if updater.launch_installer(path):
                applog.log("已启动安装器,程序退出以便覆盖升级")
                QApplication.quit()
            else:
                applog.log("安装器启动失败")
                InfoBar.error("启动失败", f"安装器无法启动,请手动运行:\n{path}",
                              duration=8000, position=InfoBarPosition.TOP, parent=self)
                self.action_widget.show()
        else:
            self.action_widget.show()
            self._set_update_status(f"安装包已就绪:{path}")

    def _skip_version(self) -> None:
        if not self._pending:
            return
        v = self._pending.version
        cfg.set("skip_version", v)
        applog.log(f"已跳过版本 v{v}")
        self.notes_label.hide()
        self.action_widget.hide()
        self._set_update_status(f"已跳过 v{v}(不再自动提示)")
        InfoBar.info("已跳过", f"将不再自动提示 v{v}。", duration=4000,
                     position=InfoBarPosition.TOP, parent=self)


class AboutInterface(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("aboutInterface")
        lo = QVBoxLayout(self)
        lo.setContentsMargins(28, 22, 28, 22)
        lo.addWidget(TitleLabel("关于"))
        lo.addWidget(BodyLabel(f"{APP_DISPLAY}  ·  {APP_VERSION}"))
        lo.addWidget(BodyLabel("HOKWorld — 《王者荣耀世界》黑盒视觉自动化"))
        lo.addWidget(CaptionLabel("仅黑盒视觉 + 标准键鼠;不读内存/不注入/不改封包。"))
        lo.addWidget(CaptionLabel("用户配置 / 日志 / 更新包保存在 %LOCALAPPDATA%\\HOKWorldScript。"))
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

        # 启动时静默检查更新(可在「设置」关闭)
        if cfg.get("check_update_on_startup"):
            QTimer.singleShot(1500, lambda: self.settings.check_update(manual=False))

    def _on_key(self, key) -> None:
        try:
            if key == keyboard.Key.f12:
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
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    win = build_window()
    center_window(win)          # 居中到当前显示器(任意分辨率/缩放)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
