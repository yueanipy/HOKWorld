'游戏进程退出监控：只在正式游戏被观察到后才允许触发脚本退出。'
from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from pathlib import Path

import win32process

from winenv import find_game_hwnd
from runtime_guard import dev_log

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


def _pid_from_hwnd(hwnd: int) -> int:
    return int(win32process.GetWindowThreadProcessId(int(hwnd))[1])


def _open_process(pid: int) -> int:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    return int(kernel32.OpenProcess(SYNCHRONIZE, 0, int(pid)) or 0)


def _process_image_path(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid)
    )
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        ok = kernel32.QueryFullProcessImageNameW(
            ctypes.c_void_p(handle), 0, buffer, ctypes.byref(size)
        )
        return buffer.value if ok else ""
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _is_formal_game_process(pid: int) -> bool:
    '排除与正式游戏同标题的启动器进程。'
    image = _process_image_path(pid)
    if not image:
        return False
    try:
        from config import cfg
        launcher_path = str(cfg.get("game_path") or "")
    except Exception:
        launcher_path = ""
    normalized = os.path.normcase(os.path.abspath(image))
    if launcher_path:
        configured = os.path.normcase(os.path.abspath(launcher_path))
        if normalized == configured:
            return False
    
    
    return Path(image).name.casefold() != "王者荣耀世界.exe".casefold()


def _process_exited(handle: int) -> bool:
    result = int(ctypes.windll.kernel32.WaitForSingleObject(
        ctypes.c_void_p(int(handle)), 0
    ))
    return result == WAIT_OBJECT_0


def _close_process(handle: int) -> None:
    if handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(int(handle)))


class GameExitMonitor:
    '记录首次正式游戏进程，并在该进程真正退出时产生一次事件。'

    def __init__(
        self,
        enabled: bool = False,
        log=print,
        find_game: Callable[[], int | None] | None = None,
        pid_from_hwnd: Callable[[int], int] | None = None,
        accept_process: Callable[[int], bool] | None = None,
        open_process: Callable[[int], int] | None = None,
        process_exited: Callable[[int], bool] | None = None,
        close_process: Callable[[int], None] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.log = log
        self._find_game = find_game or (
            lambda: find_game_hwnd(prefer_foreground=False)
        )
        self._pid_from_hwnd = pid_from_hwnd or _pid_from_hwnd
        self._accept_process = accept_process or _is_formal_game_process
        self._open_process = open_process or _open_process
        self._process_exited = process_exited or _process_exited
        self._close_process = close_process or _close_process
        self._pid = 0
        self._handle = 0
        self._exit_emitted = False
        self._deferred_exit_pid = 0

    @property
    def armed(self) -> bool:
        return bool(self._pid and self._handle)

    @property
    def pid(self) -> int:
        return self._pid

    def set_enabled(self, enabled: bool) -> None:
        '切换监控；关闭开关时立即释放进程句柄。'
        enabled = bool(enabled)
        if self.enabled == enabled:
            return
        self.enabled = enabled
        self.reset()

    def reset(self) -> None:
        '解除当前进程监控，等待下一次重新观察正式游戏。'
        handle = self._handle
        self._handle = 0
        self._pid = 0
        self._exit_emitted = False
        self._deferred_exit_pid = 0
        if handle:
            try:
                self._close_process(handle)
            except Exception as exc:
                dev_log("释放游戏进程监控句柄失败", exc)

    def close(self) -> None:
        self.reset()

    def poll(self, suppressed: bool = False) -> bool:
        '轮询一次；自动浇水期间保留退出事实，但延后到豁免结束再触发。'
        if not self.enabled:
            self.reset()
            return False
        if self._exit_emitted:
            return False
        if suppressed:
            return self._poll_suppressed()
        if self._deferred_exit_pid:
            ended_pid = self._deferred_exit_pid
            if self._try_arm():
                self._deferred_exit_pid = 0
                self.log(f"豁免期间游戏已重新启动 pid={self._pid}，继续监控")
                return False
            self._deferred_exit_pid = 0
            self._exit_emitted = True
            self.log(f"检测到游戏进程已退出 pid={ended_pid}（豁免结束后确认）")
            return True
        if not self.armed:
            self._try_arm()
            return False
        try:
            exited = bool(self._process_exited(self._handle))
        except Exception as exc:
            dev_log("检查游戏进程退出状态失败", exc)
            self.reset()
            return False
        if not exited:
            return False

        ended_pid = self._pid
        self.reset()
        if self._try_arm() and self._pid != ended_pid:
            self.log(f"游戏进程已切换 {ended_pid} → {self._pid}，继续监控")
            return False
        self._exit_emitted = True
        self.log(f"检测到游戏进程已退出 pid={ended_pid}")
        return True

    def _poll_suppressed(self) -> bool:
        '豁免期间继续观察进程，避免恢复实时检测后丢失退出事件。'
        if self._deferred_exit_pid:
            if self._try_arm():
                self._deferred_exit_pid = 0
                self.log(f"豁免期间游戏已重新启动 pid={self._pid}，继续监控")
            return False
        if not self.armed:
            self._try_arm()
            return False
        try:
            exited = bool(self._process_exited(self._handle))
        except Exception as exc:
            dev_log("豁免期间检查游戏进程退出状态失败", exc)
            return False
        if not exited:
            return False

        ended_pid = self._pid
        handle = self._handle
        self._handle = 0
        self._pid = 0
        try:
            self._close_process(handle)
        except Exception as exc:
            dev_log("释放已退出游戏进程监控句柄失败", exc)
        self._deferred_exit_pid = ended_pid
        self.log(f"自动浇水期间检测到游戏退出 pid={ended_pid}，暂不关闭脚本")
        return False

    def _try_arm(self) -> bool:
        try:
            hwnd = int(self._find_game() or 0)
            if not hwnd:
                return False
            pid = int(self._pid_from_hwnd(hwnd) or 0)
            if not pid:
                return False
            if not self._accept_process(pid):
                return False
            handle = int(self._open_process(pid) or 0)
            if not handle:
                dev_log(f"无法打开游戏进程监控句柄 pid={pid}")
                return False
            self._pid = pid
            self._handle = handle
            self._exit_emitted = False
            self.log(f"已监控游戏进程 pid={pid}")
            return True
        except Exception as exc:
            dev_log("建立游戏进程退出监控失败", exc)
            self.reset()
            return False
