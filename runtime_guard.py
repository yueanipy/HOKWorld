'开发版运行保护:日志、原子写入、安全键鼠动作、任务互斥。'
from __future__ import annotations

import ctypes
import heapq
import json
import os
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

import win32api
import win32con


_ULONG_PTR = (
    ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8
    else ctypes.c_ulong)


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouse_data", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("extra_info", _ULONG_PTR),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mouse", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ctypes.c_ulong), ("value", _InputUnion)]

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

    keys = (
        0x09, 0x1B, 0x20, 0x10, 0x11, 0x12,
        *range(0x30, 0x3A),
        *range(0x41, 0x5B),
    )
    for vk in keys:
        try:
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        except Exception as exc:
            try:
                log(f"release key failed vk={vk}: {exc}")
            except Exception:
                pass
    try:
        win32api.keybd_event(
            0,
            0x2A,
            getattr(win32con, "KEYEVENTF_SCANCODE", 0x0008)
            | win32con.KEYEVENTF_KEYUP,
            0,
        )
    except Exception as exc:
        try:
            log(f"release left shift scan code failed: {exc}")
        except Exception:
            pass
    for flag in (
        win32con.MOUSEEVENTF_LEFTUP,
        win32con.MOUSEEVENTF_RIGHTUP,
        win32con.MOUSEEVENTF_MIDDLEUP,
    ):
        try:
            win32api.mouse_event(flag, 0, 0, 0, 0)
        except Exception as exc:
            try:
                log(f"release mouse failed flag={flag}: {exc}")
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


def safe_press_scan_code(
    scan_code: int,
    stop_check=None,
    foreground_check=None,
    log=dev_log,
    hold_s: float = 0.05,
) -> bool:
    '使用硬件扫描码发送按键，供游戏不接受通用虚拟键时使用。'
    if not _allow(stop_check, foreground_check, log):
        return False
    scan_flag = getattr(win32con, "KEYEVENTF_SCANCODE", 0x0008)
    try:
        with _INPUT_ACTION_LOCK:
            if not _allow(stop_check, foreground_check, log):
                return False
            win32api.keybd_event(0, scan_code, scan_flag, 0)
            try:
                time.sleep(max(0.0, hold_s))
            finally:
                win32api.keybd_event(
                    0,
                    scan_code,
                    scan_flag | win32con.KEYEVENTF_KEYUP,
                    0,
                )
            return True
    except Exception as exc:
        dev_log(f"safe_press_scan_code failed scan={scan_code}", exc)
        try:
            log(f"[保护] 扫描码按键失败,已急停: {exc}")
        except Exception:
            pass
        release_known_keys()
        raise


class SafeKeyScheduler:
    '按下键鼠后立即返回，并在独立调度线程中按截止时间释放。'

    def __init__(
        self,
        stop_check=None,
        foreground_check=None,
        log=dev_log,
    ) -> None:
        self._stop_check = stop_check
        self._foreground_check = foreground_check
        self._log = log
        self._condition = threading.Condition()
        self._deadlines: list[tuple[float, int, tuple[str, int]]] = []
        self._held: dict[tuple[str, int], int] = {}
        self._sequence = 0
        self._closed = False
        self._thread: threading.Thread | None = None

    def press_key(self, vk: int, hold_s: float) -> bool:
        return self._press(("vk", int(vk)), hold_s)

    def press_mapped_key(self, vk: int, hold_s: float) -> bool:
        '把虚拟键映射为硬件扫描码，游戏拒绝普通虚拟键时使用。'
        map_mode = getattr(win32con, "MAPVK_VK_TO_VSC", 0)
        scan_code = int(win32api.MapVirtualKey(int(vk), map_mode)) & 0xFF
        if scan_code:
            return self.press_scan_code(scan_code, hold_s)
        return self.press_key(vk, hold_s)

    def press_scan_code(self, scan_code: int, hold_s: float) -> bool:
        return self._press(("scan", int(scan_code)), hold_s)

    def press_mouse(
        self,
        button: str,
        hold_s: float,
        *,
        cursor_position: tuple[int, int] | None = None,
    ) -> bool:
        '非阻塞按住鼠标键；仅菜单点击需要先定位光标。'
        codes = {"left": 1, "right": 2, "middle": 3}
        code = codes.get(str(button).lower())
        if code is None:
            return False
        return self._press(
            ("mouse", code), hold_s,
            cursor_position=cursor_position,
        )

    def _press(
        self,
        key: tuple[str, int],
        hold_s: float,
        *,
        cursor_position: tuple[int, int] | None = None,
    ) -> bool:
        if not _allow(
            self._stop_check,
            self._foreground_check,
            self._log,
        ):
            return False
        try:
            with self._condition:
                if self._closed:
                    return False
                if self._held.get(key, 0) == 0:
                    with _INPUT_ACTION_LOCK:
                        if not _allow(
                            self._stop_check,
                            self._foreground_check,
                            self._log,
                        ):
                            return False
                        if cursor_position is not None:
                            win32api.SetCursorPos((
                                int(cursor_position[0]),
                                int(cursor_position[1]),
                            ))
                        self._emit(key, key_up=False)
                self._held[key] = self._held.get(key, 0) + 1
                self._sequence += 1
                heapq.heappush(
                    self._deadlines,
                    (
                        time.monotonic() + max(0.01, float(hold_s)),
                        self._sequence,
                        key,
                    ),
                )
                self._ensure_thread_locked()
                self._condition.notify_all()
            return True
        except Exception as exc:
            dev_log(f"SafeKeyScheduler press failed key={key}", exc)
            try:
                self._log(f"[保护] 定时按键失败,已急停: {exc}")
            except Exception:
                pass
            self.release_all()
            release_known_keys()
            raise

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._release_loop,
            name="SafeKeyRelease",
            daemon=True,
        )
        self._thread.start()

    def _release_loop(self) -> None:
        while True:
            with self._condition:
                while not self._deadlines and not self._closed:
                    self._condition.wait()
                if self._closed and not self._deadlines:
                    return
                deadline, _sequence, key = self._deadlines[0]
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                heapq.heappop(self._deadlines)
                count = self._held.get(key, 0)
                if count > 1:
                    self._held[key] = count - 1
                    continue
                self._held.pop(key, None)
                try:
                    with _INPUT_ACTION_LOCK:
                        self._emit(key, key_up=True)
                except Exception as exc:
                    dev_log(f"SafeKeyScheduler release failed key={key}", exc)

    def release_all(self) -> None:
        with self._condition:
            held = tuple(self._held)
            self._held.clear()
            self._deadlines.clear()
            self._condition.notify_all()
            for key in held:
                try:
                    with _INPUT_ACTION_LOCK:
                        self._emit(key, key_up=True)
                except Exception as exc:
                    dev_log(f"SafeKeyScheduler release_all failed key={key}", exc)

    def close(self) -> None:
        self.release_all()
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    @staticmethod
    def _emit(key: tuple[str, int], *, key_up: bool) -> None:
        kind, code = key
        if kind == "mouse":
            flags = {
                1: (
                    win32con.MOUSEEVENTF_LEFTDOWN,
                    win32con.MOUSEEVENTF_LEFTUP,
                ),
                2: (
                    win32con.MOUSEEVENTF_RIGHTDOWN,
                    win32con.MOUSEEVENTF_RIGHTUP,
                ),
                3: (
                    win32con.MOUSEEVENTF_MIDDLEDOWN,
                    win32con.MOUSEEVENTF_MIDDLEUP,
                ),
            }
            down_flag, up_flag = flags[int(code)]
            win32api.mouse_event(
                up_flag if key_up else down_flag, 0, 0, 0, 0)
            return
        up_flag = win32con.KEYEVENTF_KEYUP if key_up else 0
        if kind == "scan":
            scan_flag = getattr(win32con, "KEYEVENTF_SCANCODE", 0x0008)
            win32api.keybd_event(0, code, scan_flag | up_flag, 0)
            return
        win32api.keybd_event(code, 0, up_flag, 0)


def safe_mouse_button(button: str, stop_check=None, foreground_check=None,
                      log=dev_log, hold_s: float = 0.04,
                      hwnd: int = 0, point=None) -> bool:
    '安全点击鼠标键；提供窗口和归一化坐标时先移动到对应位置。'
    if not _allow(stop_check, foreground_check, log):
        return False
    flags = {
        "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
        "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
        "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP),
    }
    if button not in flags:
        raise ValueError(f"不支持的鼠标键: {button}")
    down_flag, up_flag = flags[button]
    pressed = False
    try:
        cursor_position = None
        if hwnd and point is not None:
            from winenv import client_rect_on_screen

            x, y, width, height = client_rect_on_screen(hwnd)
            if width <= 0 or height <= 0:
                return False
            cursor_position = (
                int(x + float(point[0]) * width),
                int(y + float(point[1]) * height),
            )
        with _INPUT_ACTION_LOCK:
            if not _allow(stop_check, foreground_check, log):
                return False
            if cursor_position is not None:
                win32api.SetCursorPos(cursor_position)
            win32api.mouse_event(down_flag, 0, 0, 0, 0)
            pressed = True
        completed = True
        deadline = time.monotonic() + max(0.0, min(5.0, float(hold_s)))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            if not _allow(stop_check, foreground_check, log):
                completed = False
                break
            time.sleep(min(0.02, remaining))
        return completed
    except Exception as exc:
        dev_log(f"safe_mouse_button failed button={button}", exc)
        try:
            log(f"[保护] 鼠标按键失败,已急停: {exc}")
        except Exception:
            pass
        release_known_keys()
        raise
    finally:
        if pressed:
            try:
                with _INPUT_ACTION_LOCK:
                    win32api.mouse_event(up_flag, 0, 0, 0, 0)
            except Exception as exc:
                dev_log(f"safe_mouse_button release failed button={button}", exc)


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


def safe_scroll(
        notches: int, stop_check=None, foreground_check=None,
        log=dev_log) -> bool:
    '在当前 HUD 光标位置发送滚轮，不改变镜头或隐藏光标位置。'
    if notches == 0 or not _allow(stop_check, foreground_check, log):
        return False
    try:
        with _INPUT_ACTION_LOCK:
            if not _allow(stop_check, foreground_check, log):
                return False
            win32api.mouse_event(
                win32con.MOUSEEVENTF_WHEEL,
                0, 0, int(notches) * 120, 0,
            )
            return True
    except Exception as exc:
        dev_log(f"safe_scroll failed notches={notches}", exc)
        try:
            log(f"[保护] 滚轮失败,已急停: {exc}")
        except Exception:
            pass
        release_known_keys()
        raise


def safe_move_mouse_relative(
        dx: int, dy: int, *, duration_s: float = 0.0,
        stop_check=None, foreground_check=None, log=dev_log) -> bool:
    '受保护地发送相对鼠标移动，并在分步期间持续检查停止和前台。'
    if not _allow(stop_check, foreground_check, log):
        return False
    total_x, total_y = int(dx), int(dy)
    duration = max(0.0, min(5.0, float(duration_s)))
    steps = max(1, min(120, int(round(duration / 0.01))))
    moved_x = 0
    moved_y = 0
    try:
        with _INPUT_ACTION_LOCK:
            for index in range(1, steps + 1):
                if not _allow(stop_check, foreground_check, log):
                    return False
                target_x = int(round(total_x * index / steps))
                target_y = int(round(total_y * index / steps))
                step_x = target_x - moved_x
                step_y = target_y - moved_y
                if step_x or step_y:
                    _send_input_mouse_move(step_x, step_y)
                moved_x, moved_y = target_x, target_y
                if duration > 0.0 and index < steps:
                    time.sleep(duration / steps)
        return True
    except Exception as exc:
        dev_log(
            f"safe_move_mouse_relative failed dx={total_x} dy={total_y}", exc)
        try:
            log(f"[保护] 相对鼠标移动失败,已急停: {exc}")
        except Exception:
            pass
        release_known_keys()
        raise


def _send_input_mouse_move(dx: int, dy: int) -> None:
    '使用SendInput发送一帧相对鼠标增量。'
    event = _Input(
        type=0,
        mouse=_MouseInput(
            dx=int(dx), dy=int(dy), mouse_data=0,
            flags=win32con.MOUSEEVENTF_MOVE, time=0, extra_info=0,
        ),
    )
    sent = int(ctypes.windll.user32.SendInput(
        1, ctypes.byref(event), ctypes.sizeof(_Input)))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


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
        pressed = False
        with _INPUT_ACTION_LOCK:
            if not _allow(stop_check, foreground_check, log):
                return False
            win32api.SetCursorPos((sx, sy))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            pressed = True
        try:
            count = max(1, int(steps))
            delay = max(0.0, min(10.0, float(duration_s))) / count
            for i in range(1, count + 1):
                if not _allow(stop_check, foreground_check, log):
                    return False
                px = int(sx + (ex - sx) * i / count)
                py = int(sy + (ey - sy) * i / count)
                with _INPUT_ACTION_LOCK:
                    if not _allow(stop_check, foreground_check, log):
                        return False
                    win32api.SetCursorPos((px, py))
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if not _allow(stop_check, foreground_check, log):
                        return False
                    time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            return True
        finally:
            if pressed:
                with _INPUT_ACTION_LOCK:
                    win32api.mouse_event(
                        win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception as exc:
        dev_log(f"safe_drag_norm failed start={start} end={end}", exc)
        try:
            log(f"[保护] 拖动失败,已急停: {exc}")
        except Exception:
            pass
        release_known_keys()
        raise
