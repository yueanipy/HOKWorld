'HOKWord 实时剧情跳过引擎 v3(配合 recognizer v2 正向门 + 两步识别)。'
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from winenv import find_game_hwnd, is_foreground  # noqa: E402
from capture_broker import subscribe_capture  # noqa: E402
from story.recognizer import (  # noqa: E402
    REGION_CONFIRM, REGION_DLG_TITLE, REGION_OPT, REGION_TR, StoryRecognizer,
)
from config import cfg  # noqa: E402
from daily.tasks.monthly_card import (CLICK_POINT as MONTHLY_CLICK_POINT,  # noqa: E402
                                      MonthlyCardWatcher)
from runtime_guard import dev_log, release_known_keys, safe_click_norm, safe_press_key  # noqa: E402

NEUTRAL_PT = (0.5, 0.92)       
                               
                               


class StorySkipper:
    TICK = 0.04             
    TICK_IDLE = 0.2         
    IDLE_AFTER = 2.0        
    CLICK_DELAY = (0.1, 0.3)  
    CONFIRM_GAP = 0.8       
    OPT_CHECK = 0.3         
    OPT_CHECK_NONE = 1.0    
                            
    SKIP_HOLD = 1.0         
    ESC_PENDING_S = 1.3     
    POST_SKIP_BLOCK = 1.2   
    ABORT_BLOCK = 2.0       
    VK_ESC = 0x1B

    def __init__(self, log=print, on_count=lambda n: None, on_foreground=lambda active: None) -> None:
        self.rec = StoryRecognizer()
        self.log = log
        self.on_count = on_count
        self.on_foreground = on_foreground
        self.stop_flag = False
        self.paused = False
        self.skipped = 0
        self._hwnd = None

    def stop(self) -> None:
        self.stop_flag = True
        release_known_keys(self.log)

    def set_paused(self, on: bool) -> None:
        self.paused = bool(on)
        if self.paused:
            release_known_keys(self.log)

    def _press_esc(self) -> bool:
        return safe_press_key(self.VK_ESC, self._stopped, self._foreground, self.log, 0.05)

    def _click_norm(self, hwnd, pt) -> bool:
        '点客户区归一化坐标:直接定位 + 立即点击(不走弧线/不抖动 → 无闪烁、不拖延)。'
        return safe_click_norm(hwnd, pt, self._stopped, self._foreground, self.log, 0.02)

    def _stopped(self) -> bool:
        return bool(self.stop_flag or self.paused)

    def _foreground(self) -> bool:
        return bool(self._hwnd and is_foreground(self._hwnd))

    def run(self, nudge: bool = False, monthly_card: bool = False) -> None:  
        
        if self.stop_flag:
            return
        self.skipped = 0
        _ = cfg.timing_jitter()
        hwnd = find_game_hwnd()
        if not hwnd:
            self.log("未找到游戏窗口『王者荣耀世界』,请先运行游戏")
            return
        self._hwnd = hwnd
        if not self.rec.ready:
            self.log("剧情识别未标定:缺 story/templates/raw 模板(kc_f9/kc_esc/confirm_skip)")
            dev_log("剧情启动失败:识别模板未就绪")
            return
        monthly = MonthlyCardWatcher(monthly_card, self.log)
        if monthly_card:
            if monthly.done_today:
                self.log("每日奖励已完成")
            else:
                self.log("每日奖励已启用")
            self.log("实时检测已启动")
        dbg = self._open_debug()

        esc_pending = False
        esc_t = 0.0
        block_esc_until = 0.0
        last_skip = 0.0
        last_skip_seen = -99.0            
        next_advance = 0.0
        last_opt_check = 0.0
        last_confirm_check = 0.0          
        bar_state = "none"                
        bar_none_streak = 0               
        opt_mode, opt_pt = "none", None   
        last_dbg = ""
        last_log = ""
        last_foreground = None             
        gone_since = 0.0                   
        
        last_active = time.time() - self.IDLE_AFTER
        try:
            base_rois = [REGION_TR, REGION_CONFIRM]
            with subscribe_capture(hwnd, "story", base_rois, self.TICK_IDLE) as frames:
                self.log("实时检测已就绪")
                while not self.stop_flag:
                    foreground = is_foreground(hwnd)
                    if foreground != last_foreground:
                        previous = last_foreground
                        last_foreground = foreground
                        self.on_foreground(foreground)
                        if not foreground:
                            self.log("⏸ 游戏不在最前台 → 已暂停")
                        elif previous is False:
                            self.log("游戏已回到前台 → 自动继续")
                    if self.paused or not foreground:   
                        frames.set_enabled(False)
                        
                        if not self.paused and find_game_hwnd() is None:
                            if not gone_since:
                                gone_since = time.time()
                            elif time.time() - gone_since > 5.0:
                                self.log("游戏已退出 → 停止实时检测")
                                break
                        else:
                            gone_since = 0.0
                        time.sleep(0.2)
                        continue
                    gone_since = 0.0
                    interval = (self.TICK if time.time() - last_active < self.IDLE_AFTER
                                else self.TICK_IDLE)
                    monthly_due = monthly.due()
                    capture_rois = base_rois + (list(monthly.ROIS) if monthly_due else [])
                    snapshot = frames.get_frame(interval, capture_rois, timeout=max(0.6, interval * 3))
                    f = snapshot.frame if snapshot else None
                    if f is None:
                        continue
                    now = time.time()
                    
                    
                    if monthly_due:
                        monthly_state, hud_hits = monthly.classify(f)
                        if monthly_state == "monthly":
                            clicked = safe_click_norm(
                                hwnd, MONTHLY_CLICK_POINT, self._stopped,
                                self._foreground, self.log, 0.02)
                            if clicked:
                                monthly.mark_clicked()
                                self.log("每日奖励已处理")
                                time.sleep(0.35)
                        elif monthly_state == "hud":
                            monthly.mark_hud_reached()
                            self.log("每日奖励检测结束")

                    state, pt = self.rec.classify(f)          
                    if state != last_dbg:
                        self._dbg(dbg, now, state)
                        last_dbg = state
                    if state != "gate":
                        bar_state, opt_mode, opt_pt = "none", "none", None   
                        bar_none_streak = 0               
                    
                    if esc_pending and now - esc_t > self.ESC_PENDING_S:
                        esc_pending = False
                        block_esc_until = now + self.ABORT_BLOCK
                        self._dbg(dbg, now, ">> ESC 后未见确认框(超时放弃)")
                        self.log("ESC 后未见确认框,暂停(剧情可能已结束)")

                    
                    
                    ocr_gap = self.OPT_CHECK if bar_none_streak < 2 else self.OPT_CHECK_NONE
                    if state == "gate" and now - last_opt_check >= ocr_gap:
                        last_opt_check = now
                        prev_bar = bar_state
                        bar_state = self.rec.read_bar(f)          
                        bar_none_streak = 0 if bar_state != "none" else bar_none_streak + 1
                        if bar_state == "skip":
                            last_skip_seen = now
                            self._dbg(dbg, now, ">> read_bar=SKIP")
                        else:
                            if bar_state != prev_bar:             
                                self._dbg(dbg, now, f">> read_bar={bar_state}")   
                            if bar_state == "story":
                                extra = frames.get_frame(self.TICK, capture_rois + [REGION_OPT], timeout=0.6)
                                fo = extra.frame if extra else None       
                                if fo is not None:
                                    opt_mode, opt_pt = self.rec.read_options(fo)
                    skip_active = now - last_skip_seen < self.SKIP_HOLD
                    
                    
                    if state == "confirm" or esc_pending or skip_active or (state == "gate" and bar_state != "none"):
                        last_active = now

                    if state == "confirm":
                        
                        
                        if pt and now - last_confirm_check >= 0.4:
                            last_confirm_check = now
                            extra = frames.get_frame(self.TICK, capture_rois + [REGION_DLG_TITLE], timeout=0.6)
                            ft = extra.frame if extra else None             
                            isd = self.rec.is_skip_dialog(ft if ft is not None else f)
                            self._dbg(dbg, now, f">> CONFIRM态 is_skip_dialog={isd} gap_ok={now-last_skip>self.CONFIRM_GAP}")
                            if now - last_skip > self.CONFIRM_GAP and isd:
                                time.sleep(random.uniform(*self.CLICK_DELAY))   
                                if self._click_norm(hwnd, pt):
                                    self.skipped += 1
                                    self.on_count(self.skipped)
                                    self._dbg(dbg, now, ">> CLICK confirm 完成跳过")
                                    self.log(f"✓ 跳过剧情(确认「跳过」)#{self.skipped}")
                                    last_skip = now
                                    esc_pending = False
                                    block_esc_until = now + self.POST_SKIP_BLOCK
                                    time.sleep(0.3)

                    elif state == "idle":
                        pass   

                    elif skip_active:
                        
                        
                        if not esc_pending and now > block_esc_until:
                            sent = self._press_esc()
                            self._dbg(dbg, now, f">> PRESS ESC sent={sent}")
                            if sent:
                                self.log("检测到可跳过剧情 → ESC,等待确认框")
                                esc_pending, esc_t = True, now

                    elif bar_state == "story":
                        
                        if opt_mode == "hold":
                            if last_log != "hold":
                                self.log("对话选项含「再见/退出」→ 交给你手动选择,脚本不点")
                                self._dbg(dbg, now, ">> HOLD:选项含再见/退出 → 停手交还用户")
                                last_log = "hold"
                        elif now >= next_advance:
                            target = opt_pt if (opt_mode == "choice" and opt_pt) else NEUTRAL_PT
                            if self._click_norm(hwnd, target):
                                next_advance = now + random.uniform(*self.CLICK_DELAY)   
                                tag = "对话选项 → 点第一项" if opt_mode == "choice" else "不可跳过剧情 → 点击推进"
                                if last_log != tag:
                                    self.log(tag)
                                    last_log = tag

                    
                    
        finally:
            monthly.close()
            release_known_keys(self.log)
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
        except Exception as exc:
            dev_log("剧情调试日志打开失败", exc)
            return None

    def _dbg(self, fp, now, state) -> None:
        if fp is None:
            return
        try:
            fp.write(f"{time.strftime('%H:%M:%S')}  {state}\n")
            fp.flush()
        except Exception as exc:
            dev_log("剧情调试日志写入失败", exc)

    def _close_debug(self, fp) -> None:
        try:
            fp and fp.close()
        except Exception as exc:
            dev_log("剧情调试日志关闭失败", exc)


if __name__ == "__main__":
    StorySkipper().run()
