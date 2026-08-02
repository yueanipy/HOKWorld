'实时检测页面。'
from __future__ import annotations

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ExpandGroupSettingCard,
    FluentIcon as FIF,
    IconWidget, IndeterminateProgressRing, InfoBar, InfoBarPosition,
    PrimaryPushButton, PushButton, SwitchButton,
)

from app_workers import GatherWorker, LaunchWorker, StoryWorker
from config import cfg
from winenv import (
    activate_game_window, can_auto_activate_game, find_game_hwnd, is_admin,
    is_foreground,
)
from runtime_guard import registry, release_known_keys

from .shared import ScrollInterface, _LatestStatusBatcher, _minimize_for_task

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

        self._status_batch = _LatestStatusBatcher(self, self._apply_status_message)
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
        self.status_card.show()
        self._status_batch.show_now(msg)

    def _append(self, msg: str) -> None:
        self._status_batch.push(msg)

    def _apply_status_message(self, msg: str) -> None:
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

