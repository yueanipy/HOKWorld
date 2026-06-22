"""HOKWord 实时采集引擎:跑图经过材料 → 识别 F 提示 → 按 F 采集。

**默认即时采集(快)**:每帧只做「图标分类」(模板匹配 ~3ms,无 OCR)判断有没有可采提示;
只有在**提示刚出现的那一帧**(上升沿)、且需要核对名字时,才 OCR 读一次文字(~150ms)按
白/黑名单决定按不按 —— 同一个提示**全程只 OCR 一次**(老版每帧都 OCR,故慢)。
重现图标唯一、无碰撞,免 OCR 直接按。

边沿触发(仿 better-genshin-impact 自动拾取,避免一直摁 F):
  · 提示刚出现(上升沿)→ 决策一次:可采则按一下 F;碰撞名单命中则跳过(也只判一次)。
  · 提示迟迟没消失(首按可能漏)→ 至多补按一次(间隔 RETRY_GAP),绝不连点。
  · 提示消失够久(ABSENT_RESET)→ 复位,下一个提示再触发。
  · 优先级:白名单(强制采)> 黑名单(碰撞跳,如渡石/滑索)> 图标默认(手型/重现采、其它跳)。

按 F 用 win32 合成输入,需以管理员运行(游戏提权,UIPI 会拦普通权限的合成输入)。
复用 recorder 的窗口/前台/截图 与 1920 归一化。可单独自测:  python -m gather.picker
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import mss
import numpy as np
import win32api
import win32con

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from winenv import client_rect_on_screen, find_game_hwnd, is_admin, is_foreground  # noqa: E402
from gather.recognizer import GatherRecognizer  # noqa: E402

VK_F = 0x46


class GatherPicker:
    DETECT_INTERVAL = 0.05     # 轮询间隔(无提示时仅算图标分,~3ms;尽量快,识别到立刻按,无随机延迟)
    RETRY_GAP = 2.0            # 同一提示两次按 F 的最小间隔(仅"迟迟没消失"时补按,绝不连点)
    MAX_PRESS = 2             # 同一提示最多按几次(首按 + 至多一次补按)
    ABSENT_RESET = 0.5       # 提示消失这么久才算"结束";再出现当新提示(去抖,防闪烁误触发)

    def __init__(self, log=print, on_count=lambda n: None) -> None:
        self.rec = GatherRecognizer()
        self.log = log
        self.on_count = on_count
        self.stop_flag = False
        self.paused = False
        self.picked = 0

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

    def _press_f(self) -> None:
        win32api.keybd_event(VK_F, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(VK_F, 0, win32con.KEYEVENTF_KEYUP, 0)

    def _decide(self, kind, fn):
        """上升沿决策(每个提示只调一次):重现免 OCR;其余读一次名字按白/黑名单判。"""
        if kind == "chongxian" and not self.rec.whitelist:
            return (True, "重现", "chongxian")
        return self.rec.judge(kind, self.rec.read_name(fn))

    def run(self) -> None:
        self.stop_flag = False
        self.picked = 0
        hwnd = find_game_hwnd()
        if not hwnd:
            self.log("未找到游戏窗口『王者荣耀世界』,请先运行游戏")
            return
        if not self.rec.ready:
            self.log("自动采集未标定:缺 F 键帽/图标模板(pick_f / icon_pick / icon_chongxian);现在空转、不按键")
        if not is_admin():
            self.log("⚠ 非管理员运行!按 F 会被提权游戏拦截(识别得到却采不到)→ 请以管理员重启本程序")
        self.log(f"自动采集已启动(图标识别即时按 F,只在新提示出现时读一次名字核对;"
                 f"碰撞名单 {len(self.rec.blacklist)} 条 / 白名单 {len(self.rec.whitelist)} 条;"
                 "NPC/商店/对话不动;仅游戏前台;F12 急停)")

        last_tick = 0.0
        prompt_active = False      # 当前是否处于"一个提示"中(已决策过)
        decided_press = False      # 该提示决策结果:采(True)/跳(False)
        press_round = 0            # 该提示已按次数(首按 + 补按)
        last_press = 0.0
        last_seen = 0.0            # 最近一次见到可动作提示(去抖,防闪烁误复位)
        text = ""
        with mss.mss() as sct:
            while not self.stop_flag:
                if self.paused or not is_foreground(hwnd):
                    prompt_active = False        # 暂停/切走 → 复位,回来当新提示
                    time.sleep(0.2)
                    continue
                now = time.time()
                if now - last_tick < self.DETECT_INTERVAL:
                    time.sleep(0.01)
                    continue
                last_tick = now
                f = self._grab(sct, hwnd)
                if f is None:
                    continue
                kind, fn = self.rec.classify(f)          # 快路:无 OCR
                # 可动作 = 手型/重现;"别的图标"仅当配了白名单才需读字核对(否则直接忽略,不 OCR)
                actionable = kind in ("pick", "chongxian") or (kind == "other" and self.rec.whitelist)
                if actionable:
                    last_seen = now
                    if not prompt_active:                # 上升沿:只在这一帧决策(必要时 OCR 一次)
                        press, text, reason = self._decide(kind, fn)
                        prompt_active, decided_press = True, press
                        if press:
                            self._press_f()
                            self.picked += 1
                            self.on_count(self.picked)
                            self.log(f"采集:{text}  #{self.picked}")
                            press_round, last_press = 1, now
                        else:
                            press_round = 0
                            if reason.startswith("skip"):
                                self.log(f"跳过碰撞名单「{text}」")
                    elif decided_press and press_round < self.MAX_PRESS and now - last_press >= self.RETRY_GAP:
                        self._press_f()                  # 迟迟没消失 → 有限补按(防首按漏),不连点
                        self.log(f"采集(补按):{text}")
                        press_round += 1
                        last_press = now
                elif prompt_active and now - last_seen >= self.ABSENT_RESET:
                    prompt_active, press_round, decided_press = False, 0, False
                time.sleep(0.01)
        self.log(f"自动采集结束,共采 {self.picked} 处")


if __name__ == "__main__":
    GatherPicker().run()
