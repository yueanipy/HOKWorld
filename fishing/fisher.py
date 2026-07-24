'HOKWord 钓鱼自动化引擎。'
from __future__ import annotations

import random
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  

from capture import GameCapture  # noqa: E402
from winenv import find_game_hwnd, is_foreground  # noqa: E402
from fishing.matcher import CLICK_POINT, FishingRecognizer  # noqa: E402
from runtime_guard import dev_log, release_known_keys, safe_click_norm, safe_press_key  # noqa: E402
from paths import is_dev, sessions_dir  # noqa: E402


_CAST_REASON = {
    "too_close": "落点过近", "too_far": "超出落杆范围",
    "not_water": "落点不在水面", "shallow": "水域深度不足",
    "bait_wrong": "当前鱼饵不适用", "bait_empty": "需要装备鱼饵",
}


class FishingBot:
    def __init__(self, log=print, on_count=lambda n: None, debug=True) -> None:
        self.rec = FishingRecognizer()
        self.log = log
        self.on_count = on_count
        self.stop_flag = False
        self.caught = 0
        self.debug = bool(debug) and is_dev()  
        self._dbgdir = None
        self._last_qdbg = 0.0       
        self.cast_pt = list(CLICK_POINT)  
        self._hwnd = None
        self._pause_cond = threading.Condition()
        self._paused = False
        self._paused_total = 0.0
        self._pause_started: float | None = None

    def stop(self) -> None:
        self.stop_flag = True
        with self._pause_cond:
            self._pause_cond.notify_all()
        release_known_keys(self.log)

    def set_paused(self, on: bool) -> None:
        '暂停截图、识别和输入；恢复后沿用原状态及已钓数量。'
        on = bool(on)
        with self._pause_cond:
            if self._paused == on:
                return
            now = time.monotonic()
            if on:
                self._paused = True
                self._pause_started = now
            else:
                if self._pause_started is not None:
                    self._paused_total += now - self._pause_started
                self._pause_started = None
                self._paused = False
            self._pause_cond.notify_all()
        if on:
            self._release_all()

    def _clock(self) -> float:
        '不计暂停时长的逻辑时钟，避免恢复后看门狗立即超时。'
        with self._pause_cond:
            now = time.monotonic()
            current_pause = now - self._pause_started if self._pause_started is not None else 0.0
            return now - self._paused_total - current_pause

    def _wait_if_paused(self) -> bool:
        with self._pause_cond:
            while self._paused and not self.stop_flag:
                self._pause_cond.wait(0.2)
        return not self.stop_flag

    def _sleep(self, seconds: float) -> bool:
        '可被暂停/停止打断，且暂停时间不消耗原等待时长。'
        deadline = self._clock() + max(0.0, seconds)
        while not self.stop_flag:
            if not self._wait_if_paused():
                return False
            remaining = deadline - self._clock()
            if remaining <= 0:
                return True
            with self._pause_cond:
                self._pause_cond.wait(min(0.05, remaining))
        return False

    def _dbg(self, frame, tag: str) -> None:
        if not self.debug or frame is None or self._dbgdir is None:
            return
        try:
            cv2.imwrite(str(self._dbgdir / f"{tag}_{time.strftime('%H%M%S')}_{time.perf_counter()%100:.2f}.jpg"),
                        frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        except Exception as exc:
            dev_log("钓鱼调试帧保存失败", exc)

    def _press_f(self) -> None:
        safe_press_key(0x46, self._stopped, self._foreground, self.log, 0.05)

    def _dbg_qte(self, frame, tag: str) -> None:
        'QTE 审计抓帧(限频 ~4/s):记录每刻画面 + 机器人判定,供事后核对。'
        now = time.perf_counter()
        if now - self._last_qdbg >= 0.25:
            self._dbg(frame, tag)
            self._last_qdbg = now

    _VK = {"1": 0x31, "4": 0x34, "A": 0x41, "D": 0x44, "W": 0x57, "S": 0x53, "F": 0x46}

    
    
    TAP_DOWN_S = 0.012                
    TAP_GAP = (0.006, 0.022)          
    
    
    DISC_PRESS_DELAY = (0.30, 0.95)

    def _press_key(self, k: str) -> None:
        vk = self._VK.get(k.upper())
        if not vk:
            return
        safe_press_key(vk, self._stopped, self._foreground, self.log, 0.02)

    def _tap(self, k: str) -> None:
        '大鱼 QTE 用的快速点按(按下→极短保持→抬起→很小间隔)。'
        vk = self._VK.get(k.upper())
        if not vk:
            return
        safe_press_key(vk, self._stopped, self._foreground, self.log, self.TAP_DOWN_S)
        self._sleep(random.uniform(*self.TAP_GAP))

    def _release_all(self) -> None:
        '急停/退出时把方向键全部抬起,防止某次点按的抬键漏发导致角色卡键乱走。'
        release_known_keys(self.log)

    def _stopped(self) -> bool:
        with self._pause_cond:
            return bool(self.stop_flag or self._paused)

    def _foreground(self) -> bool:
        return bool(self._hwnd and is_foreground(self._hwnd))

    
    
    
    
    HOOK_FAIL_S = 0.5
    HOOK_FIXED_S = 0.14

    def _delay_hook(self) -> None:
        budget = self.HOOK_FAIL_S * 2 / 3 - self.HOOK_FIXED_S    
        self._sleep(random.uniform(0.0, max(0.0, min(0.10, budget))))

    def _delay_action(self) -> None:
        self._sleep(random.uniform(0.10, 0.45))   

    def _delay_cast(self) -> None:
        self._sleep(random.uniform(0.5, 3.0))     

    
    def _grab(self, sct, hwnd):
        if not self._wait_if_paused():
            return None
        return sct.grab()

    def _click(self, hwnd, pt=None) -> None:
        pt = pt if pt is not None else self.cast_pt
        safe_click_norm(hwnd, pt, self._stopped, self._foreground, self.log, 0.04)

    def _esc(self) -> None:
        safe_press_key(0x1B, self._stopped, self._foreground, self.log, 0.05)

    @staticmethod
    def _water_center(frame) -> list:
        '重新检测水域中央(蓝青色大片区域质心),用作落杆点兜底。'
        h, w = frame.shape[:2]
        y0 = int(0.20 * h)
        band = cv2.cvtColor(frame[y0:int(0.66 * h), :], cv2.COLOR_BGR2HSV)
        H, S, V = band[:, :, 0], band[:, :, 1], band[:, :, 2]
        mask = (((H > 80) & (H < 140)) & (V > 40) & (V < 235)).astype(np.uint8) * 255
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cnts:
            c = max(cnts, key=cv2.contourArea)
            m = cv2.moments(c)
            if m["m00"] > 0.04 * mask.size:           
                cx = (m["m10"] / m["m00"]) / w
                cy = (m["m01"] / m["m00"] + y0) / h
                return [float(np.clip(cx, 0.32, 0.68)), float(np.clip(cy, 0.34, 0.58))]
        return [0.50, 0.45]

    def _foreground_ok(self, hwnd) -> bool:
        '等待游戏回到前台(切走时自动暂停动作)。'
        if not self._wait_if_paused():
            return False
        warned = False
        while not is_foreground(hwnd):
            if self.stop_flag:
                return False
            if not warned:
                self.log("游戏不在前台,暂停动作…")
                warned = True
            if not self._sleep(0.3):
                return False
        return True

    def _classify(self, sct, hwnd):
        f = self._grab(sct, hwnd)
        if f is None:
            return "NO_FRAME", None, {}
        st, sc = self.rec.classify(f)
        return st, f, sc

    
    def _save_debug(self, frame, tag, scores=None) -> None:
        d = sessions_dir() / "_debug"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{tag}_{time.strftime('%H%M%S')}.png"
        cv2.imwrite(str(p), frame)
        self.log(f"调试帧已存: sessions/_debug/{p.name}" + (f"  scores={scores}" if scores else ""))

    def _ensure_ready(self, sct, hwnd, timeout=20.0) -> bool:
        start = self._clock()
        last_log = 0.0
        last = None
        while self._clock() - start < timeout:
            if self.stop_flag or not self._foreground_ok(hwnd):
                return False
            st, f, sc = self._classify(sct, hwnd)
            last = (f, sc)
            if self._clock() - last_log > 2.0:
                self.log(f"等待预备态… 识别={st} ready={sc.get('ready')} "
                         f"wait={sc.get('wait')} banner={sc.get('banner')} 黑={sc.get('black_mean')}")
                last_log = self._clock()
            if st == "FISHING_READY":
                return True
            if f is not None and self.rec.is_record_screen(f):   
                self._press_f()
                self._sleep(0.5)
            self._sleep(0.2)
        if last and last[0] is not None:  
            self._save_debug(last[0], "ready_fail", last[1])
        return False

    @staticmethod
    def _is_black(frame) -> bool:
        return float(cv2.cvtColor(cv2.resize(frame, (96, 54)), cv2.COLOR_BGR2GRAY).mean()) < 12.0

    def _confirm_cast(self, sct, hwnd, timeout=5.0) -> bool:
        '抛竿后确认进入"等待咬钩"(取消按钮,winner-take-all 区分预备/等待)。'
        start = self._clock()
        while self._clock() - start < timeout:
            if self.stop_flag or not is_foreground(hwnd):
                return False
            st, _, _ = self._classify(sct, hwnd)
            if st == "WAITING_FOR_BITE":
                return True
            self._sleep(0.15)
        return False

    def _wait_hook(self, sct, hwnd, timeout=30.0) -> str:
        '快速等待上钩(单/小尺度横幅检测,低延迟)。'
        start = self._clock()
        while self._clock() - start < timeout:
            if self.stop_flag or not self._foreground_ok(hwnd):
                return "stop"
            f = self._grab(sct, hwnd)
            if f is None:
                continue
            if self.rec.is_hook(f):
                self._dbg(f, "hookfire")
                return "hook"
            if self._is_black(f):       
                return "settle"
            self._sleep(0.03)            
        return "timeout"

    def _resolve_outcome(self, sct, hwnd, timeout=16.0) -> str:
        '拉杆后判结果。'
        start = self._clock()
        success = False
        n = 0
        while self._clock() - start < timeout:
            if self.stop_flag:
                return "stop"
            if not is_foreground(hwnd):
                self._sleep(0.2)
                continue
            f = self._grab(sct, hwnd)
            if f is None:
                continue
            if n % 3 == 0:
                self._dbg(f, "outcome")   
            n += 1
            if not success and self.rec.is_success(f):
                success = True
                self.log("✓ 检测到渔获奖励")
            
            if self.rec.is_record_screen(f):
                self.log("个人新纪录 → 按 F 放入背包")
                self._press_f()
                self._sleep(0.5)
                continue
            st, _, _ = self._classify(sct, hwnd)
            if st in ("FISHING_READY", "WAITING_FOR_BITE"):
                return "success" if success else "escape"
            self._sleep(0.12)
        return "success" if success else "escape"

    def _back_to_ready(self, sct, hwnd, timeout=8.0) -> None:
        '确保回到预备态。'
        start = self._clock()
        while self._clock() - start < timeout:
            if self.stop_flag:
                return
            f = self._grab(sct, hwnd)
            if f is not None and self.rec.is_record_screen(f):
                self._press_f()
                self._sleep(0.5)
                continue
            st, _, _ = self._classify(sct, hwnd)
            if st in ("FISHING_READY", "WAITING_FOR_BITE"):
                return
            self._sleep(0.3)

    def _wait_bait_panel(self, sct, hwnd, timeout: float = 3.0):
        '等待鱼饵面板出现，成功时返回面板状态。'
        deadline = self._clock() + timeout
        while self._clock() < deadline and not self.stop_flag:
            if not self._foreground_ok(hwnd):
                return None
            frame = self._grab(sct, hwnd)
            if frame is not None:
                state = self.rec.bait_panel_state(frame)
                if state["open"]:
                    return state
            self._sleep(0.15)
        return None

    def _open_bait_panel(self, sct, hwnd):
        '从角色 HUD 或钓鱼预备态进入鱼饵面板。'
        for attempt in range(1, 4):
            frame = self._grab(sct, hwnd)
            if frame is None:
                continue
            state = self.rec.bait_panel_state(frame)
            if state["open"]:
                return state
            fishing_state, _ = self.rec.classify(frame)
            if fishing_state not in ("FISHING_READY", "WAITING_FOR_BITE"):
                self.log(f"鱼饵异常后已回角色界面，重新进入钓鱼({attempt}/3)")
                self._press_key("4")
                self._sleep(0.65)
                continue
            self._press_key("1")
            state = self._wait_bait_panel(sct, hwnd)
            if state is not None:
                return state
        return None

    def _equip_allowed_bait(self, sct, hwnd, reason: str) -> bool:
        '仅在普通鱼饵和浓香拟饵之间选择并验证装备结果。'
        state = self._open_bait_panel(sct, hwnd)
        if state is None:
            self.log("无法打开鱼饵面板，停止钓鱼")
            return False

        current = state["current"]
        if current == "普通鱼饵":
            order = ("浓香拟饵", "普通鱼饵")
        elif current == "浓香拟饵":
            order = ("普通鱼饵", "浓香拟饵")
        else:
            order = ("普通鱼饵", "浓香拟饵")

        for name in order:
            frame = self._grab(sct, hwnd)
            if frame is None:
                continue
            state = self.rec.bait_panel_state(frame)
            point = state["allowed"].get(name) if state else None
            if point is None or name in state["unavailable"]:
                continue
            if reason == "bait_wrong" and name == current:
                continue
            self._click(hwnd, point)
            self._sleep(0.30)
            frame = self._grab(sct, hwnd)
            if frame is None:
                continue
            selected = self.rec.bait_panel_state(frame)
            use_point = selected["use_point"] if selected else None
            if use_point is None:
                continue
            self._click(hwnd, use_point)
            self._sleep(0.45)
            frame = self._grab(sct, hwnd)
            if frame is None:
                continue
            verified = self.rec.bait_panel_state(frame)
            if verified and verified["current"] == name:
                self.log(f"已更换为{name}")
                self._esc()
                self._sleep(0.45)
                frame = self._grab(sct, hwnd)
                if frame is not None and self.rec.bait_panel_state(frame)["open"]:
                    self._esc()
                    self._sleep(0.45)
                    frame = self._grab(sct, hwnd)
                    if frame is not None and self.rec.bait_panel_state(frame)["open"]:
                        self.log("鱼饵已装备，但鱼饵面板未关闭，停止钓鱼")
                        return False
                return True
        self.log("普通鱼饵和浓香拟饵均不可用，停止钓鱼")
        return False

    
    def run(self, count: int = 10, exit_after: bool = False,
            startup_delay_s: float = 3.0) -> None:
        
        if self.stop_flag:
            return
        self.caught = 0
        if count <= 0:           
            count = 1
        hwnd = find_game_hwnd()
        if not hwnd:
            self.log("未找到游戏窗口『王者荣耀世界』,请先运行游戏")
            return
        self._hwnd = hwnd
        if not getattr(self.rec, "ready", False):
            self.log("钓鱼识别未标定:缺 fishing/templates/raw 关键模板,已停止,不会发送键鼠输入")
            dev_log("钓鱼启动失败:识别模板未就绪")
            return
        if self.debug:
            self._dbgdir = sessions_dir() / "_debug" / f"run_{time.strftime('%H%M%S')}"
            self._dbgdir.mkdir(parents=True, exist_ok=True)
            self.log(f"调试抓帧 → sessions/_debug/{self._dbgdir.name}")
        self.log(f"开始钓鱼,目标 {count} 条")
        startup_delay_s = max(0.0, float(startup_delay_s))
        self.log(f"{startup_delay_s:g} 秒后开始 — 请切到游戏并站在钓鱼点(已持竿、可抛竿的预备态)")
        if not self._sleep(startup_delay_s):
            self.log("已取消")
            return

        pulled = False          
        pull_t = 0.0
        current_ad = None       
        last_rapid = 0.0        
        disc_key = None         
        disc_press_at = 0.0     
        disc_pressed = False    
        last_disc_press = 0.0   
        end_streak = 0          
        qframe = 0              
        last_cast = 0.0
        last_progress = self._clock()
        IDLE_STOP_S = 60.0      
        
        t_start = self._clock()
        cast_count = 0          
        cast_pending = False    
        cast_t = 0.0            
        consec_cast_fail = 0    
        CAST_CONFIRM_S = 4.0    
        MAX_CAST_FAIL = 5       
        self.cast_pt = list(CLICK_POINT)  
        cast_adjust = 0         
        err_checked = False     
        bait_switches = 0       
        MAX_BAIT_SWITCHES = 4
        MAX_ADJUST = 3          
        CAST_DY = 0.06          
        ERR_CHECK_S = 0.8       
        with GameCapture(hwnd) as sct:
          try:
            while self.caught < count and not self.stop_flag:
                if not self._foreground_ok(hwnd):
                    break
                f = self._grab(sct, hwnd)
                if f is None:
                    self._sleep(0.03)
                    continue

                
                if not pulled and self.rec.is_hook(f):
                    self._dbg(f, "hookfire")
                    self._delay_hook()
                    self._click(hwnd)
                    self.log("上钩 → 拉杆")
                    pulled = True
                    pull_t = self._clock()
                    current_ad = None
                    end_streak = 0
                    last_progress = self._clock()
                    self._sleep(0.25)
                    continue

                
                
                
                
                
                
                
                
                if pulled:
                    qframe += 1
                    bs, _ = self.rec.button_state(f)
                    
                    if bs in ("ready", "wait"):
                        current_ad = None
                        disc_key = None
                        end_streak += 1
                        if end_streak >= 2:
                            if self.rec.is_success(f):
                                self.caught += 1
                                self.on_count(self.caught)
                                self.log(f"✓ 钓到! 已钓 {self.caught}/{count}")
                            else:
                                self.log("✗ 脱钩/结束,重抛")
                            pulled = False
                            end_streak = 0
                            last_progress = self._clock()
                        self._sleep(0.03)
                        continue
                    end_streak = 0

                    
                    if qframe % 6 == 0 and self.rec.is_record_screen(f):
                        current_ad = None
                        disc_key = None
                        self._dbg(f, "record_caught")
                        self.caught += 1
                        self.on_count(self.caught)
                        self.log(f"✓ 钓到记录鱼! 已钓 {self.caught}/{count}")
                        pulled = False
                        last_progress = self._clock()
                        continue

                    now = self._clock()
                    
                    
                    dk = self.rec.qte_disc(f)
                    if dk:
                        current_ad = None            
                        if dk != disc_key:           
                            disc_key = dk
                            disc_press_at = now + random.uniform(*self.DISC_PRESS_DELAY)
                            disc_pressed = False
                        if now >= disc_press_at and (not disc_pressed or (now - last_disc_press) > 0.45):
                            self._press_key(dk)
                            self.log(f"QTE 按键:{dk}")
                            disc_pressed = True
                            last_disc_press = now
                        last_progress = now
                        self._sleep(0.02)
                        continue
                    disc_key = None                  

                    
                    qk = self.rec.qte_key(f)
                    if qk:
                        if qk != current_ad:
                            self.log(f"快速连点:{qk}")
                            current_ad = qk
                        last_rapid = now
                        last_progress = now
                    if current_ad in ("A", "D") and now - last_rapid <= 0.30:
                        self._tap(current_ad)
                        if now - last_progress > 15.0:
                            self.log("✗ 收线超时,重抛")
                            current_ad = None
                            pulled = False
                        continue
                    current_ad = None                

                    
                    if self.rec.is_success(f):
                        self.caught += 1
                        self.on_count(self.caught)
                        self.log(f"✓ 钓到! 已钓 {self.caught}/{count}")
                        pulled = False
                        last_progress = self._clock()
                        self._sleep(0.15)
                        continue
                    if now - last_progress > 15.0:
                        self.log("✗ 脱钩(超时),重抛")
                        pulled = False
                    self._sleep(0.03)
                    continue

                
                if self.rec.is_record_screen(f):
                    self._dbg(f, "record")
                    self._delay_action()
                    self._press_f()
                    self.log("个人记录 → F 放入背包")
                    last_progress = self._clock()
                    self._sleep(0.5)
                    continue

                
                
                now = self._clock()
                bs, _ = self.rec.button_state(f)
                if (bs != "ready" and cast_pending and not err_checked
                        and now - cast_t > ERR_CHECK_S):
                    bait_error = self.rec.cast_error(f)
                    if bait_error in ("bait_wrong", "bait_empty"):
                        self._save_debug(f, bait_error)
                        bait_switches += 1
                        if bait_switches > MAX_BAIT_SWITCHES:
                            self.log("鱼饵自动更换次数达到上限，停止钓鱼")
                            break
                        self.log(f"检测到{_CAST_REASON[bait_error]}，开始更换允许的鱼饵")
                        if not self._equip_allowed_bait(sct, hwnd, bait_error):
                            break
                        cast_pending = False
                        err_checked = False
                        consec_cast_fail = 0
                        last_cast = 0.0
                        last_progress = self._clock()
                        continue

                
                if bs == "ready":
                    if pulled:                       
                        self.log("✗ 脱钩,未钓到,重抛")
                        pulled = False
                    now = self._clock()
                    
                    if cast_pending and not err_checked and now - cast_t > ERR_CHECK_S:
                        err_checked = True
                        if self.rec.is_level_cap(f):
                            self._save_debug(f, "level_cap")
                            self.log("已达等级上限(脚本不处理),停机")
                            break
                        err = self.rec.cast_error(f)
                        if err in ("bait_wrong", "bait_empty"):
                            self._save_debug(f, err)
                            bait_switches += 1
                            if bait_switches > MAX_BAIT_SWITCHES:
                                self.log("鱼饵自动更换次数达到上限，停止钓鱼")
                                break
                            self.log(f"检测到{_CAST_REASON[err]}，开始更换允许的鱼饵")
                            if not self._equip_allowed_bait(sct, hwnd, err):
                                break
                            cast_pending = False
                            err_checked = False
                            consec_cast_fail = 0
                            last_cast = 0.0
                            last_progress = self._clock()
                            continue
                        if err in ("too_close", "too_far"):
                            cast_adjust += 1
                            if cast_adjust > MAX_ADJUST:
                                self._save_debug(f, "cast_adjust_fail")
                                self.log(f"落杆位置调整 {MAX_ADJUST} 次仍失败({_CAST_REASON[err]}),停机")
                                break
                            if cast_adjust >= MAX_ADJUST:               
                                self.cast_pt = self._water_center(f)
                                self.log(f"落杆{_CAST_REASON[err]} → 重定位水域中央(第{cast_adjust}/{MAX_ADJUST}次)")
                            else:                                       
                                dy = -CAST_DY if err == "too_close" else CAST_DY
                                ny = min(0.70, max(0.30, self.cast_pt[1] + dy))
                                self.cast_pt = [self.cast_pt[0] * 0.6 + 0.5 * 0.4, ny]
                                self.log(f"落杆{_CAST_REASON[err]},{'上移' if err == 'too_close' else '下移'}重试(第{cast_adjust}/{MAX_ADJUST}次)")
                            cast_pending = False
                            last_cast = 0.0                              
                        elif err in ("not_water", "shallow"):
                            self._save_debug(f, "cast_err")
                            self.log(f"落杆失败:{_CAST_REASON[err]}(脚本不处理),停机")
                            break
                    
                    if cast_pending and now - cast_t > CAST_CONFIRM_S:
                        consec_cast_fail += 1
                        cast_pending = False
                        self.log(f"抛竿未进入等待({consec_cast_fail}/{MAX_CAST_FAIL})")
                        if consec_cast_fail >= MAX_CAST_FAIL:
                            self._save_debug(f, "cast_fail")
                            self.log("连续多次抛竿无效,疑似缺饵/朝向不对/钓点异常,停机")
                            break
                    
                    if not cast_pending and now - last_cast > 2.0:
                        self._delay_cast()           
                        self._click(hwnd)
                        cast_count += 1
                        self.log(f"抛竿(目标 {self.caught + 1}/{count})")
                        t_now = self._clock()          
                        last_cast = t_now
                        cast_t = t_now
                        cast_pending = True
                        err_checked = False
                        last_progress = t_now
                    self._sleep(0.15)
                elif bs == "wait":
                    if cast_pending:                 
                        cast_pending = False
                        consec_cast_fail = 0
                        cast_adjust = 0
                    last_progress = self._clock()
                    self._sleep(0.05)                 
                else:                                
                    if pulled and self._clock() - pull_t > 12.0:   
                        self.log("✗ 脱钩(超时未见渔获),重抛")
                        pulled = False
                    if self._clock() - last_progress > IDLE_STOP_S:
                        self._save_debug(f, "stuck")
                        self.log("长时间无可识别钓鱼状态,停止(可能离开钓点/异常界面)")
                        break
                    self._sleep(0.1)
          finally:
            self._release_all()   
        dt = self._clock() - t_start
        mins = dt / 60.0
        rate = (self.caught / mins) if mins > 0 else 0.0
        self.log(f"钓鱼结束,共钓到 {self.caught} 条 · 抛竿 {cast_count} 次 · "
                 f"用时 {int(dt // 60)}分{int(dt % 60)}秒 · 约 {rate:.1f} 条/分")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    FishingBot().run(n)
