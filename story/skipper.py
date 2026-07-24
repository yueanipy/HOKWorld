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
from daily import regions as DAILY_R  # noqa: E402
from daily.recognizer import in_esc_menu, in_world_hud  # noqa: E402
from story.recognizer import (  # noqa: E402
    REGION_CONFIRM, REGION_DLG_TITLE, REGION_OPT, REGION_TR, StoryRecognizer,
)
from daily.tasks.monthly_card import (CLICK_POINT as MONTHLY_CLICK_POINT,  # noqa: E402
                                      MonthlyCardWatcher)
from runtime_guard import dev_log, release_known_keys, safe_click_norm, safe_press_key  # noqa: E402

NEUTRAL_PT = (0.5, 0.92)       
                               
                               
_PERF_LOG_INTERVAL = 10.0


class _PerfStats:
    '汇总模板快路和实际 OCR 慢路耗时。'

    _SLOW_NAMES = ("bar", "options", "dialog", "monthly", "esc_recovery")

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._reset_values()

    def _reset_values(self) -> None:
        self.frames = 0
        self.classify_total = 0.0
        self.classify_max = 0.0
        self.slow_count = 0
        self.slow_total = 0.0
        self.slow_max = 0.0
        self.slow_counts = {name: 0 for name in self._SLOW_NAMES}
        self.bar_wakes = 0
        self.bar_fallbacks = 0
        self.bar_static_skips = 0

    def add_classify(self, elapsed: float) -> None:
        self.frames += 1
        self.classify_total += elapsed
        self.classify_max = max(self.classify_max, elapsed)

    def add_slow(self, name: str, elapsed: float) -> None:
        self.slow_count += 1
        self.slow_total += elapsed
        self.slow_max = max(self.slow_max, elapsed)
        self.slow_counts[name] += 1

    def add_bar_schedule(self, reason: str) -> None:
        if reason == "wake":
            self.bar_wakes += 1
        elif reason == "fallback":
            self.bar_fallbacks += 1
        elif reason == "static":
            self.bar_static_skips += 1

    @staticmethod
    def _avg_ms(total: float, count: int) -> float:
        return total * 1000.0 / count if count else 0.0

    def maybe_log(self) -> None:
        now = time.perf_counter()
        window = now - self._started
        if window < _PERF_LOG_INTERVAL:
            return
        detail = ",".join(f"{name}={self.slow_counts[name]}" for name in self._SLOW_NAMES)
        dev_log(
            f"剧情性能[{window:.1f}s]: frames={self.frames}; "
            f"classify avg={self._avg_ms(self.classify_total, self.frames):.2f}ms "
            f"max={self.classify_max * 1000.0:.2f}ms; "
            f"ocr calls={self.slow_count} "
            f"avg={self._avg_ms(self.slow_total, self.slow_count):.2f}ms "
            f"max={self.slow_max * 1000.0:.2f}ms; {detail}; "
            f"bar wake/fallback/static-skip="
            f"{self.bar_wakes}/{self.bar_fallbacks}/{self.bar_static_skips}"
        )
        self._started = now
        self._reset_values()


class _BarOcrSchedule:
    '剧情控制条 OCR 的变化唤醒与静态兜底调度。'

    def __init__(self, normal_gap: float, wake_gap: float,
                 fallback_gap: float, change_threshold: float) -> None:
        self.normal_gap = float(normal_gap)
        self.wake_gap = float(wake_gap)
        self.fallback_gap = float(fallback_gap)
        self.change_threshold = float(change_threshold)
        self.last_check = 0.0
        self.negative_signature = None
        self.change_hits = 0

    def due(self, now: float, none_streak: int, signature, change_fn) -> tuple[bool, str]:
        elapsed = now - self.last_check
        if none_streak < 2:
            self.change_hits = 0
            return (elapsed >= self.normal_gap, "normal")
        if elapsed >= self.fallback_gap:
            self.change_hits = 0
            return (True, "fallback")
        changed = change_fn(self.negative_signature, signature) >= self.change_threshold
        self.change_hits = self.change_hits + 1 if changed else 0
        if elapsed >= self.wake_gap and self.change_hits >= 2:
            self.change_hits = 0
            return (True, "wake")
        return (False, "static")

    def record(self, now: float, result: str, signature) -> None:
        self.last_check = now
        self.change_hits = 0
        if result == "none" and signature is not None:
            self.negative_signature = signature.copy()
        else:
            self.negative_signature = None

    def note_non_gate(self) -> None:
        '离开剧情门时清除未完成的连续变化计数。'
        self.change_hits = 0

    def reset_negative(self) -> None:
        self.negative_signature = None
        self.change_hits = 0


class _PostEscGuard:
    '限制一次剧情周期内的 ESC，并处理误开的系统菜单。'

    WATCH_S = 4.0
    HUD_VERIFY_S = 2.0
    NEW_SCENE_IDLE_S = 0.8

    def __init__(self) -> None:
        self.locked = False
        self.recovery_sent = False
        self.ready_for_new_scene = False
        self.watch_until = 0.0
        self.next_scan = 0.0
        self.idle_since: float | None = None

    def primary_sent(self, now: float) -> None:
        '开始观察本次主 ESC 的离场结果。'
        self.watch_until = now + self.WATCH_S
        self.next_scan = now

    def confirmed(self, now: float) -> None:
        '确认跳过成功后锁住当前剧情周期。'
        self.locked = True
        self.recovery_sent = False
        self.ready_for_new_scene = False
        self.idle_since = None
        self.watch_until = now + self.WATCH_S
        self.next_scan = now

    def timeout(self, now: float) -> None:
        '主 ESC 未弹确认框时继续短时检查 HUD 或误开的菜单。'
        self.watch_until = max(self.watch_until, now + self.WATCH_S)
        self.next_scan = min(self.next_scan, now)

    def scan_due(self, now: float, esc_pending: bool) -> bool:
        return (not esc_pending and now <= self.watch_until and now >= self.next_scan)

    def scanned(self, now: float, state: str) -> bool:
        '记录恢复检查结果；返回是否只补按一次 ESC。'
        self.next_scan = now + 0.25
        if state == "menu":
            self.locked = True
            self.ready_for_new_scene = False
            self.idle_since = None
            if not self.recovery_sent:
                self.recovery_sent = True
                self.watch_until = now + self.HUD_VERIFY_S
                return True
            return False
        if state == "hud":
            self.locked = True
            self.ready_for_new_scene = True
            self.idle_since = None
            self.watch_until = 0.0
        return False

    def observe_story_state(self, now: float, state: str, bar_state: str) -> None:
        '用稳定离场或非跳过剧情建立下一剧情周期边界。'
        if not self.locked:
            return
        if state == "idle":
            
            
            if self.recovery_sent:
                self.idle_since = None
                return
            if self.idle_since is None:
                self.idle_since = now
            elif now - self.idle_since >= self.NEW_SCENE_IDLE_S:
                self.ready_for_new_scene = True
            return
        self.idle_since = None
        if state == "gate" and bar_state == "story":
            self.ready_for_new_scene = True

    def allow_primary_esc(self, fresh_skip: bool = False) -> bool:
        '只在新剧情边界和当前帧跳过信号同时成立时解除锁。'
        if not self.locked:
            return True
        if not self.ready_for_new_scene or not fresh_skip:
            return False
        self.locked = False
        self.recovery_sent = False
        self.ready_for_new_scene = False
        self.watch_until = 0.0
        self.idle_since = None
        return True



_POST_ESC_ROIS = (
    DAILY_R.ROI_ESC_GRID,       
    (0.69, 0.01, 1.00, 0.97),  
    (0.00, 0.81, 0.31, 1.00),  
)


def _post_esc_state(frame) -> str:
    '识别误开的 ESC 菜单或已经恢复的角色 HUD。'
    if in_esc_menu(frame):
        return "menu"
    if in_world_hud(frame):
        return "hud"
    return "other"


class StorySkipper:
    TICK = 0.04             
    TICK_IDLE = 0.2         
    IDLE_AFTER = 2.0        
    CLICK_DELAY = (0.1, 0.3)  
    CONFIRM_GAP = 0.8       
    OPT_CHECK = 0.3         
    OPT_CHECK_NONE = 4.0    
    BAR_WAKE_GAP = 0.5      
    BAR_WAKE_CHANGE = 3.0   
    CONFIRM_SCAN_IDLE = 1.0 
    SKIP_HOLD = 1.0         
    ESC_PENDING_S = 1.3     
    POST_SKIP_BLOCK = 1.2   
    ABORT_BLOCK = 2.0       
    DEBUG_FLUSH_INTERVAL = 5.0  
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
        self._debug_last_flush = 0.0

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
        last_confirm_check = 0.0          
        last_confirm_capture = -99.0      
        bar_state = "none"                
        bar_none_streak = 0               
        opt_mode, opt_pt = "none", None   
        last_dbg = ""
        last_log = ""
        last_foreground = None             
        gone_since = 0.0                   
        
        last_active = time.time() - self.IDLE_AFTER
        perf = _PerfStats()
        bar_schedule = _BarOcrSchedule(
            self.OPT_CHECK, self.BAR_WAKE_GAP, self.OPT_CHECK_NONE, self.BAR_WAKE_CHANGE)
        post_esc_guard = _PostEscGuard()
        try:
            base_rois = [REGION_TR]
            
            
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
                    request_now = time.time()
                    confirm_capture_due = (
                        esc_pending
                        or request_now - last_confirm_capture >= self.CONFIRM_SCAN_IDLE)
                    post_esc_scan_due = post_esc_guard.scan_due(request_now, esc_pending)
                    capture_rois = base_rois + (
                        [REGION_CONFIRM] if confirm_capture_due else []) + (
                        list(monthly.ROIS) if monthly_due else []) + (
                        list(_POST_ESC_ROIS) if post_esc_scan_due else [])
                    snapshot = frames.get_frame(interval, capture_rois, timeout=max(0.6, interval * 3))
                    f = snapshot.frame if snapshot else None
                    if f is None:
                        continue
                    if confirm_capture_due:
                        last_confirm_capture = request_now
                    now = time.time()
                    
                    
                    if monthly_due:
                        monthly_started = time.perf_counter()
                        monthly_state, hud_hits = monthly.classify(f)
                        perf.add_slow("monthly", time.perf_counter() - monthly_started)
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

                    classify_started = time.perf_counter()
                    
                    
                    state, pt = self.rec.classify(
                        f, check_confirm=confirm_capture_due)
                    perf.add_classify(time.perf_counter() - classify_started)
                    if state != last_dbg:
                        self._dbg(dbg, now, state)
                        last_dbg = state
                    if state != "gate":
                        bar_state, opt_mode, opt_pt = "none", "none", None   
                        bar_schedule.note_non_gate()
                        
                        
                        if state == "confirm":
                            bar_none_streak = 0
                            bar_schedule.reset_negative()
                    
                    if esc_pending and now - esc_t > self.ESC_PENDING_S:
                        esc_pending = False
                        block_esc_until = now + self.ABORT_BLOCK
                        post_esc_guard.timeout(now)
                        self._dbg(dbg, now, ">> ESC 后未见确认框(超时放弃)")
                        self.log("ESC 后未见确认框,暂停(剧情可能已结束)")

                    
                    bar_signature = None
                    bar_due = False
                    if state == "gate":
                        if bar_none_streak >= 2:
                            bar_signature = self.rec.bar_visual_signature(f)
                        bar_due, schedule_reason = bar_schedule.due(
                            now, bar_none_streak, bar_signature, self.rec.bar_visual_change)
                        perf.add_bar_schedule(schedule_reason)
                    if state == "gate" and bar_due:
                        prev_bar = bar_state
                        bar_started = time.perf_counter()
                        bar_state = self.rec.read_bar(f)          
                        perf.add_slow("bar", time.perf_counter() - bar_started)
                        if bar_state == "none" and bar_signature is None:
                            bar_signature = self.rec.bar_visual_signature(f)
                        bar_schedule.record(now, bar_state, bar_signature)
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
                                    options_started = time.perf_counter()
                                    opt_mode, opt_pt = self.rec.read_options(fo)
                                    perf.add_slow("options", time.perf_counter() - options_started)
                    skip_active = now - last_skip_seen < self.SKIP_HOLD
                    post_esc_guard.observe_story_state(now, state, bar_state)

                    
                    
                    if post_esc_scan_due:
                        recovery_started = time.perf_counter()
                        post_esc_state = _post_esc_state(f)
                        perf.add_slow("esc_recovery", time.perf_counter() - recovery_started)
                        if post_esc_guard.scanned(now, post_esc_state):
                            sent = self._press_esc()
                            self._dbg(dbg, now, f">> RECOVER ESC menu sent={sent}")
                            if sent:
                                self.log("检测到多按 ESC 打开菜单 → 补按一次返回角色界面")
                            
                            perf.maybe_log()
                            continue
                    
                    
                    if state == "confirm" or esc_pending or skip_active or (state == "gate" and bar_state != "none"):
                        last_active = now

                    if state == "confirm":
                        
                        
                        if pt and now - last_confirm_check >= 0.4:
                            last_confirm_check = now
                            extra = frames.get_frame(self.TICK, capture_rois + [REGION_DLG_TITLE], timeout=0.6)
                            ft = extra.frame if extra else None             
                            dialog_started = time.perf_counter()
                            isd = self.rec.is_skip_dialog(ft if ft is not None else f)
                            perf.add_slow("dialog", time.perf_counter() - dialog_started)
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
                                    post_esc_guard.confirmed(now)
                                    time.sleep(0.3)

                    elif state == "idle":
                        pass   

                    elif skip_active:
                        
                        
                        if (not esc_pending and now > block_esc_until
                                and post_esc_guard.allow_primary_esc(
                                    fresh_skip=(bar_state == "skip"))):
                            sent = self._press_esc()
                            self._dbg(dbg, now, f">> PRESS ESC sent={sent}")
                            if sent:
                                self.log("检测到可跳过剧情 → ESC,等待确认框")
                                esc_pending, esc_t = True, now
                                post_esc_guard.primary_sent(now)

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

                    
                    
                    perf.maybe_log()
        finally:
            monthly.close()
            release_known_keys(self.log)
            self._close_debug(dbg)
        self.log(f"实时检测结束,共跳过 {self.skipped} 段")

    
    def _open_debug(self):
        try:
            d = HERE.parent / "sessions"
            d.mkdir(parents=True, exist_ok=True)
            fp = open(d / "_story_debug.log", "a", encoding="utf-8", buffering=64 * 1024)
            fp.write(f"\n==== run {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
            fp.flush()
            self._debug_last_flush = time.monotonic()
            return fp
        except Exception as exc:
            dev_log("剧情调试日志打开失败", exc)
            return None

    def _dbg(self, fp, now, state) -> None:
        if fp is None:
            return
        try:
            fp.write(f"{time.strftime('%H:%M:%S')}  {state}\n")
            current = time.monotonic()
            if (str(state).startswith(">>")
                    or current - self._debug_last_flush >= self.DEBUG_FLUSH_INTERVAL):
                fp.flush()
                self._debug_last_flush = current
        except Exception as exc:
            dev_log("剧情调试日志写入失败", exc)

    def _close_debug(self, fp) -> None:
        try:
            fp and fp.close()
        except Exception as exc:
            dev_log("剧情调试日志关闭失败", exc)


if __name__ == "__main__":
    StorySkipper().run()
