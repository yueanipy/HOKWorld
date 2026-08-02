'为路线录制和播放提供Qt线程边界。'
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from runtime_guard import dev_log, input_owner, release_known_keys

from .model import RouteScript
from .player import RoutePlayer, RouteRunResult
from .route_recorder import RouteRecorder


class RoutePlaybackWorker(QThread):
    '在后台线程中执行路线，所有UI更新通过Signal返回。'

    sig_log = Signal(str)
    sig_state = Signal(str)
    sig_progress = Signal(int, int)
    sig_done = Signal(object)

    def __init__(
            self, route: RouteScript, *, loop_count: int = 1,
            coordinate_correction: bool | None = None) -> None:
        super().__init__()
        self._route = route
        self._loop_count = int(loop_count)
        self._coordinate_correction = coordinate_correction
        self._player: RoutePlayer | None = None
        self._stop_requested = threading.Event()
        self._paused = False

    def run(self) -> None:
        result = RouteRunResult(False, "initialization_failed", 0)
        try:
            self._player = RoutePlayer(
                self._route,
                loop_count=self._loop_count,
                coordinate_correction=self._coordinate_correction,
                log=self.sig_log.emit,
                on_state=self.sig_state.emit,
                on_progress=self.sig_progress.emit,
            )
            if self._paused:
                self._player.set_paused(True)
            if self._stop_requested.is_set():
                self._player.stop()
            with input_owner("自定义路线"):
                result = self._player.run()
        except Exception as exc:
            dev_log("[route] 路线Worker初始化或运行异常", exc)
            self.sig_log.emit(f"[错误] {type(exc).__name__}: {exc}")
            result = RouteRunResult(False, "worker_exception", 0)
        finally:
            release_known_keys(self.sig_log.emit)
            self._player = None
            self.sig_done.emit(result)

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        player = self._player
        if player is not None:
            player.set_paused(self._paused)

    def stop(self) -> None:
        self._stop_requested.set()
        player = self._player
        if player is not None:
            player.stop()


class RouteRecordWorker(QThread):
    '在后台线程中监听游戏前台输入并生成路线文件。'

    sig_log = Signal(str)
    sig_state = Signal(str)
    sig_done = Signal(object, str)

    def __init__(
            self, name: str, *, start_teleport: str = "",
            coordinate_correction: bool = False) -> None:
        super().__init__()
        self._name = name
        self._start_teleport = start_teleport
        self._coordinate_correction = bool(coordinate_correction)
        self._route_capture: RouteRecorder | None = None
        self._stop_requested = threading.Event()
        self._cancel_requested = False

    def run(self) -> None:
        path = None
        error = ""
        try:
            self._route_capture = RouteRecorder(
                self._name,
                start_teleport=self._start_teleport,
                coordinate_correction=self._coordinate_correction,
                log=self.sig_log.emit,
                on_state=self.sig_state.emit,
            )
            if self._stop_requested.is_set():
                self._route_capture.stop(cancel=self._cancel_requested)
            path = self._route_capture.record()
        except Exception as exc:
            dev_log("[route] 路线录制Worker异常", exc)
            error = f"{type(exc).__name__}: {exc}"
            self.sig_log.emit(f"[错误] {error}")
        finally:
            self._route_capture = None
            self.sig_done.emit(path, error)

    def stop(self, *, cancel: bool = False) -> None:
        self._cancel_requested = self._cancel_requested or bool(cancel)
        self._stop_requested.set()
        capture = self._route_capture
        if capture is not None:
            capture.stop(cancel=self._cancel_requested)
