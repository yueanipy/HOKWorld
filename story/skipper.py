"""HOKWord 实时剧情跳过引擎(状态 → 动作)。"""
from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

import mss
import numpy as np
import win32api
import win32con

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from winenv import client_rect_on_screen, find_game_hwnd, is_foreground  # noqa: E402
from story.recognizer import StoryRecognizer  # noqa: E402
from config import cfg  # noqa: E402
import applog  # noqa: E402


class StorySkipper:
    OCR_INTERVAL = 0.45            # 限频识别
    ADVANCE_INTERVAL = (0.6, 1.0)  # 对话/继续/黑屏 推进点击间隔
    CONFIRM_GAP = 0.8             # 两次点确认框最小间隔
    ESC_PENDING_S = 1.3          # 按 ESC 后等确认框的最长时间
    POST_SKIP_BLOCK = 2.0        # 跳过后这么久内不按 ESC
    ABORT_BLOCK = 2.5            # 放弃后这么久内不按 ESC
    GAMEPLAY_STICK = 1.2         # 见到游戏态后这么久内都当游戏态(跨过边界抖动)
    MOVE_TIME = (0.3, 0.8)       # 光标移动用时
    NUDGE_DIST = (28, 56)        # 沉浸式唤条:每次随机微动的距离(像素,仅开关开启时)
    NUDGE_TIME = (0.12, 0.28)    # 微动的平滑用时(分多步移动,不瞬移、不闪)
    NUDGE_INTERVAL = (0.8, 1.2)  # 两次微动的随机间隔(秒)
    VK_ESC = 0x1B

    def __init__(self, log=print, on_count=lambda n: None) -> None:
        self.rec = StoryRecognizer()
        self.log = log
        self.on_count = on_count
        self.stop_flag = False
        self.paused = False
        self.skipped = 0
        self.jitter = cfg.timing_jitter()  # 光标位移微抖动(默认关闭)

    def stop(self) -> None:
        self.stop_flag = True

    def set_paused(self, on: bool) -> None:
        self.paused = on

    def _grab(self, sct, hwnd):
        x, y, w, h = client_rect_on_screen(hwnd)
        if w <= 0 or h <= 0:
            return None
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
        return np.asarray(shot)[:, :, :3]

    def _press_esc(self) -> None:
        win32api.keybd_event(self.VK_ESC, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(self.VK_ESC, 0, win32con.KEYEVENTF_KEYUP, 0)

    def _move_to(self, tx: int, ty: int) -> None:
        """光标沿随机弧线平滑移动到 (tx,ty),不瞬移。"""
        try:
            sx, sy = win32api.GetCursorPos()
        except Exception:
            sx, sy = tx, ty
        dx, dy = tx - sx, ty - sy
        dist = math.hypot(dx, dy)
        if dist < 2:
            win32api.SetCursorPos((tx, ty))
            return
        # 控制点 = 中点 + 垂直方向随机偏移,形成不同的弧
        px, py = -dy / dist, dx / dist
        off = random.uniform(-0.25, 0.25) * dist
        cx = (sx + tx) / 2 + px * off + random.uniform(-0.08, 0.08) * dist
        cy = (sy + ty) / 2 + py * off + random.uniform(-0.08, 0.08) * dist
        dur = random.uniform(*self.MOVE_TIME)
        steps = max(6, int(dur / 0.012))
        for i in range(1, steps + 1):
            t = i / steps
            tt = t * t * (3 - 2 * t)             # smoothstep 缓动
            u = 1 - tt
            bx = u * u * sx + 2 * u * tt * cx + tt * tt * tx
            by = u * u * sy + 2 * u * tt * cy + tt * tt * ty
            if i < steps and self.jitter:        # 中途微抖动(可选,默认关闭)
                bx += random.uniform(-1.5, 1.5)
                by += random.uniform(-1.5, 1.5)
            win32api.SetCursorPos((int(bx), int(by)))
            time.sleep(dur / steps)
        win32api.SetCursorPos((tx, ty))

    def _click_norm(self, hwnd, pt) -> None:
        """移动到归一化坐标处再点击(用于点确认框「跳过」按钮)。"""
        x, y, w, h = client_rect_on_screen(hwnd)
        self._move_to(int(x + pt[0] * w), int(y + pt[1] * h))
        time.sleep(random.uniform(0.03, 0.08))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(random.uniform(0.03, 0.06))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _click_here(self) -> None:
        """在当前光标位置原地点一下(不移动光标),用于对话/继续/黑屏推进。"""
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.04)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _nudge(self) -> None:
        """沿随机弧线平滑移到附近随机点(不瞬移、不点击),唤出会自动隐藏的剧情控制条。"""
        try:
            sx, sy = win32api.GetCursorPos()
        except Exception:
            return
        ang = random.uniform(0, 2 * math.pi)
        dist = random.uniform(*self.NUDGE_DIST)
        tx, ty = sx + dist * math.cos(ang), sy + dist * math.sin(ang)
        px, py = -math.sin(ang), math.cos(ang)
        off = random.uniform(-0.35, 0.35) * dist
        cx, cy = (sx + tx) / 2 + px * off, (sy + ty) / 2 + py * off
        dur = random.uniform(*self.NUDGE_TIME)
        steps = max(5, int(dur / 0.012))
        for i in range(1, steps + 1):
            t = i / steps
            tt = t * t * (3 - 2 * t)
            u = 1 - tt
            bx = u * u * sx + 2 * u * tt * cx + tt * tt * tx
            by = u * u * sy + 2 * u * tt * cy + tt * tt * ty
            if i < steps and self.jitter:
                bx += random.uniform(-1.0, 1.0)
                by += random.uniform(-1.0, 1.0)
            win32api.SetCursorPos((int(bx), int(by)))
            time.sleep(dur / steps)

    def run(self, nudge: bool = False) -> None:
        self.stop_flag = False
        self.skipped = 0
        hwnd = find_game_hwnd()
        if not hwnd:
            self.log("未找到游戏窗口『王者荣耀世界』,请先运行游戏")
            return
        self.log("实时检测已启动(游戏态不动作;仅游戏前台;F12 急停)")
        dbg = self._open_debug()

        esc_pending = False        # 已按 ESC,等确认框
        esc_t = 0.0
        last_skip = 0.0            # 上次点确认框跳过
        block_esc_until = 0.0      # 此刻前不按 ESC
        last_play_t = -99.0        # 上次见到游戏态
        next_advance = 0.0
        next_nudge = 0.0           # 沉浸式微动:下次允许微动的时刻
        last_ocr = 0.0
        last_dbg = ""
        last_advance_log = ""
        with mss.mss() as sct:
            while not self.stop_flag:
                if self.paused or not is_foreground(hwnd):
                    time.sleep(0.25)
                    continue
                now = time.time()
                if now - last_ocr < self.OCR_INTERVAL:
                    time.sleep(0.05)
                    continue
                last_ocr = now
                f = self._grab(sct, hwnd)
                if f is None:
                    continue

                state, pt = self.rec.classify(f)
                if state != last_dbg:
                    self._dbg(dbg, now, state)
                    last_dbg = state
                # ESC 后超时没等到确认框就放弃,并冷却一段
                if esc_pending and now - esc_t > self.ESC_PENDING_S:
                    esc_pending = False
                    block_esc_until = now + self.ABORT_BLOCK
                    self.log("ESC 后未见确认框,暂停(剧情可能已结束)")
                if state == "play":
                    last_play_t = now
                    esc_pending = False
                recent_play = now - last_play_t < self.GAMEPLAY_STICK

                if state == "confirm" and pt and now - last_skip > self.CONFIRM_GAP:
                    self._click_norm(hwnd, pt)
                    self.skipped += 1
                    self.on_count(self.skipped)
                    self.log(f"✓ 跳过剧情(确认「跳过」)#{self.skipped}")
                    last_skip = now
                    block_esc_until = now + self.POST_SKIP_BLOCK
                    esc_pending = False
                    time.sleep(0.4)

                elif recent_play:
                    pass   # 刚在游戏世界(含边界抖动),不动作

                elif state == "skip":
                    if not esc_pending and now > block_esc_until:
                        self._press_esc()
                        self.log("检测到可跳过剧情 → ESC,等待确认框")
                        esc_pending, esc_t = True, now

                elif state in ("dialogue", "continue", "black"):
                    if now >= next_advance:
                        self._click_here()
                        next_advance = now + random.uniform(*self.ADVANCE_INTERVAL)
                        if last_advance_log != state:
                            self.log({"dialogue": "对话推进(点击)", "continue": "点击空白处继续",
                                      "black": "黑屏过场 → 点击推进"}[state])
                            last_advance_log = state

                elif nudge and state == "immersive":
                    # 可选(界面开关):仅沉浸式剧情才微动唤出控制条,唤出后由上面 ESC/点击接管。
                    # 只移动不点击;游戏态已被 recent_play 先拦截,不会触发。
                    if now >= next_nudge:
                        self._nudge()
                        next_nudge = now + random.uniform(*self.NUDGE_INTERVAL)
                        if last_advance_log != "nudge":
                            self.log("沉浸式剧情 → 鼠标微动唤出控制条")
                            last_advance_log = "nudge"

                # 'interact' / 'immersive'(未开微动):不动作
                time.sleep(0.05)
        self._close_debug(dbg)
        self.log(f"实时检测结束,共跳过 {self.skipped} 段")

    _STATE_CN = {
        "play": "游戏世界", "confirm": "可跳过确认框", "interact": "交互框(宝箱等)",
        "skip": "可跳过剧情", "dialogue": "对话推进", "continue": "点击空白处继续",
        "black": "黑屏过场", "immersive": "沉浸式剧情",
    }

    def _state_cn(self, state: str) -> str:
        return self._STATE_CN.get(state, state)

    def _open_debug(self):
        applog.debug("story: run start")
        return True

    def _dbg(self, fp, now, state) -> None:
        applog.debug(f"story state -> {self._state_cn(state)}")

    def _close_debug(self, fp) -> None:
        applog.debug("story: run end")


if __name__ == "__main__":
    StorySkipper().run(nudge="--nudge" in sys.argv)
