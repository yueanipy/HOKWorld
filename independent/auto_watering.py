'按固定间隔启动游戏并直接调用两处现有田地任务。'
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

import win32con
import win32gui

from daily import navigation as nav
from daily.base import TaskResult
from daily.context import DailyContext
from daily.tasks.farm import FarmTask
from daily.tasks.incubator import IncubatorTask
from daily.tasks.monthly_card import handle_monthly_card_once
from launcher import GameLauncher
from winenv import (activate_game_window, allow_foreground_activation,
                      can_auto_activate_game, find_game_hwnd, last_input_tick)
from runtime_guard import dev_log, release_known_keys


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_thread_execution_state(flags: int) -> int:
    '设置当前调度线程的系统睡眠要求。'
    import ctypes
    return int(ctypes.windll.kernel32.SetThreadExecutionState(int(flags)))


@contextmanager
def prevent_automatic_sleep(set_execution_state=None, log=print):
    '实际启动和田地操作期间阻止系统自动休眠，结束后恢复默认。'
    setter = set_execution_state or _set_thread_execution_state
    enabled = False
    try:
        enabled = bool(setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
        if not enabled:
            log("未能设置系统防休眠，本轮继续运行")
    except Exception as exc:
        dev_log("自动浇水:设置系统防休眠失败", exc)
        log(f"设置系统防休眠失败:{type(exc).__name__}: {exc}")
    try:
        yield
    finally:
        if enabled:
            try:
                setter(ES_CONTINUOUS)
            except Exception as exc:
                dev_log("自动浇水:恢复系统休眠策略失败", exc)


def close_game_window(hwnd: int, stop_event: threading.Event | None = None,
                      log=print, timeout_s: float = 20.0) -> bool:
    '向任务实际操作的游戏窗口发送正常关闭消息，并有界等待。'
    hwnd = int(hwnd or 0)
    if not hwnd or not win32gui.IsWindow(hwnd):
        log("游戏窗口已经关闭")
        return True
    try:
        release_known_keys(log)
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as exc:
        dev_log("自动浇水:发送游戏关闭消息失败", exc)
        log(f"关闭游戏失败:{type(exc).__name__}: {exc}")
        return False

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while win32gui.IsWindow(hwnd) and time.monotonic() < deadline:
        if stop_event is not None:
            if stop_event.wait(0.2):
                return False
        else:
            time.sleep(0.2)
    if not win32gui.IsWindow(hwnd):
        log("游戏已正常关闭")
        return True
    log("游戏未在限定时间内关闭，已停止等待且不会强杀进程")
    return False


class AutoWaterScheduler:
    '循环调度游戏启动、田地任务、可选关闭和低资源等待。'

    TASKS = (
        ("农贸作物", FarmTask),
        ("培养箱", IncubatorTask),
    )

    def __init__(self, interval_minutes: int = 90, close_game_after: bool = False,
                 shutdown_hours: int = 0,
                 log=print, on_state=lambda _state: None,
                 input_tick_at_start: int | None = None,
                 launcher_factory: Callable | None = None,
                 context_factory: Callable | None = None,
                 task_factories: tuple | None = None,
                 find_game: Callable[[int], int | None] | None = None,
                 activate_game: Callable[[int], bool] | None = None,
                 can_activate_game: Callable[[int | None], bool] | None = None,
                 close_game: Callable[[int], bool] | None = None,
                 back_to_world: Callable | None = None,
                 monthly_handler: Callable | None = None,
                 set_execution_state: Callable[[int], int] | None = None) -> None:
        self.interval_minutes = max(1, int(interval_minutes))
        self.close_game_after = bool(close_game_after)
        self.shutdown_hours = max(0, int(shutdown_hours))
        self.log = log
        self.on_state = on_state
        self._first_input_tick = input_tick_at_start
        self._launcher_factory = launcher_factory or (
            lambda tick: GameLauncher(log=self.log, input_tick_at_start=tick))
        self._context_factory = context_factory or (lambda: DailyContext(log=self.log))
        self._task_factories = task_factories or self.TASKS
        self._find_game = find_game or (
            lambda exclude=0: find_game_hwnd(
                prefer_foreground=False, exclude_hwnd=int(exclude or 0)))
        self._activate_game = activate_game or activate_game_window
        self._can_activate_game = can_activate_game or can_auto_activate_game
        self._close_game = close_game or (
            lambda hwnd: close_game_window(hwnd, self._stop_event, self.log))
        self._back_to_world = back_to_world or nav.back_to_world
        self._monthly_handler = monthly_handler or handle_monthly_card_once
        self._set_execution_state = set_execution_state
        self._stop_event = threading.Event()
        self._active_lock = threading.RLock()
        self._launcher = None
        self._ctx = None
        self._cycle_hwnd = 0
        self._cycle_index = 0
        self._interval_seconds = self.interval_minutes * 60.0
        self._shutdown_seconds = self.shutdown_hours * 3600.0
        self._shutdown_deadline: float | None = None
        self.auto_close_requested = False
        self._last_cycle_stop_reason = ""

    def stop(self) -> None:
        '唤醒等待并协作式停止当前启动器或田地任务。'
        self._stop_event.set()
        with self._active_lock:
            launcher = self._launcher
            ctx = self._ctx
        if launcher is not None:
            launcher.stop()
        if ctx is not None:
            ctx.stop()
        release_known_keys(self.log)

    def _stopped(self) -> bool:
        return self._stop_event.is_set()

    def _state(self, text: str) -> None:
        try:
            self.on_state(str(text))
        except Exception as exc:
            dev_log("自动浇水:状态回调失败", exc)

    def _next_input_tick(self) -> int | None:
        tick = self._first_input_tick
        self._first_input_tick = None
        return tick if tick is not None else last_input_tick()

    def _wait_for_game_window(self, exclude_hwnd: int = 0,
                              timeout_s: float = 20.0) -> int:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while not self._stopped() and time.monotonic() < deadline:
            hwnd = int(self._find_game(exclude_hwnd) or 0)
            if hwnd:
                return hwnd
            self._stop_event.wait(0.2)
        return 0

    def _ensure_game_ready(self) -> bool:
        '由公共启动状态机确认游戏，并且本轮最多主动交接一次正式游戏前台。'
        tick = self._next_input_tick()
        allow_foreground_activation()
        self._state("检查并启动游戏中…")
        self.log("开始确认游戏状态；未启动时将自动启动")
        launcher = self._launcher_factory(tick)
        with self._active_lock:
            self._launcher = launcher
        try:
            if self._stopped():
                launcher.stop()
                return False
            ok = bool(launcher.run())
        finally:
            with self._active_lock:
                self._launcher = None
        if self._stopped() or not ok:
            self.log("本轮自动启动游戏未完成")
            return False

        launcher_hwnd = int(getattr(launcher, "_launcher_hwnd", 0) or 0)
        hwnd = self._wait_for_game_window(launcher_hwnd)
        if not hwnd:
            self.log("启动器已完成，但未找到正式游戏窗口")
            return False
        self._cycle_hwnd = hwnd
        if not bool(getattr(launcher, "_foreground_handoff_attempted", False)):
            if self._can_activate_game(tick):
                self.log("游戏状态已确认，执行本轮唯一一次前台交接")
                if not self._activate_game(hwnd):
                    self.log("本轮唯一一次游戏前台交接失败，等待游戏自然回到前台")
            else:
                self.log("检测到用户在启动期间操作其它程序，本轮不抢前台")
        return True

    def run_cycle(self, stop_deadline: float | None = None) -> dict[str, str]:
        '在防自动休眠保护下执行一轮启动与田地任务。'
        with prevent_automatic_sleep(self._set_execution_state, self.log):
            return self._run_cycle_active(stop_deadline)

    def _run_cycle_active(self, stop_deadline: float | None = None) -> dict[str, str]:
        '立即执行一轮启动与浇水，供循环和离线测试复用。'
        results: dict[str, str] = {}
        self._last_cycle_stop_reason = ""
        self._cycle_hwnd = 0
        if self._stopped() or not self._ensure_game_ready():
            results["launch"] = TaskResult.ABORT if self._stopped() else TaskResult.FAIL
            return results

        ctx = self._context_factory()
        if hasattr(ctx, "set_stop_deadline"):
            ctx.set_stop_deadline(stop_deadline)
        with self._active_lock:
            self._ctx = ctx
        started = False
        try:
            started = bool(ctx.start())
            if not started:
                results["context"] = TaskResult.FAIL
                return results
            
            if not ctx.wait_foreground(timeout=None):
                results["foreground"] = (
                    TaskResult.ABORT if self._stopped() else TaskResult.FAIL)
                return results
            self._cycle_hwnd = int(ctx.hwnd or self._cycle_hwnd)
            try:
                monthly_state = str(self._monthly_handler(ctx, self.log) or "")
                if monthly_state == "clicked":
                    self.log("自动浇水：月卡界面已处理，继续田地任务")
            except Exception as exc:
                
                dev_log("自动浇水:月卡检查异常，已跳过", exc)
                self.log(f"月卡检查失败，已跳过:{type(exc).__name__}")
            for name, factory in self._task_factories:
                if self._stopped() or ctx.should_stop():
                    results[name] = TaskResult.ABORT
                    break
                self._state(f"执行中 · {name}")
                self._back_to_world(ctx)
                try:
                    task = factory(ctx)
                    result = task.run() or TaskResult.SUCCESS
                except Exception as exc:
                    dev_log(f"自动浇水[{name}]异常", exc)
                    self.log(f"[{name}] 出错，继续下一处:{type(exc).__name__}: {exc}")
                    result = TaskResult.FAIL
                results[name] = result
                self.log(f"[{name}] 结束:{result}")
                if result == TaskResult.ABORT:
                    break
            if not self._stopped():
                self._back_to_world(ctx)
            return results
        finally:
            self._last_cycle_stop_reason = str(getattr(ctx, "stop_reason", "") or "")
            try:
                ctx.close()
            finally:
                with self._active_lock:
                    self._ctx = None

    def _shutdown_due(self) -> bool:
        return bool(
            self._shutdown_deadline is not None
            and time.monotonic() >= self._shutdown_deadline
        )

    def _perform_auto_close(self) -> None:
        '到达用户设置的总时长后关闭游戏，并通知 GUI 关闭软件。'
        if self._stopped():
            return
        self._state("自动关闭中…")
        self.log("已到自动关闭时间，正在停止任务并关闭游戏和软件")
        hwnd = int(self._find_game(0) or self._cycle_hwnd or 0)
        if hwnd:
            self._close_game(hwnd)
        if self._stopped():
            return
        self.auto_close_requested = True

    def run(self) -> None:
        '立即跑首轮，后续周期始终从每轮实际启动时间计算。'
        self.log(
            f"自动浇水开始:间隔 {self.interval_minutes} 分钟，"
            f"完成后{'关闭' if self.close_game_after else '保留'}游戏，"
            f"自动关闭={'关闭' if self.shutdown_hours == 0 else f'{self.shutdown_hours} 小时'}")
        if self.shutdown_hours > 0:
            self._shutdown_deadline = time.monotonic() + self._shutdown_seconds
        try:
            while not self._stopped():
                cycle_started = time.monotonic()
                cycle_deadline = cycle_started + self._interval_seconds
                stop_deadline = min(
                    deadline for deadline in (cycle_deadline, self._shutdown_deadline)
                    if deadline is not None
                )
                self._cycle_index += 1
                self._state(f"第 {self._cycle_index} 轮准备中…")
                self.log(f"自动浇水:第 {self._cycle_index} 轮开始")
                results = self.run_cycle(stop_deadline=stop_deadline)
                if self._stopped():
                    break
                self.log(f"自动浇水:第 {self._cycle_index} 轮结束，明细:{results}")
                if self._last_cycle_stop_reason == "game_closed":
                    self.log("检测到用户关闭游戏，本轮计为结束；不会立即重启或补做")
                elif self._last_cycle_stop_reason == "deadline":
                    self.log("本轮到达固定周期截止点，放弃未完成步骤并刷新到下一轮")
                if self._shutdown_due():
                    self._perform_auto_close()
                    break
                task_results = [results.get(name) for name, _factory in self._task_factories]
                cycle_completed = bool(task_results) and all(
                    result in (TaskResult.SUCCESS, TaskResult.SKIP)
                    for result in task_results)
                if self.close_game_after and self._cycle_hwnd and cycle_completed:
                    self._state("正在关闭游戏…")
                    self._close_game(self._cycle_hwnd)
                    if self._stopped():
                        break
                elif self.close_game_after and self._cycle_hwnd:
                    self.log("本轮浇水未全部完成，保留游戏窗口以便下轮重试")
                remaining = max(0.0, cycle_deadline - time.monotonic())
                if remaining > 0.0:
                    self._state(f"等待中 · 本轮启动后 {self.interval_minutes} 分钟继续")
                    self.log(f"下一轮仍按本轮启动时间计时，剩余 {remaining / 60.0:.1f} 分钟")
                if self._shutdown_deadline is not None:
                    remaining = min(
                        remaining,
                        max(0.0, self._shutdown_deadline - time.monotonic()),
                    )
                if remaining > 0.0 and self._stop_event.wait(remaining):
                    break
                if self._shutdown_due():
                    self._perform_auto_close()
                    break
        finally:
            self.stop()
            self._state("已停止")
            self.log("自动浇水已停止")
