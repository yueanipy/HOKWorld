'每日任务启动前置：按需启动游戏，确认角色 HUD 后运行一条龙。'
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from daily.context import DailyContext
from daily.orchestrator import DailyOrchestrator
from daily.tasks.monthly_card import CLICK_POINT, MonthlyCardWatcher
from launcher import GameLauncher
from runtime_guard import dev_log


class DailyStartupRunner:
    '串联公共游戏启动器、HUD 确认和每日任务调度器。'

    HUD_CONFIRM_FRAMES = 2
    HUD_TIMEOUT_S = 120.0

    def __init__(
        self,
        auto_launch_game: bool,
        log=print,
        on_progress=lambda _done, _total: None,
        input_tick_at_start: int | None = None,
        launcher_factory: Callable | None = None,
        context_factory: Callable | None = None,
        orchestrator_factory: Callable | None = None,
        monthly_factory: Callable | None = None,
    ) -> None:
        self.auto_launch_game = bool(auto_launch_game)
        self.log = log
        self.on_progress = on_progress
        self.input_tick_at_start = input_tick_at_start
        self._launcher_factory = launcher_factory or (
            lambda tick: GameLauncher(log=self.log, input_tick_at_start=tick)
        )
        self._context_factory = context_factory or (lambda: DailyContext(log=self.log))
        self._orchestrator_factory = orchestrator_factory or (
            lambda: DailyOrchestrator(log=self.log, on_progress=self.on_progress)
        )
        self._monthly_factory = monthly_factory or (
            lambda: MonthlyCardWatcher(True, self.log)
        )
        self._stop_event = threading.Event()
        self._paused = False
        self._active_lock = threading.RLock()
        self._launcher = None
        self._context = None
        self._orchestrator = None

    def stop(self) -> None:
        '停止当前所处的启动、HUD 确认或每日任务阶段。'
        self._stop_event.set()
        with self._active_lock:
            launcher = self._launcher
            context = self._context
            orchestrator = self._orchestrator
        if launcher is not None:
            launcher.stop()
        if context is not None:
            context.stop()
        if orchestrator is not None:
            orchestrator.stop()

    def set_paused(self, on: bool) -> None:
        '把暂停状态传播给当前活动阶段。'
        self._paused = bool(on)
        with self._active_lock:
            launcher = self._launcher
            context = self._context
            orchestrator = self._orchestrator
        if launcher is not None:
            launcher.set_paused(self._paused)
        if context is not None:
            context.set_paused(self._paused)
        if orchestrator is not None:
            orchestrator.set_paused(self._paused)

    def run(self) -> bool:
        '完成可选启动前置并运行一条龙，返回是否进入过调度阶段。'
        if self._stop_event.is_set():
            return False
        if self.auto_launch_game and not self._prepare_game():
            if not self._stop_event.is_set():
                self.log("每日任务未进入角色 HUD，本次一条龙已停止")
            return False
        if self._stop_event.is_set():
            return False

        orchestrator = self._orchestrator_factory()
        with self._active_lock:
            self._orchestrator = orchestrator
        try:
            orchestrator.set_paused(self._paused)
            if self._stop_event.is_set():
                orchestrator.stop()
                return False
            orchestrator.run()
            return True
        finally:
            with self._active_lock:
                self._orchestrator = None

    def _prepare_game(self) -> bool:
        '调用公共启动器，并继续等待真正可操作的角色 HUD。'
        self.log("每日任务：正在自动启动游戏")
        launcher = self._launcher_factory(self.input_tick_at_start)
        with self._active_lock:
            self._launcher = launcher
        try:
            launcher.set_paused(self._paused)
            if self._stop_event.is_set():
                launcher.stop()
                return False
            if not bool(launcher.run()):
                return False
        finally:
            with self._active_lock:
                self._launcher = None
        if self._stop_event.is_set():
            return False
        return self._wait_world_hud()

    def _wait_world_hud(self) -> bool:
        '处理可能遮挡 HUD 的月卡界面，只在角色 HUD 稳定后放行。'
        context = self._context_factory()
        watcher = self._monthly_factory()
        with self._active_lock:
            self._context = context
        started = False
        try:
            context.set_paused(self._paused)
            started = bool(context.start())
            if not started:
                return False
            logical_time = getattr(context, "logical_time", time.monotonic)
            deadline = logical_time() + self.HUD_TIMEOUT_S
            hud_count = 0
            monthly_clicked = False
            self.log("每日任务：等待角色 HUD")
            while not self._stop_event.is_set() and logical_time() < deadline:
                if context.should_stop():
                    return False
                frame = context.grab()
                if frame is None:
                    context.sleep(0.15)
                    continue
                state, hits = watcher.classify(frame)
                if state == "monthly":
                    hud_count = 0
                    if not monthly_clicked:
                        self.log("每日任务：检测到月卡界面，关闭后继续等待角色 HUD")
                        if context.click(CLICK_POINT):
                            watcher.mark_clicked()
                            monthly_clicked = True
                            context.sleep(1.0)
                    else:
                        context.sleep(0.2)
                    continue
                if state == "hud":
                    hud_count += 1
                    if hud_count >= self.HUD_CONFIRM_FRAMES:
                        watcher.mark_hud_reached()
                        self.log(f"每日任务：角色 HUD 已确认 {hits}")
                        return True
                else:
                    hud_count = 0
                context.sleep(0.2)
            if not self._stop_event.is_set():
                self.log("每日任务：等待角色 HUD 超时")
            return False
        except Exception as exc:
            dev_log("每日任务启动前置异常", exc)
            self.log(f"每日任务启动失败:{type(exc).__name__}: {exc}")
            return False
        finally:
            try:
                watcher.close()
            finally:
                context.close()
                with self._active_lock:
                    self._context = None
