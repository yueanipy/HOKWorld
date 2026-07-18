'每日任务一条龙 —— 运行上下文 DailyContext(所有任务共用的"手和眼")。'
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from winenv import find_game_hwnd, is_foreground  
from capture import GameCapture
from runtime_guard import (dev_log, input_allowed, release_known_keys, safe_click_norm,
                           safe_drag_norm, safe_hold_key, safe_press_key, safe_scroll_norm)


VK = {
    "esc": 0x1B, "f": 0x46, "g": 0x47, "e": 0x45, "q": 0x51, "m": 0x4D,
    "w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44, "c": 0x43,
    "f7": 0x76,
    "f11": 0x7A,  
                  
    "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
}


class DailyContext:
    def __init__(self, log=print) -> None:
        self.log = log
        self.stop_flag = False
        self.paused = False
        self._pause_lock = threading.Lock()
        self._paused_total = 0.0
        self._pause_started: float | None = None
        self._hwnd: int | None = None
        self._cap: GameCapture | None = None
        self._hwnd_lock = threading.RLock()

    
    def start(self) -> bool:
        hwnd = find_game_hwnd()
        with self._hwnd_lock:
            self._hwnd = hwnd
        if not hwnd:
            self.log("未找到游戏窗口『王者荣耀世界』,请先进入游戏")
            return False
        try:
            
            
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
            self._timer_res = True
        except Exception:
            self._timer_res = False
        self._cap = GameCapture(hwnd)
        self._cap.start()
        dev_log(f"[daily] context 初始游戏窗口 hwnd={hwnd} foreground={is_foreground(hwnd)}")
        return True

    def close(self) -> None:
        if getattr(self, "_timer_res", False):
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)   
            except Exception:
                pass
            self._timer_res = False
        if self._cap:
            self._cap.stop()
            self._cap = None
        release_known_keys(self.log)

    def stop(self) -> None:
        self.stop_flag = True
        release_known_keys(self.log)

    def set_paused(self, on: bool) -> None:
        on = bool(on)
        with self._pause_lock:
            if self.paused == on:
                return
            now = time.monotonic()
            if on:
                self._pause_started = now
            elif self._pause_started is not None:
                self._paused_total += now - self._pause_started
                self._pause_started = None
            self.paused = on
        if on:
            release_known_keys(self.log)

    def _clock(self) -> float:
        '排除暂停时长的单调逻辑时钟。'
        with self._pause_lock:
            now = time.monotonic()
            current = now - self._pause_started if self._pause_started is not None else 0.0
            return now - self._paused_total - current

    def logical_time(self) -> float:
        '任务逻辑时钟；暂停期间冻结，供需要视觉闭环的自定义状态机计时。'
        return self._clock()

    
    def should_stop(self) -> bool:
        '停止条件:用户停 / 游戏窗口没了。'
        if self.stop_flag:
            return True
        if not find_game_hwnd():
            self.log("游戏窗口消失 → 停止一条龙")
            self.stop_flag = True
            return True
        return False

    def _stopped(self) -> bool:
        return bool(self.stop_flag or self.paused)

    def foreground(self) -> bool:
        '判断游戏是否在前台，并修正最小化交接期间缓存的旧句柄。'
        with self._hwnd_lock:
            current = self._hwnd
        if current and is_foreground(current):
            return True

        candidate = find_game_hwnd(prefer_foreground=True)
        if not candidate or not is_foreground(candidate):
            return False
        if candidate != current:
            with self._hwnd_lock:
                old = self._hwnd
                self._hwnd = candidate
                if self._cap is not None:
                    self._cap.rebind(candidate)
            dev_log(f"[daily] 前台游戏窗口重绑定 {old} -> {candidate}")
        return True

    def action_ready(self) -> bool:
        '当前是否仍可安全输入；长按闭环必须持续复查，不能只在按下前检查一次。'
        return bool(not self._stopped() and input_allowed() and self.foreground())

    def wait_foreground(self, timeout: float = 30.0) -> bool:
        '等待游戏真正取得前台。'
        end = self._clock() + max(0.0, timeout)
        warned = False
        while self._clock() < end:
            if self.should_stop():
                return False
            if self.paused:
                time.sleep(0.10)
                continue
            if self.foreground():
                if warned:
                    self.log("游戏已回到前台，继续每日任务")
                return True
            if not warned:
                self.log("等待游戏回到前台；脚本不会重复抢前台")
                dev_log("[daily] 等待游戏前台，首个输入尚未发送")
                warned = True
            time.sleep(0.10)
        self.log(f"等待游戏前台超时({timeout:.0f}秒)，本任务停止")
        dev_log(f"[daily] 等待游戏前台超时 {timeout:.0f}s")
        return False

    @property
    def hwnd(self) -> int | None:
        with self._hwnd_lock:
            return self._hwnd

    def _action_hwnd(self) -> int | None:
        '先修正前台句柄，再给坐标类输入返回同一个有效 hwnd。'
        if not self.foreground():
            return None
        return self.hwnd

    
    def grab(self):
        '抓一帧整客户区 BGR(4K);识别函数内部会归一化到 1920。'
        while self.paused and not self.should_stop():
            time.sleep(0.2)
        
        self.foreground()
        with self._hwnd_lock:
            cap = self._cap
        if cap is None:
            return None
        return cap.grab()

    def grab_nowait(self):
        '暂停时立即返回 None，不阻塞在长按键上下文内，便于先抬键再等待恢复。'
        if self.paused or self.should_stop() or self._cap is None:
            return None
        self.foreground()
        with self._hwnd_lock:
            cap = self._cap
        return None if cap is None else cap.grab()

    
    def click(self, pt) -> bool:
        '点归一化坐标(仅前台+未停止时;不走弧线直接点)。'
        if pt is None:
            return False
        hwnd = self._action_hwnd()
        if not hwnd:
            return False
        return safe_click_norm(hwnd, pt, self._stopped, self.foreground, self.log, 0.02)

    def drag(self, start, end, duration_s: float = 0.5) -> bool:
        '按住左键从 start 拖到 end(好友列表翻页等 UI 拖动)。'
        hwnd = self._action_hwnd()
        if not hwnd:
            return False
        return safe_drag_norm(hwnd, start, end, self._stopped, self.foreground,
                              self.log, duration_s)

    def scroll(self, notches: int, pt=(0.50, 0.50)) -> bool:
        '在指定归一化位置滚轮；正数向上放大、负数向下缩小。'
        hwnd = self._action_hwnd()
        if not hwnd:
            return False
        return safe_scroll_norm(hwnd, pt, int(notches), self._stopped,
                                self.foreground, self.log)

    def press(self, key: str, hold_s: float = 0.05) -> bool:
        '按键(key 见 VK;holds 可长按,用于走路时按住 WASD)。'
        vk = VK.get(key.lower())
        if vk is None:
            self.log(f"未知按键 {key!r}")
            return False
        return safe_press_key(vk, self._stopped, self.foreground, self.log, hold_s)

    @contextmanager
    def hold(self, key: str):
        '持续按住方向键，供“移动中持续截图直到视觉条件满足”的闭环使用。'
        vk = VK.get(key.lower())
        if vk is None:
            self.log(f"未知按键 {key!r}")
            yield False
            return
        with safe_hold_key(vk, self._stopped, self.foreground, self.log) as held:
            yield held

    def walk(self, key: str = "w", seconds: float = 1.2) -> bool:
        '朝某方向走(按住 WASD 一段时间)。'
        self.log(f"走位:按住 {key.upper()} {seconds:.1f}s")
        return self.press(key, hold_s=max(0.0, seconds))

    def tap(self, key: str, seconds: float = 0.18) -> None:
        '轻点方向键一小步(带视觉确认的步进走位用;比盲走一大段可控)。'
        self.press(key, hold_s=max(0.05, seconds))

    def center_camera(self) -> bool:
        '镜头回正:屏幕中央点一次中键(同类脚本 centercamera 同款。'
        if not (input_allowed() and self.foreground() and not self._stopped()):
            return False
        import win32api
        import win32con
        try:
            from winenv import client_rect_on_screen
            x, y, w, h = client_rect_on_screen(self._hwnd)
            if w <= 0 or h <= 0:
                return False
            win32api.SetCursorPos((int(x + w * 0.5), int(y + h * 0.5)))
            win32api.mouse_event(win32con.MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0)
            try:
                time.sleep(0.15)
            finally:
                win32api.mouse_event(win32con.MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0)  
            self.sleep(1.6)                   
                                              
            return True
        except Exception as exc:
            dev_log("center_camera 失败", exc)
            return False

    def turn_direction(self, key: str) -> bool:
        '离散转向(同类脚本 turndirection 同款):轻点方向键让角色转身 → 中键让镜头甩到。'
        if key not in ("a", "s", "d", "w"):
            return False
        self.press(key, hold_s=0.06)
        self.sleep(0.15)
        return self.center_camera()

    def drag_camera(self, dx_px: int, steps: int = 12, dy_px: int = 0) -> bool:
        '转视角:注入鼠标相对移动(负 dx=左转,负 dy=向上看),分步小幅更像人手。'
        if not (input_allowed() and self.foreground() and not self._stopped()):
            dev_log(f"drag_camera 跳过:前台={self.foreground()} stop={self.stop_flag} paused={self.paused}"
                    "(传送加载/失焦时会走到这——20260712 实测偶发'传送后不转视角'的可见化)")
            return False
        import win32api
        import win32con
        try:
            step_x = int(dx_px / max(1, steps))
            step_y = int(dy_px / max(1, steps))
            moved_x = 0
            moved_y = 0
            for i in range(steps):
                if self._stopped() or not input_allowed() or not self.foreground():
                    return False
                mx = dx_px - moved_x if i == steps - 1 else step_x
                my = dy_px - moved_y if i == steps - 1 else step_y
                win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, mx, my, 0, 0)
                moved_x += mx
                moved_y += my
                time.sleep(0.02)
            return True
        except Exception as exc:
            dev_log("drag_camera 失败", exc)
            return False

    
    def sleep(self, seconds: float) -> None:
        '可被停止打断、暂停期间不消耗剩余时长的 sleep。'
        deadline = self._clock() + max(0.0, float(seconds))
        while True:
            if self.should_stop():
                return
            if self.paused:
                time.sleep(0.1)
                continue
            remaining = deadline - self._clock()
            if remaining <= 0.0:
                return
            time.sleep(min(0.1, max(0.0, remaining)))

    def wait_until(self, predicate, timeout: float = 8.0, interval: float = 0.4, desc: str = ""):
        '轮询 predicate(frame) 直到真值或超时。'
        end = self._clock() + timeout
        last = None
        while self._clock() < end:
            if self.should_stop():
                return None
            f = self.grab()
            if f is not None:
                try:
                    last = predicate(f)
                except Exception as exc:
                    dev_log(f"wait_until 判定异常 [{desc}]", exc)
                    last = None
                if last:
                    return last
            self.sleep(interval)
        if desc:
            self.log(f"等待超时:{desc}")
        return last
