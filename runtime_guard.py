"""开发版运行保护:日志、原子写入、安全键鼠动作、任务互斥。"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

import win32api
import win32con

HERE = Path(__file__).resolve().parent
try:                                      # 发布版:日志写用户可写目录(冻结后包目录只读)
    from paths import logs_dir as _logs_dir
    LOG_DIR = _logs_dir()
except Exception:                         # 开发版:退回源码下 data/logs
    LOG_DIR = HERE / "data" / "logs"
DEV_LOG = LOG_DIR / "hokworld_dev.log"


def dev_log(msg: str, exc: BaseException | None = None) -> None:
    """写开发日志。日志失败不应影响主流程。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with DEV_LOG.open("a", encoding="utf-8") as fp:
            fp.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
            if exc is not None:
                fp.write("".join(traceback.format_exception(exc)))
            fp.flush()
    except Exception:
        pass


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def atomic_write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class TaskRegistry:
    """进程内任务互斥。防止多个真实键鼠任务同时抢鼠标。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: str | None = None
        self._stoppers: dict[str, object] = {}

    def start(self, name: str, stopper=None) -> tuple[bool, str]:
        with self._lock:
            if self._active and self._active != name:
                return False, f"已有任务「{self._active}」运行中,请先停止后再启动「{name}」"
            self._active = name
            if stopper:
                self._stoppers[name] = stopper
            dev_log(f"task start: {name}")
            return True, ""

    def set_stopper(self, name: str, stopper) -> None:
        with self._lock:
            self._stoppers[name] = stopper

    def finish(self, name: str) -> None:
        with self._lock:
            self._stoppers.pop(name, None)
            if self._active == name:
                dev_log(f"task finish: {name}")
                self._active = None

    def active(self) -> str | None:
        with self._lock:
            return self._active

    def stop_all(self, reason: str = "stop_all") -> None:
        with self._lock:
            items = list(self._stoppers.items())
        dev_log(f"task stop_all: {reason}; active={self.active()}")
        for name, stopper in items:
            try:
                stopper()
            except Exception as exc:
                dev_log(f"task stopper failed: {name}", exc)
        release_known_keys(dev_log)


registry = TaskRegistry()


def release_known_keys(log=dev_log) -> None:
    for vk in (0x41, 0x44, 0x57, 0x53, 0x46, 0x1B):
        try:
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        except Exception as exc:
            try:
                log(f"release key failed vk={vk}: {exc}")
            except Exception:
                pass


def _allow(stop_check, foreground_check, log) -> bool:
    try:
        if stop_check and stop_check():
            return False
        if foreground_check and not foreground_check():
            return False
        return True
    except Exception as exc:
        dev_log("action guard check failed", exc)
        try:
            log(f"[保护] 动作检查失败,已跳过: {exc}")
        except Exception:
            pass
        return False


def safe_press_key(vk: int, stop_check=None, foreground_check=None, log=dev_log, hold_s: float = 0.05) -> bool:
    if not _allow(stop_check, foreground_check, log):
        return False
    try:
        win32api.keybd_event(vk, 0, 0, 0)
        time.sleep(max(0.0, hold_s))
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        return True
    except Exception as exc:
        dev_log(f"safe_press_key failed vk={vk}", exc)
        try:
            log(f"[保护] 按键失败,已急停: {exc}")
        except Exception:
            pass
        release_known_keys()
        raise


def safe_click_norm(hwnd, pt, stop_check=None, foreground_check=None, log=dev_log, down_s: float = 0.02) -> bool:
    if not _allow(stop_check, foreground_check, log):
        return False
    try:
        from winenv import client_rect_on_screen

        x, y, w, h = client_rect_on_screen(hwnd)
        if w <= 0 or h <= 0:
            return False
        sx, sy = int(x + pt[0] * w), int(y + pt[1] * h)
        if not _allow(stop_check, foreground_check, log):
            return False
        win32api.SetCursorPos((sx, sy))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(max(0.0, down_s))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True
    except Exception as exc:
        dev_log(f"safe_click_norm failed pt={pt}", exc)
        try:
            log(f"[保护] 点击失败,已急停: {exc}")
        except Exception:
            pass
        release_known_keys()
        raise
