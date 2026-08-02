'主程序后台工作线程。'
from __future__ import annotations

import os
import queue
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from config import user_data_dir
from runtime_guard import dev_log, input_owner, registry, release_known_keys

_REALTIME_INIT_LOCK = threading.Lock()


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

    def __init__(self, auto_launch_game: bool) -> None:
        super().__init__()
        self._auto_launch_game = bool(auto_launch_game)
        self._input_tick_at_start: int | None = None
        self.runner = None
        self._paused = False
        self._stop_requested = False

    def set_initial_input_tick(self, tick: int | None) -> None:
        self._input_tick_at_start = tick

    def run(self) -> None:
        try:
            import importlib
            import sys as _sys

            import daily.base
            import daily.config
            import daily.context
            import daily.navigation
            import daily.orchestrator
            import daily.ranch_recognizer
            import daily.ranch_route
            import daily.recognizer
            import daily.regions
            import daily.startup
            import daily.tasks
            import daily.tasks._field

            task_mods = [m for n, m in sorted(_sys.modules.items())
                         if n.startswith("daily.tasks.") and n != "daily.tasks._field"]
            for m in (daily.regions, daily.recognizer, daily.ranch_recognizer,
                      daily.ranch_route,
                      daily.context, daily.navigation,
                      daily.base, daily.config, daily.tasks._field,
                      *task_mods, daily.tasks, daily.orchestrator, daily.startup):
                importlib.reload(m)
            from daily.startup import DailyStartupRunner
            self.runner = DailyStartupRunner(
                auto_launch_game=self._auto_launch_game,
                log=self.sig_log.emit,
                on_progress=lambda d, t: self.sig_progress.emit(d, t),
                input_tick_at_start=self._input_tick_at_start,
            )
            self.runner.set_paused(self._paused)
            if self._stop_requested:
                self.runner.stop()
                return
            with input_owner("每日任务"):
                self.runner.run()
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
        if self.runner:
            self.runner.stop()

    def set_paused(self, on: bool) -> None:
        self._paused = bool(on)
        if self.runner:
            self.runner.set_paused(self._paused)


class CombatWorker(QThread):
    '自动战斗线程。'

    sig_log = Signal(str)
    sig_done = Signal(bool)

    def __init__(self, ultimate_mode: str, profile_file: str) -> None:
        super().__init__()
        self._ultimate_mode = str(ultimate_mode)
        self._profile_file = str(profile_file)
        self._stop_requested = False
        self.bot = None
        self._log_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._log_sentinel = object()
        self._log_thread: threading.Thread | None = None

    def run(self) -> None:
        ok = False
        self._start_log_writer()
        try:
            import importlib
            import combat.audio
            import combat.engine
            import combat.profile
            import combat.recognizer
            import combat.vision

            for module in (
                combat.profile,
                combat.recognizer,
                combat.audio,
                combat.vision,
                combat.engine,
            ):
                importlib.reload(module)
            from combat.engine import CombatBot
            from combat.profile import CombatProfile

            profile = CombatProfile.load(Path(self._profile_file))
            self.bot = CombatBot(
                profile=profile,
                ultimate_mode=self._ultimate_mode,
                log=self._emit_log,
            )
            if self._stop_requested:
                self.bot.stop()
                return
            with input_owner("自动战斗"):
                ok = bool(self.bot.run())
        except Exception as exc:
            dev_log("自动战斗线程异常,执行保守急停", exc)
            registry.stop_all("自动战斗线程异常")
            release_known_keys(self._emit_log)
            self._emit_log(f"[错误] {type(exc).__name__}: {exc}")
            self._emit_log("自动战斗已停止,详情见 data/logs/hokworld_dev.log")
        finally:
            self._stop_log_writer()
            self.sig_done.emit(ok)

    def _emit_log(self, message: str) -> None:
        text = str(message)
        thread = self._log_thread
        if thread is not None and thread.is_alive():
            self._log_queue.put(text)
        else:
            dev_log(f"[combat] {text}")
        self.sig_log.emit(text)

    def _start_log_writer(self) -> None:
        if self._log_thread is not None and self._log_thread.is_alive():
            return
        self._log_thread = threading.Thread(
            target=self._drain_log_queue,
            name="combat-log",
            daemon=True,
        )
        self._log_thread.start()

    def _stop_log_writer(self) -> None:
        thread = self._log_thread
        if thread is None:
            return
        self._log_queue.put(self._log_sentinel)
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._log_thread = None

    def _drain_log_queue(self) -> None:
        while True:
            message = self._log_queue.get()
            if message is self._log_sentinel:
                return
            try:
                dev_log(f"[combat] {message}")
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_requested = True
        if self.bot:
            self.bot.stop()


class AutoWaterWorker(QThread):
    '独立自动浇水线程。'
    sig_log = Signal(str)
    sig_state = Signal(str)
    sig_done = Signal()

    def __init__(self, interval_minutes: int, close_game_after: bool,
                 include_ranch: bool,
                 shutdown_hours: int) -> None:
        super().__init__()
        self._interval_minutes = int(interval_minutes)
        self._close_game_after = bool(close_game_after)
        self._include_ranch = bool(include_ranch)
        self._shutdown_hours = int(shutdown_hours)
        self._input_tick_at_start: int | None = None
        self.scheduler = None
        self._stop_requested = False
        self.auto_close_requested = False

    def set_initial_input_tick(self, tick: int | None) -> None:
        self._input_tick_at_start = tick

    def run(self) -> None:
        try:
            import importlib
            import sys as _sys
            import launcher
            import daily.base
            import daily.config
            import daily.context
            import daily.navigation
            import daily.ranch_recognizer
            import daily.ranch_route
            import daily.recognizer
            import daily.regions
            import daily.tasks
            import daily.tasks._field
            import independent.auto_watering

            task_mods = [m for n, m in sorted(_sys.modules.items())
                         if n.startswith("daily.tasks.") and n != "daily.tasks._field"]
            for module in (daily.regions, daily.recognizer,
                           daily.ranch_recognizer, daily.ranch_route,
                           daily.context,
                           daily.navigation, daily.base, daily.config,
                           daily.tasks._field, *task_mods, daily.tasks, launcher,
                           independent.auto_watering):
                importlib.reload(module)
            from independent.auto_watering import AutoWaterScheduler

            self.scheduler = AutoWaterScheduler(
                interval_minutes=self._interval_minutes,
                close_game_after=self._close_game_after,
                include_ranch=self._include_ranch,
                shutdown_hours=self._shutdown_hours,
                log=self.sig_log.emit,
                on_state=self.sig_state.emit,
                input_tick_at_start=self._input_tick_at_start,
            )
            if self._stop_requested:
                self.scheduler.stop()
                return
            with input_owner("自动浇水"):
                self.scheduler.run()
            self.auto_close_requested = bool(self.scheduler.auto_close_requested)
        except Exception as exc:
            dev_log("自动浇水线程异常,执行保守急停", exc)
            registry.stop_all("自动浇水线程异常")
            release_known_keys(self.sig_log.emit)
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
            self.sig_log.emit("自动浇水已停止,详情见 data/logs/hokworld_dev.log")
        finally:
            self.sig_done.emit()

    def stop(self) -> None:
        self._stop_requested = True
        if self.scheduler:
            self.scheduler.stop()





