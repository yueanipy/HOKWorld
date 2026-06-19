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

from recorder import client_rect_on_screen, find_game_hwnd, is_foreground  # noqa: E402
from story.recognizer import StoryRecognizer  # noqa: E402


class StorySkipper:
    OCR_INTERVAL = 0.45            # 限频识别
    ADVANCE_INTERVAL = (0.6, 1.0)  # 对话/继续/黑屏 推进点击间隔
    CONFIRM_GAP = 0.8             # 两次点确认框最小间隔
    ESC_PENDING_S = 1.3          # 按 ESC 后等确认框的最长时间
    POST_SKIP_BLOCK = 2.0        # 跳过后这么久内不按 ESC
    ABORT_BLOCK = 2.5            # 放弃后这么久内不按 ESC
    GAMEPLAY_STICK = 1.2         # 见到游戏态后这么久内都当游戏态(跨过边界抖动)
    MOVE_TIME = (0.3, 0.8)       # 光标移动用时
    VK_ESC = 0x1B

    def __init__(self, log=print, on_count=lambda n: None) -> None:
        self.rec = StoryRecognizer()
        self.log = log
        self.on_count = on_count
        self.stop_flag = False
        self.paused = False
        self.skipped = 0

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
            if i < steps:                        # 中途微抖动,最后一步不抖
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

                time.sleep(0.05)
        self._close_debug(dbg)
        self.log(f"实时检测结束,共跳过 {self.skipped} 段")

    def _open_debug(self):
        try:
            d = HERE.parent / "sessions"
            d.mkdir(parents=True, exist_ok=True)
            fp = open(d / "_story_debug.log", "a", encoding="utf-8")
            fp.write(f"\n==== run {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
            fp.flush()
            return fp
        except Exception:
            return None

    def _dbg(self, fp, now, state) -> None:
        if fp is None:
            return
        try:
            fp.write(f"{time.strftime('%H:%M:%S')}  {state}\n")
            fp.flush()
        except Exception:
            pass

    def _close_debug(self, fp) -> None:
        try:
            fp and fp.close()
        except Exception:
            pass


if __name__ == "__main__":
    StorySkipper().run(nudge="--nudge" in sys.argv)
