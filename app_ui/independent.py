'独立任务页面。'
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QHBoxLayout
from qfluentwidgets import (
    BodyLabel, CardWidget, ExpandGroupSettingCard, FluentIcon as FIF,
    IconWidget, InfoBar, InfoBarPosition, PrimaryPushButton, PushButton,
    SpinBox, SwitchButton,
)

from app_workers import AutoWaterWorker, FishWorker
from config import cfg
from winenv import is_admin
from route_script.controller import RouteQuickRunCard, RouteScriptInterface
from route_script.store import RouteStore
from runtime_guard import dev_log, registry, release_known_keys

from .shared import (
    ScrollInterface, _LatestStatusBatcher, _minimize_for_task,
    _resume_realtime_for, _suspend_realtime_for,
)

class FishingInterface(ScrollInterface):
    _CARD_DESC = "自动完成钓鱼"

    def __init__(self, *, route_store: RouteStore | None = None) -> None:
        super().__init__("fishingInterface")
        self._worker: FishWorker | None = None
        self._water_worker: AutoWaterWorker | None = None
        self._route_worker = None
        self._route_record_worker = None
        self._paused = False
        self._caught = 0
        self._resume_realtime = False
        self._water_resume_realtime = False
        self._route_resume_realtime = False
        self._route_owner = ""
        self._route_emergency = threading.Event()
        self._route_store = route_store or RouteStore()
        self._route_studio: RouteScriptInterface | None = None

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

        self.water_card = ExpandGroupSettingCard(
            FIF.LEAF, "自动浇水", "定时运行田地和可选牧场", self)
        self.water_start_btn = PrimaryPushButton(FIF.PLAY, "开始")
        self.water_stop_btn = PushButton(FIF.PAUSE, "停止")
        self.water_stop_btn.setEnabled(False)
        self.water_card.addWidget(self.water_start_btn)
        self.water_card.addWidget(self.water_stop_btn)

        self.water_interval_spin = SpinBox()
        self.water_interval_spin.setRange(1, 1440)
        self.water_interval_spin.setValue(
            max(1, int(cfg.get("auto_water_interval_minutes") or 90)))
        self.water_interval_spin.setFixedWidth(150)
        self.water_card.addGroup(
            FIF.SYNC, "间隔时间", "相邻两轮启动间隔（分钟）", self.water_interval_spin)
        self.water_ranch_switch = SwitchButton()
        self.water_ranch_switch.setChecked(
            bool(cfg.get("auto_water_include_ranch")))
        self.water_card.addGroup(
            FIF.TILES,
            "牧场",
            "按每日任务设置的最高开放区域运行",
            self.water_ranch_switch,
        )
        self.water_close_switch = SwitchButton()
        self.water_close_switch.setChecked(bool(cfg.get("auto_water_close_game")))
        self.water_card.addGroup(
            FIF.POWER_BUTTON, "完成后关闭游戏", "等待期间只保留脚本运行", self.water_close_switch)
        self.water_shutdown_spin = SpinBox()
        self.water_shutdown_spin.setRange(0, 720)
        self.water_shutdown_spin.setValue(
            max(0, int(cfg.get("auto_water_shutdown_hours") or 0)))
        self.water_shutdown_spin.setFixedWidth(150)
        self.water_card.addGroup(
            FIF.POWER_BUTTON, "自动关闭", "关闭游戏和软件的小时数，0 表示关闭", self.water_shutdown_spin)
        root.addWidget(self.water_card)

        self.route_card = RouteQuickRunCard(self, store=self._route_store)
        root.addWidget(self.route_card)



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
        self._status_batch = _LatestStatusBatcher(self, self._apply_status_message)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.water_start_btn.clicked.connect(self._start_water)
        self.water_stop_btn.clicked.connect(self._stop_water)
        self.water_interval_spin.valueChanged.connect(
            lambda value: cfg.set("auto_water_interval_minutes", int(value)))
        self.water_close_switch.checkedChanged.connect(
            lambda on: cfg.set("auto_water_close_game", bool(on)))
        self.water_shutdown_spin.valueChanged.connect(
            lambda value: cfg.set("auto_water_shutdown_hours", int(value)))
        self.route_card.playRequested.connect(self._start_route)
        self.route_card.stopRequested.connect(self._stop_route)
        self.route_card.catalogRefreshRequested.connect(
            self._refresh_route_studio_catalog)
        self.route_card.message.connect(self._append)


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


    def _start_water(self) -> None:
        if self._water_worker:
            return
        self._water_resume_realtime = _suspend_realtime_for(self, "自动浇水")
        ok, reason = registry.start("自动浇水")
        if not ok:
            _resume_realtime_for(self, "自动浇水", self._water_resume_realtime)
            self._water_resume_realtime = False
            InfoBar.warning("任务已在运行", reason, duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        self._warn_admin()
        interval = self.water_interval_spin.value()
        include_ranch = self.water_ranch_switch.isChecked()
        close_game = self.water_close_switch.isChecked()
        shutdown_hours = self.water_shutdown_spin.value()
        cfg.set("auto_water_interval_minutes", int(interval), save=False)
        cfg.set("auto_water_include_ranch", bool(include_ranch), save=False)
        cfg.set("auto_water_close_game", bool(close_game), save=False)
        cfg.set("auto_water_shutdown_hours", int(shutdown_hours), save=True)
        self._show_status("自动浇水启动中…")
        self._water_worker = AutoWaterWorker(
            interval,
            close_game,
            include_ranch,
            shutdown_hours,
        )
        self._water_worker.sig_log.connect(self._append)
        self._water_worker.sig_state.connect(self._on_water_state)
        self._water_worker.sig_done.connect(self._on_water_done)
        registry.set_stopper("自动浇水", self._water_worker.stop)
        self.water_start_btn.setEnabled(False)
        self.water_stop_btn.setEnabled(True)
        self.water_interval_spin.setEnabled(False)
        self.water_ranch_switch.setEnabled(False)
        self.water_close_switch.setEnabled(False)
        self.water_shutdown_spin.setEnabled(False)
        self._set_card_content(self.water_card, "运行中…")
        tick = _minimize_for_task(
            self, self._append, handoff=False, after=self._water_worker.start)
        self._water_worker.set_initial_input_tick(tick)

    def _on_water_state(self, state: str) -> None:
        self._set_card_content(self.water_card, state)
        self._append(state)

    def _stop_water(self) -> None:
        if self._water_worker:
            self._append("自动浇水停止中…")
            self.water_stop_btn.setEnabled(False)
            self._water_worker.stop()

    def _on_water_done(self) -> None:
        auto_closing = bool(
            self._water_worker and self._water_worker.auto_close_requested)
        if self._water_worker:
            self._water_worker.wait(1500)
            self._water_worker = None
        registry.finish("自动浇水")
        resume_realtime = self._water_resume_realtime
        self._water_resume_realtime = False
        self.water_start_btn.setEnabled(True)
        self.water_stop_btn.setEnabled(False)
        self.water_interval_spin.setEnabled(True)
        self.water_ranch_switch.setEnabled(True)
        self.water_close_switch.setEnabled(True)
        self.water_shutdown_spin.setEnabled(True)
        self._set_card_content(self.water_card, "定时运行田地和可选牧场")
        if auto_closing:
            self._append("自动关闭时间已到，正在关闭软件")
            window = self.window()
            if window is not None:
                QTimer.singleShot(0, window.close)
        else:
            _resume_realtime_for(self, "自动浇水", resume_realtime)


    def bind_route_interface(self, interface: RouteScriptInterface) -> None:
        '把导航工作台接入独立任务持有的唯一运行控制器。'
        if self._route_studio is interface:
            return
        if self._route_studio is not None:
            raise RuntimeError("路线工作台只能绑定一次")
        self._route_studio = interface
        interface.playRequested.connect(self._start_route)
        interface.stopRequested.connect(self._stop_route)
        interface.recordRequested.connect(self._start_route_recording)
        interface.finishRecordRequested.connect(self._finish_route_recording)
        interface.catalogChanged.connect(self._on_route_catalog_changed)
        interface.message.connect(self._append_route)
        interface.selectionChanged.connect(self.route_card.select_route)
        self.route_card.selectionChanged.connect(interface.select_route)
        self.route_card.refresh_routes(
            select=interface.selected_name(), announce=False)

    def _route_views(self) -> tuple[object, ...]:
        '返回共享同一运行状态的轻量卡片和导航工作台。'
        if self._route_studio is None:
            return (self.route_card,)
        return (self.route_card, self._route_studio)

    def _apply_route_view(self, method: str, *args) -> None:
        '同步两个入口，单个界面销毁时不影响任务停机。'
        for view in self._route_views():
            try:
                getattr(view, method)(*args)
            except RuntimeError:
                continue

    def _refresh_route_views(self, *, select: str = "") -> None:
        for view in self._route_views():
            try:
                view.refresh_routes(select=select, announce=False)
            except RuntimeError:
                continue

    def _refresh_route_studio_catalog(self, selected: str) -> None:
        if self._route_studio is not None:
            self._route_studio.refresh_routes(select=selected, announce=False)

    def _on_route_catalog_changed(self, selected: str) -> None:
        self.route_card.refresh_routes(select=selected, announce=False)

    def shutdown_route_views(self) -> None:
        self._apply_route_view("shutdown")

    def _start_route(self, name: str, loop_count: int,
                     coordinate_correction: bool) -> None:
        '先校验TXT，再让实时检测静默让位并启动路线。'
        if self._route_worker or self._route_record_worker:
            return

        self._route_emergency.clear()
        try:
            route = self._route_store.load(name)
        except Exception as exc:

            message = f"路线配置无效：{exc}"
            self._apply_route_view("mark_invalid", name, str(exc))
            self._append_route(message)
            return

        if self._route_emergency.is_set():
            self._apply_route_view("reset_runtime", "自定义路线已停止")
            return

        self._route_resume_realtime = _suspend_realtime_for(self, "自定义路线")
        ok, reason = registry.start(
            "自定义路线", self._stop_route_playback_from_registry)
        if not ok:
            _resume_realtime_for(
                self, "自定义路线", self._route_resume_realtime)
            self._route_resume_realtime = False
            InfoBar.warning("任务已在运行", reason, duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        self._warn_admin()
        try:
            from route_script.worker import RoutePlaybackWorker

            worker = RoutePlaybackWorker(
                route,
                loop_count=int(loop_count),
                coordinate_correction=bool(coordinate_correction),
            )
        except Exception as exc:
            registry.finish("自定义路线")
            resume = self._route_resume_realtime
            self._route_resume_realtime = False
            if not self._route_emergency.is_set():
                _resume_realtime_for(self, "自定义路线", resume)
            message = f"路线启动失败：{exc}"
            self._apply_route_view("reset_runtime", message)
            self._append_route(message)
            return

        self._route_worker = worker
        self._route_owner = "自定义路线"
        worker.sig_log.connect(self._append_route)
        worker.sig_state.connect(self._on_route_state)
        worker.sig_progress.connect(self._on_route_progress)
        worker.sig_done.connect(self._on_route_done)
        if self._route_emergency.is_set():
            worker.stop()
            from route_script.player import RouteRunResult

            self._on_route_done(RouteRunResult(False, "stopped", 0))
            return
        self._apply_route_view("set_playing")
        self._append_route(f"自定义路线启动中：{name}")
        _minimize_for_task(
            self,
            self._append,
            handoff=True,
            after=self._launch_route_worker,
        )

    def _launch_route_worker(self) -> None:
        worker = self._route_worker
        if worker is None or worker.isRunning():
            return
        if self._route_emergency.is_set():
            worker.stop()
            from route_script.player import RouteRunResult

            self._on_route_done(RouteRunResult(False, "stopped", 0))
            return
        try:
            worker.start(QThread.Priority.NormalPriority)
        except Exception as exc:
            dev_log("自定义路线QThread启动失败", exc)
            self._append_route(f"路线启动失败：{exc}")
            worker.stop()
            self._on_route_done(None)

    def _set_route_paused(self, paused: bool) -> None:
        worker = self._route_worker
        if worker is None:
            return
        worker.set_paused(bool(paused))
        self._apply_route_view("set_paused", bool(paused))
        self._append("路线已暂停" if paused else "路线已继续")

    def _on_route_state(self, state: str) -> None:
        self._apply_route_view("set_runtime_state", state)
        self._append_route(state)

    def _on_route_progress(self, current: int, total: int) -> None:
        self._apply_route_view("set_progress", current, total)

    def _stop_route(self) -> None:
        if self._route_worker:
            self._apply_route_view("set_stopping", "停止中…")
            self._append_route("自定义路线停止中…")
            self._route_worker.stop()
            return
        if self._route_record_worker:
            self._apply_route_view("set_stopping", "取消录制中…")
            self._append_route("正在取消路线录制…")
            self._route_record_worker.stop(cancel=True)

    def _on_route_done(self, result) -> None:
        worker = self._route_worker
        if worker:
            worker.wait(1500)
            self._route_worker = None
        owner = self._route_owner or "自定义路线"
        registry.finish(owner)
        resume = self._route_resume_realtime
        self._route_resume_realtime = False
        self._route_owner = ""
        window = self.window()
        closing = bool(getattr(window, "_closing", False))
        emergency = self._route_emergency.is_set()
        self._route_emergency.clear()
        reason = str(getattr(result, "reason", "stopped") or "stopped")
        success = bool(getattr(result, "success", False))
        if success:
            skipped_coordinates = int(
                getattr(result, "skipped_checkpoints", 0) or 0)
            skipped = []
            if skipped_coordinates:
                skipped.append(f"坐标{skipped_coordinates}个")
            text = "自定义路线已完成"
            if skipped:
                text += "，已跳过" + "、".join(skipped)
        elif reason == "teleport_failed":
            text = "图谱传送失败，路线已停止"
        elif reason == "game_closed":
            text = "游戏已关闭，路线已停止"
        elif reason == "worker_exception":
            text = "自定义路线异常，已安全停止"
        elif reason == "input_failed":
            text = "路线输入发送失败，已安全停止"
        elif reason == "checkpoint_timeout":
            text = "路线检查点超时，重试后仍失败"
        elif reason == "route_deviation":
            text = "路线坐标偏差过大，重试后仍失败"
        elif reason == "coordinate_unavailable":
            text = "小地图坐标无法确认，重试后仍失败"
        elif reason == "game_not_found":
            text = "未找到游戏窗口，路线未启动"
        elif reason == "initial_frame_missing":
            text = "无法获取游戏画面，路线已停止"
        else:
            text = "自定义路线已停止"
        self._apply_route_view("reset_runtime", text)
        self._append_route(text)
        if not emergency and not closing:
            _resume_realtime_for(self, owner, resume)

    def _start_route_recording(
            self, name: str, start_teleport: str,
            coordinate_correction: bool) -> None:
        if self._route_worker or self._route_record_worker:
            return

        self._route_emergency.clear()
        try:

            clean_name = self._route_store.normalize_name(name)
        except Exception as exc:
            self._append_route(f"无法录制路线：{exc}")
            return
        if self._route_emergency.is_set():
            self._apply_route_view("reset_runtime", "路线录制已取消")
            return
        self._route_resume_realtime = _suspend_realtime_for(self, "路线录制")
        ok, reason = registry.start(
            "路线录制", self._stop_route_recording_from_registry)
        if not ok:
            _resume_realtime_for(self, "路线录制", self._route_resume_realtime)
            self._route_resume_realtime = False
            InfoBar.warning("任务已在运行", reason, duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        self._warn_admin()
        try:
            from route_script.worker import RouteRecordWorker

            worker = RouteRecordWorker(
                clean_name,
                start_teleport=str(start_teleport or "").strip(),
                coordinate_correction=bool(coordinate_correction),
            )
        except Exception as exc:
            registry.finish("路线录制")
            resume = self._route_resume_realtime
            self._route_resume_realtime = False
            if not self._route_emergency.is_set():
                _resume_realtime_for(self, "路线录制", resume)
            self._append_route(f"路线录制启动失败：{exc}")
            return
        self._route_record_worker = worker
        self._route_owner = "路线录制"
        worker.sig_log.connect(self._append_route)
        worker.sig_state.connect(self._on_route_state)
        worker.sig_done.connect(self._on_route_record_done)
        if self._route_emergency.is_set():
            worker.stop(cancel=True)
            self._on_route_record_done(None, "")
            return
        self._apply_route_view("set_recording")
        self._append_route(f"准备录制路线：{clean_name}")
        _minimize_for_task(
            self,
            self._append,
            handoff=True,
            after=self._launch_route_record_worker,
        )

    def _launch_route_record_worker(self) -> None:
        worker = self._route_record_worker
        if worker is None or worker.isRunning():
            return
        if self._route_emergency.is_set():
            worker.stop(cancel=True)
            self._on_route_record_done(None, "")
            return
        try:
            worker.start(QThread.Priority.NormalPriority)
        except Exception as exc:
            dev_log("路线录制QThread启动失败", exc)
            self._append_route(f"路线录制启动失败：{exc}")
            worker.stop(cancel=True)
            self._on_route_record_done(None, str(exc))

    def _finish_route_recording(self) -> None:
        worker = self._route_record_worker
        if worker is None:
            return
        self._apply_route_view("set_stopping", "保存录制中…")
        self._append_route("正在保存路线录制…")
        worker.stop(cancel=False)

    def finish_route_recording_from_hotkey(self) -> None:
        '在 GUI 主线程处理 F12 完成录制请求。'
        self._finish_route_recording()

    def _on_route_record_done(self, path, error: str) -> None:
        worker = self._route_record_worker
        if worker:
            worker.wait(1500)
            self._route_record_worker = None
        owner = self._route_owner or "路线录制"
        registry.finish(owner)
        resume = self._route_resume_realtime
        self._route_resume_realtime = False
        self._route_owner = ""
        window = self.window()
        closing = bool(getattr(window, "_closing", False))
        emergency = self._route_emergency.is_set()
        self._route_emergency.clear()
        if path:
            selected = Path(path).stem
            self._apply_route_view("reset_runtime", "路线录制已保存")
            self._refresh_route_views(select=selected)
            self._append_route(f"路线录制已保存：{selected}")
        elif error:
            self._apply_route_view("reset_runtime", f"路线录制失败：{error}")
            self._append_route(f"路线录制失败：{error}")
        else:
            self._apply_route_view("reset_runtime", "路线录制已取消")
            self._append_route("路线录制已取消")
        if not emergency and not closing:
            _resume_realtime_for(self, owner, resume)

    def route_workers(self) -> tuple[QThread, ...]:
        '返回仍由独立任务页持有的路线线程。'
        return tuple(worker for worker in (
            self._route_worker, self._route_record_worker) if worker is not None)

    def _stop_route_playback_from_registry(self) -> None:
        '供任意线程急停尚未创建完成或正在运行的路线。'
        self._route_emergency.set()
        worker = self._route_worker
        if worker is not None:
            worker.stop()

    def _stop_route_recording_from_registry(self) -> None:
        '供任意线程取消尚未创建完成或正在运行的录制。'
        self._route_emergency.set()
        worker = self._route_record_worker
        if worker is not None:
            worker.stop(cancel=True)

    def stop_route_workers(self, *, emergency: bool = False) -> None:
        '幂等停止播放和录制；急停时录制不保存。'
        if emergency:
            self._route_emergency.set()
        if self._route_worker:
            self._route_worker.stop()
        if self._route_record_worker:
            self._route_record_worker.stop(cancel=True)

    def mark_route_emergency(self) -> None:
        '可从全局热键线程调用；只设置线程安全标记，不触碰Qt控件。'
        self._route_emergency.set()


    def _warn_admin(self) -> None:
        if not is_admin():
            InfoBar.warning("需要管理员", "请以管理员权限重启后再开始。",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)

    def _show_status(self, msg: str) -> None:
        self.status_card.show()
        self._status_batch.show_now(msg)

    def _set_card_content(self, card, text: str) -> None:
        try:
            card.card.setContent(text)
        except Exception:
            pass

    def _append(self, msg: str) -> None:
        self._status_batch.push(msg)

    def _append_route(self, msg: str) -> None:
        '把路线状态同步到独立任务和路线页底部状态栏。'
        self._append(msg)
        studio = self._route_studio
        if studio is not None:
            try:
                studio.show_status(msg)
            except RuntimeError:
                self._route_studio = None

    def _apply_status_message(self, msg: str) -> None:
        self._last_msg = msg
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.status.setText(self._last_msg)

    def emergency_stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._append("F12 急停")
        if self._water_worker:
            self._water_worker.stop()
            self._append("F12 急停")
        if self._route_worker or self._route_record_worker:
            self.stop_route_workers(emergency=True)
            self._append("F12 急停")
        release_known_keys(self._append)
