'开发版运行保护:日志、原子写入、安全键鼠动作、任务互斥。'
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

import win32api
import win32con

HERE = Path(__file__).resolve().parent
try:
    from paths import logs_dir as _logs_dir
    LOG_DIR = _logs_dir()
except Exception:
    LOG_DIR = HERE / "data" / "logs"
DEV_LOG = LOG_DIR / "hokworld_dev.log"


def dev_log(msg: str, exc: BaseException | None = None) -> None:
    '写开发日志。'
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
    '进程内任务互斥。'

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

    def suspend(self, name: str) -> bool:
        '临时释放活动任务槽但保留 stopper，供低优先级实时检测让位。'
        with self._lock:
            if self._active != name or name not in self._stoppers:
                return False
            self._active = None
            dev_log(f"task suspend: {name}")
            return True

    def resume(self, name: str) -> bool:
        '恢复曾 suspend 的任务；期间若已有别的任务则保持暂停。'
        with self._lock:
            if self._active is not None or name not in self._stoppers:
                return False
            self._active = name
            dev_log(f"task resume: {name}")
            return True

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

_INPUT_LOCAL = threading.local()
_INPUT_ACTION_LOCK = threading.RLock()


@contextmanager
def input_owner(name: str):
    '把当前 Worker 线程绑定到任务所有者，安全输入只允许活动任务发送。'
    previous = getattr(_INPUT_LOCAL, "owner", None)
    _INPUT_LOCAL.owner = name
    try:
        yield
    finally:
        _INPUT_LOCAL.owner = previous


def input_allowed() -> bool:
    '未绑定的维护/UI调用保持兼容；已绑定 Worker 必须持有当前任务槽。'
    owner = getattr(_INPUT_LOCAL, "owner", None)
    return owner is None or registry.active() == owner


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
        if not input_allowed():
            return False
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
        with _INPUT_ACTION_LOCK:
            if not _allow(stop_check, foreground_check, log):
                return False
            win32api.keybd_event(vk, 0, 0, 0)
            try:
                time.sleep(max(0.0, hold_s))
            finally:
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


@contextmanager
def safe_hold_key(vk: int, stop_check=None, foreground_check=None, log=dev_log):
    '受保护地持续按住一个键；调用方可在上下文内截图闭环，退出时无条件抬键。'
    if not _allow(stop_check, foreground_check, log):
        yield False
        return
    pressed = False
    try:
        with _INPUT_ACTION_LOCK:
            if not _allow(stop_check, foreground_check, log):
                yield False
                return
            win32api.keybd_event(vk, 0, 0, 0)
            pressed = True
            yield True
    except Exception as exc:
        dev_log(f"safe_hold_key failed vk={vk}", exc)
        try:
            log(f"[保护] 持续按键失败,已急停: {exc}")
        except Exception:
            pass
        release_known_keys()
        raise
    finally:
        if pressed:
            try:
                win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
            except Exception as exc:
                dev_log(f"safe_hold_key release failed vk={vk}", exc)


def safe_scroll_norm(hwnd, pt, notches: int, stop_check=None, foreground_check=None,
                     log=dev_log) -> bool:
    '在归一化位置发送鼠标滚轮；正数向上、负数向下。'
    if notches == 0 or not _allow(stop_check, foreground_check, log):
        return False
    try:
        from winenv import client_rect_on_screen

        x, y, w, h = client_rect_on_screen(hwnd)
        if w <= 0 or h <= 0:
            return False
        sx, sy = int(x + pt[0] * w), int(y + pt[1] * h)
        with _INPUT_ACTION_LOCK:
            if not _allow(stop_check, foreground_check, log):
                return False
            win32api.SetCursorPos((sx, sy))
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(notches) * 120, 0)
            return True
    except Exception as exc:
        dev_log(f"safe_scroll_norm failed pt={pt} notches={notches}", exc)
        try:
            log(f"[保护] 滚轮失败,已急停: {exc}")
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
        with _INPUT_ACTION_LOCK:
            if not _allow(stop_check, foreground_check, log):
                return False
            win32api.SetCursorPos((sx, sy))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            try:
                time.sleep(max(0.0, down_s))
            finally:
                
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


def safe_drag_norm(hwnd, start, end, stop_check=None, foreground_check=None,
                   log=dev_log, duration_s: float = 0.5, steps: int = 16) -> bool:
    '前台受保护的归一化左键拖动;任何异常/中止都保证释放左键。'
    if not _allow(stop_check, foreground_check, log):
        return False
    try:
        from winenv import client_rect_on_screen

        x, y, w, h = client_rect_on_screen(hwnd)
        if w <= 0 or h <= 0:
            return False
        sx, sy = int(x + start[0] * w), int(y + start[1] * h)
        ex, ey = int(x + end[0] * w), int(y + end[1] * h)
        with _INPUT_ACTION_LOCK:
            if not _allow(stop_check, foreground_check, log):
                return False
            win32api.SetCursorPos((sx, sy))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            try:
                count = max(1, int(steps))
                delay = max(0.0, duration_s) / count
                for i in range(1, count + 1):
                    if not _allow(stop_check, foreground_check, log):
                        return False
                    px = int(sx + (ex - sx) * i / count)
                    py = int(sy + (ey - sy) * i / count)
                    win32api.SetCursorPos((px, py))
                    time.sleep(delay)
                return True
            finally:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception as exc:
        dev_log(f"safe_drag_norm failed start={start} end={end}", exc)
        try:
            log(f"[保护] 拖动失败,已急停: {exc}")
        except Exception:
            pass
        release_known_keys()
        raise
