'培养箱任务：按录制路线到第一行第一列，再执行收割、播种、浇水状态机。'
from __future__ import annotations

import cv2
import numpy as np
import time

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import TaskResult
from daily.tasks._field import FieldTask


class IncubatorTask(FieldTask):
    task_id = "incubator"
    name = "培养箱"
    NODE_PT = R.PT_NODE_INCUBATOR

    
    ARRIVE_SEQ = (("drag", -215),)
    FRONT_TURN_PX = 223
    MINI_MAX_APPROACH = 15
    MINI_MIN_APPROACH = 8
    MINI_MAX_ADVANCE = 60
    STRAFE_TAP = 0.09
    FIRST_COL_TAPS = 8
    FIRST_COL_NUDGE_S = 0.05
    FIRST_COL_W_CORRECT_MAX = 4
    FIRST_COL_W_CORRECT_S = 0.05
    FIELD_EXIT_MISSES = 7
    RIGHT_STATE_RETRIES = 3
    RIGHT_STATE_RETRY_S = 0.5
    REL_RING_SAMPLES = 8
    REL_RING_INTERVAL = 0.15
    REL_RING_PEAK_MIN = 12.0
    REL_RING_SWING_MIN = 9.0
    REL_RING_STD_MIN = 3.0
    ACTION_EVIDENCE_TTL_S = 2.0

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._plot_evidence_at = 0.0
        self._action_evidence: tuple[float, str] | None = None

    def _clear_evidence(self) -> None:
        self._plot_evidence_at = 0.0
        self._action_evidence = None

    def _mark_plot_evidence(self) -> None:
        self._plot_evidence_at = time.monotonic()

    def _plot_evidence_valid(self) -> bool:
        return (self._plot_evidence_at > 0.0
                and time.monotonic() - self._plot_evidence_at <= self.ACTION_EVIDENCE_TTL_S)

    def _approach_field(self) -> bool:
        '首次转角后 W 碎步；脚下框连续两次确认后才算真正到达第一行。'
        ctx = self.ctx
        from runtime_guard import dev_log

        for step in range(1, self.MINI_MAX_APPROACH + 1):
            if ctx.should_stop():
                return False
            self._clear_evidence()
            ctx.tap("w", self.MINI_STEP_S)
            frame = ctx.grab()
            state = rec.plot_frame_state(frame, R.ROI_PLOT_FEET_TIGHT) if frame is not None else None
            rechecked = False
            if state is None:
                
                rechecked = True
                ctx.sleep(self.FRAME_RECHECK_S)
                frame = ctx.grab()
                state = (rec.plot_frame_state(frame, R.ROI_PLOT_FEET_TIGHT)
                         if frame is not None else None)
            dev_log(f"[daily] {self.name}: 首段 W 碎步 {step}/{self.MINI_MAX_APPROACH}"
                    f" 框={state} 复查={rechecked}")
            if step < self.MINI_MIN_APPROACH or state is None:
                continue

            
            ctx.sleep(self.MINI_RECONFIRM_S)
            confirm = ctx.grab()
            state2 = rec.plot_frame_state(confirm, R.ROI_PLOT_FEET_TIGHT) if confirm is not None else None
            if state2 is not None:
                self._clear_evidence()
                ctx.tap("w", self.MINI_STEP_S)
                ctx.sleep(self.MINI_SETTLE_S)
                ctx.log(f"{self.name}:W 碎步 {step} 次连续确认脚下框，再补 1 步 → 到达第一行")
                dev_log(f"[daily] {self.name}: 第一行确认通过({state}/{state2})，补1次W碎步后允许第二次转角")
                return True
            dev_log(f"[daily] {self.name}: 第{step}步首帧见框但复查消失，继续 W")

        ctx.log(f"{self.name}:W 碎步 15 次仍未连续确认脚下框 → 结束本任务")
        return False

    def _goto_field(self) -> bool:
        if not super()._goto_field():
            return False
        return self._turn_front()

    def _turn_front(self) -> bool:
        '踩上第一行后转正，并验证画面确实发生变化。'
        ctx = self.ctx
        from runtime_guard import dev_log

        for attempt in (1, 2, 3):
            if ctx.should_stop():
                return False
            pre = ctx.grab()
            ok = ctx.drag_camera(self.FRONT_TURN_PX)
            ctx.sleep(0.8)
            shift = self._scene_shift(pre, ctx.grab())
            if ok and shift >= 6.0:
                dev_log(f"[daily] {self.name}: 正面转角生效(第{attempt}次,画面差{shift:.1f})")
                return True
            dev_log(f"[daily] {self.name}: 正面转角未生效(第{attempt}次,注入={ok},画面差{shift:.1f})")
        ctx.log(f"{self.name}:正面转角连续失败 → 结束本任务")
        return False

    def _left_to_col1_checked(self) -> bool:
        '按录制路线只用 A 碎步移动到第一列，不再向 D 方向回退。'
        ctx = self.ctx
        from runtime_guard import dev_log

        for step in range(1, self.FIRST_COL_TAPS + 1):
            if ctx.should_stop():
                return False
            self._clear_evidence()
            ctx.tap("a", self.STRAFE_TAP)
            ctx.sleep(self.MINI_SETTLE_S)
        
        self._clear_evidence()
        ctx.tap("a", self.FIRST_COL_NUDGE_S)
        ctx.sleep(self.MINI_SETTLE_S)
        on_plot = self._on_plot()
        dev_log(f"[daily] {self.name}: A 碎步 {self.FIRST_COL_TAPS} 次+{self.FIRST_COL_NUDGE_S}s小步"
                f"到第一列(脚下框={on_plot})")
        if on_plot:
            self._mark_plot_evidence()
            return True

        
        
        ctx.log(f"{self.name}:A 碎步后脚下无框 → 小幅 W 校正第一行第一列")
        for step in range(1, self.FIRST_COL_W_CORRECT_MAX + 1):
            if ctx.should_stop():
                return False
            self._clear_evidence()
            ctx.tap("w", self.FIRST_COL_W_CORRECT_S)
            frame = ctx.grab()
            state = rec.plot_frame_state(frame, R.ROI_PLOT_FEET_TIGHT) if frame is not None else None
            if state is None:
                ctx.sleep(self.FRAME_RECHECK_S)
                frame = ctx.grab()
                state = rec.plot_frame_state(frame, R.ROI_PLOT_FEET_TIGHT) if frame is not None else None
            dev_log(f"[daily] {self.name}: 第一列 W 校正 {step}/{self.FIRST_COL_W_CORRECT_MAX} 框={state}")
            if state is not None:
                self._mark_plot_evidence()
                ctx.log(f"{self.name}:W 校正 {step} 步确认第一行第一列脚下框")
                return True
        ctx.log(f"{self.name}:第一列校正后仍无脚下框 → 停止培养箱，避免跳过首格")
        return False

    def _action_kind(self):
        '培养箱专用同轮判定，并保存可供执行阶段复用的短时凭据。'
        ctx = self.ctx
        from runtime_guard import dev_log

        if self._action_evidence_valid():
            kind = self._action_evidence[1]
            dev_log(f"[daily] {self.name}: 复用停步同轮{kind}凭据，跳过重复金环/图标采样")
            return kind
        self._action_evidence = None

        f0 = ctx.grab()
        state = rec.plot_frame_state(f0, R.ROI_PLOT_FEET_TIGHT) if f0 is not None else None
        if state is None:
            ctx.sleep(self.FRAME_RECHECK_S)
            f0 = ctx.grab()
            state = rec.plot_frame_state(f0, R.ROI_PLOT_FEET_TIGHT) if f0 is not None else None
        if state is None and not self._plot_evidence_valid():
            self._action_evidence = None
            dev_log(f"[daily] {self.name}: 行判定 kind=None(脚下无框且无近期到位凭据)")
            return None
        if state is None:
            dev_log(f"[daily] {self.name}: 行判定单帧漏框，沿用角色未移动期间的到位凭据")
        else:
            self._mark_plot_evidence()

        ring: list[int] = []
        water_score = harvest_score = 0.0
        for _ in range(6):
            if ctx.should_stop():
                self._action_evidence = None
                return None
            frame = ctx.grab()
            if frame is not None:
                water_score = max(water_score, rec.water_action_score(frame))
                harvest_score = max(harvest_score, rec.harvest_action_score(frame))
                ring.append(rec.action_ring_gold_px(frame))
            ctx.sleep(0.15)
        peak = max(ring) if ring else 0
        low = min(ring) if ring else 0
        active = peak >= rec.ACTION_RING_MIN and low <= peak * 0.45
        if not active:
            kind = None
        elif water_score >= rec.WATER_ACTION_TH and water_score >= harvest_score:
            kind = "water"
        elif harvest_score >= rec.HARVEST_ACTION_TH:
            kind = "harvest"
        else:
            kind = "plant"
        self._action_evidence = (time.monotonic(), kind) if kind else None
        dev_log(f"[daily] {self.name}: 行判定 kind={kind}(环峰{peak}/谷{low}/阈{rec.ACTION_RING_MIN}"
                f" 水分{water_score:.2f}/阈{rec.WATER_ACTION_TH}"
                f" 镰分{harvest_score:.2f}/阈{rec.HARVEST_ACTION_TH})")
        return kind

    def _operation_allowed(self, expected_kind: str, *, plot_validated: bool = False) -> bool:
        '培养箱操作总门槛：脚下有框且右下角金环高亮，缺一不可。'
        from runtime_guard import dev_log

        if plot_validated and self._action_evidence_valid(expected_kind):
            dev_log(f"[daily] {self.name}: 复用同轮{expected_kind}凭据，执行阶段不重复采样否决")
            return True
        dev_log(f"[daily] {self.name}: 操作否决({expected_kind}凭据缺失或过期)，原地重新识别")
        return False

    def _action_transition_confirmed(self, kind: str, before_signature) -> bool:
        '动作静默期结束后确认按钮已切换；不在 3 秒动画期内截图或输入。'
        ctx = self.ctx
        from runtime_guard import dev_log

        scores: list[float] = []
        changes: list[float] = []
        for sample in range(3):
            if ctx.should_stop():
                return False
            frame = ctx.grab()
            if frame is not None:
                score = (rec.water_action_score(frame) if kind == "water"
                         else rec.harvest_action_score(frame))
                scores.append(score)
                changes.append(rec.action_icon_change_score(before_signature, frame))
            if sample < 2:
                ctx.sleep(0.12)

        threshold = rec.WATER_ACTION_TH if kind == "water" else rec.HARVEST_ACTION_TH
        below_count = sum(score < threshold for score in scores)
        max_change = max(changes, default=0.0)
        confirmed = bool(scores) and (
            below_count >= min(2, len(scores))
            or max_change >= rec.ACTION_ICON_CHANGE_TH
        )
        dev_log(
            f"[daily] {self.name}: {kind}结果确认 分数="
            f"{','.join(f'{score:.2f}' for score in scores) or '无帧'} "
            f"低于阈值={below_count}/{len(scores)} 图标变化={max_change:.3f}/"
            f"{rec.ACTION_ICON_CHANGE_TH} → {'成功' if confirmed else '未确认'}"
        )
        return confirmed

    @classmethod
    def _relative_ring_metrics(cls, frames) -> tuple[float, float, float, list[float]]:
        '方案三：金环亮度减去内圆/外环背景亮度，抵消夕阳造成的整体增亮。'
        values: list[float] = []
        for frame in frames:
            if frame is None:
                continue
            f = rec.normalize(frame)
            h, w = f.shape[:2]
            cx, cy = int(0.935 * w), int(0.915 * h)
            radius = 88
            x0, y0 = max(0, cx - radius), max(0, cy - radius)
            x1, y1 = min(w, cx + radius), min(h, cy + radius)
            sub = f[y0:y1, x0:x1]
            if sub.size == 0:
                continue
            gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY).astype(np.float32)
            yy, xx = np.mgrid[0:sub.shape[0], 0:sub.shape[1]]
            rr = np.sqrt((xx - (cx - x0)) ** 2 + (yy - (cy - y0)) ** 2)
            ring = gray[(rr >= 25) & (rr <= 60)]
            inner = gray[rr <= 20]
            outer = gray[(rr >= 68) & (rr <= 86)]
            if not ring.size or not inner.size or not outer.size:
                continue
            reference = (float(inner.mean()) + float(outer.mean())) * 0.5
            values.append(float(ring.mean()) - reference)
        if not values:
            return 0.0, 0.0, 0.0, []
        peak = max(values)
        swing = peak - min(values)
        std = float(np.std(values))
        return peak, swing, std, values

    def _relative_ring_active(self) -> bool:
        ctx = self.ctx
        from runtime_guard import dev_log

        frames = []
        for _ in range(self.REL_RING_SAMPLES):
            if ctx.should_stop():
                return False
            frames.append(ctx.grab())
            ctx.sleep(self.REL_RING_INTERVAL)
        peak, swing, std, values = self._relative_ring_metrics(frames)
        active = (peak >= self.REL_RING_PEAK_MIN
                  and swing >= self.REL_RING_SWING_MIN
                  and std >= self.REL_RING_STD_MIN)
        seq = ",".join(f"{v:.1f}" for v in values)
        dev_log(f"[daily] {self.name}: 相对金环[{seq}] 峰{peak:.1f}/{self.REL_RING_PEAK_MIN}"
                f" 摆幅{swing:.1f}/{self.REL_RING_SWING_MIN} std{std:.1f}/{self.REL_RING_STD_MIN}"
                f" → {'高亮' if active else '灰暗'}")
        return active

    def _action_kind_relative_sunset_disabled(self):
        '【停用保留】落日相对亮度状态识别，需要时将方法名恢复为 actionkind。'
        ctx = self.ctx
        from runtime_guard import dev_log

        frame = ctx.grab()
        state = rec.plot_frame_state(frame, R.ROI_PLOT_FEET_TIGHT) if frame is not None else None
        if state is None:
            ctx.sleep(0.3)
            frame = ctx.grab()
            state = rec.plot_frame_state(frame, R.ROI_PLOT_FEET_TIGHT) if frame is not None else None
        if state is None:
            dev_log(f"[daily] {self.name}: 行判定 kind=None(脚下无框)")
            return None
        if not self._relative_ring_active():
            dev_log(f"[daily] {self.name}: 行判定 kind=None(相对金环未闪烁)")
            return None

        water_score = harvest_score = 0.0
        for _ in range(3):
            frame = ctx.grab()
            if frame is not None:
                water_score = max(water_score, rec.water_action_score(frame))
                harvest_score = max(harvest_score, rec.harvest_action_score(frame))
            ctx.sleep(0.12)
        if water_score >= rec.WATER_ACTION_TH and water_score >= harvest_score:
            kind = "water"
        elif harvest_score >= rec.HARVEST_ACTION_TH:
            kind = "harvest"
        else:
            kind = "plant"
        dev_log(f"[daily] {self.name}: 相对亮度行判定 kind={kind}"
                f" 水分{water_score:.2f} 镰分{harvest_score:.2f}")
        return kind

    def _harvest_here(self, *, plot_validated: bool = False) -> bool:
        '识别为收割且双门槛通过时只点一次左键，随后由状态机重新识别。'
        if not self._operation_allowed("harvest", plot_validated=plot_validated):
            return False
        ctx = self.ctx
        from runtime_guard import dev_log

        frame = ctx.grab()
        before_signature = rec.action_icon_signature(frame) if frame is not None else None
        if not ctx.click(R.PT_PLOT_FEET):
            dev_log(f"[daily] {self.name}: 收割左键注入失败，未标记完成")
            return False
        self._action_evidence = None
        dev_log(f"[daily] {self.name}: 收割左键已发送 → 动作后静默 {self.ACTION_SETTLE_S:.1f} 秒")
        ctx.sleep(self.ACTION_SETTLE_S)
        confirmed = self._action_transition_confirmed("harvest", before_signature)
        if confirmed:
            self._mark_plot_evidence()
        dev_log(f"[daily] {self.name}: 收割动作后"
                f"{'确认生效' if confirmed else '未确认生效，原地重识别'}")
        return confirmed

    def _plant_here(self, check_prof: bool = False, *, plot_validated: bool = False) -> bool:
        '空地只在双门槛通过时点一次左键；不再盲点脚下三次。'
        ctx = self.ctx
        from runtime_guard import dev_log

        self._plant_click_sent = False
        if not self._operation_allowed("plant", plot_validated=plot_validated):
            return True
        if not ctx.click(R.PT_PLOT_FEET):
            dev_log(f"[daily] {self.name}: 种植左键注入失败，未标记完成")
            return True
        self._action_evidence = None
        dev_log(f"[daily] {self.name}: 脚下有框且右下角高亮 → 左键种植(单次)")
        ctx.sleep(self.ACTION_SETTLE_S)
        if ctx.wait_until(rec.seed_panel_open, timeout=0.8, interval=0.2, desc="种子面板"):
            planted = self._pick_seed_and_confirm()
            self._plant_click_sent = planted
            if planted:
                self._mark_plot_evidence()
            return planted
        dev_log(f"[daily] {self.name}: 左键后未打开种子面板 → 不重试，继续路线")
        return True

    def _water_here(self, wait_pot_s: float = 0.0, *, plot_validated: bool = False) -> bool:
        '识别为浇水且双门槛通过时只点一次左键，不使用共享的四次点击重试。'
        if not self._operation_allowed("water", plot_validated=plot_validated):
            return False
        ctx = self.ctx
        from runtime_guard import dev_log

        frame = ctx.grab()
        before_signature = rec.action_icon_signature(frame) if frame is not None else None
        if not ctx.click(R.PT_PLOT_FEET):
            dev_log(f"[daily] {self.name}: 浇水左键注入失败，未标记完成")
            return False
        self._action_evidence = None
        dev_log(f"[daily] {self.name}: 浇水左键已发送 → 动作后静默 {self.ACTION_SETTLE_S:.1f} 秒")
        ctx.sleep(self.ACTION_SETTLE_S)
        confirmed = self._action_transition_confirmed("water", before_signature)
        if confirmed:
            self._mark_plot_evidence()
        dev_log(f"[daily] {self.name}: 浇水动作后"
                f"{'确认生效' if confirmed else '未确认生效，原地重识别'}")
        return confirmed

    def _advance_next_row(self) -> bool:
        '培养箱独立 W 碎步：每步结束立即识别，无操作则直接继续下一步。'
        ctx = self.ctx
        from runtime_guard import dev_log

        no_frame_steps = 0
        ctx.log(f"{self.name}:开始 W 碎步寻找下一可操作位置")
        for step in range(1, self.MINI_MAX_ADVANCE + 1):
            if ctx.should_stop():
                return False
            self._clear_evidence()
            ctx.tap("w", self.MINI_STEP_S)
            
            frame = ctx.grab()
            
            
            tight_state = (rec.plot_frame_state(frame, R.ROI_PLOT_FEET_TIGHT)
                           if frame is not None else None)
            route_present = rec.plot_frame_present(frame) if frame is not None else False
            frame_seen = tight_state is not None or route_present
            rechecked = False
            if not frame_seen:
                
                rechecked = True
                ctx.sleep(self.FRAME_RECHECK_S)
                frame = ctx.grab()
                tight_state = (rec.plot_frame_state(frame, R.ROI_PLOT_FEET_TIGHT)
                               if frame is not None else None)
                route_present = rec.plot_frame_present(frame) if frame is not None else False
                frame_seen = tight_state is not None or route_present
            if frame_seen:
                no_frame_steps = 0
                dev_log(f"[daily] {self.name}: W 碎步 {step} 识别到框 → {self.FIELD_EXIT_MISSES}步停止计数刷新为0")
            else:
                no_frame_steps += 1
            dev_log(f"[daily] {self.name}: W 碎步 {step} 紧脚框={tight_state}"
                    f" 路线框={route_present} 识别到框={frame_seen}"
                    f" 复查={rechecked} 连续无框={no_frame_steps}/{self.FIELD_EXIT_MISSES}")

            
            
            if step >= self.MINI_MIN_STEPS and tight_state is not None:
                self._mark_plot_evidence()
                fast_gold = rec.action_ring_gold_px(frame)
                prefilter_min = max(1, int(rec.ACTION_RING_MIN * self.ACTION_RING_PREFILTER_RATIO))
                if fast_gold < prefilter_min:
                    continue
                dev_log(f"[daily] {self.name}: W 碎步 {step} 金环预门={fast_gold}/{prefilter_min}")
                kind = self._action_kind()
                if kind is not None:
                    dev_log(f"[daily] {self.name}: W 碎步 {step} 到达可操作位置"
                            f"(紧脚框={tight_state},联合判定={kind})")
                    return True
            if no_frame_steps >= self.FIELD_EXIT_MISSES:
                ctx.log(f"{self.name}:W 连续 {self.FIELD_EXIT_MISSES} 步在脚下及身前均无框"
                        " → 确认出田，路线结束")
                return False
        ctx.log(f"{self.name}:W 碎步达到 {self.MINI_MAX_ADVANCE} 步上限 → 路线结束")
        return False

    def run(self) -> str:
        '培养箱独立主流程，不调用 FieldTask.run，避免改变农贸作物路线。'
        ctx = self.ctx
        if not self._goto_field():
            if ctx.should_stop():
                return TaskResult.ABORT
            ctx.log(f"{self.name}:W 碎步 15 次仍无脚下框或转角失败 → 结束本任务")
            return TaskResult.FAIL
        if not self._left_to_col1_checked():
            if not ctx.should_stop():
                nav.back_to_world(ctx)
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL

        can_plant = self.DO_PLANT
        self._adv_w_count = 0
        for row in range(1, self.MAX_ROWS + 1):
            if ctx.should_stop():
                return TaskResult.ABORT
            ctx.log(f"{self.name}:第 {row} 行(上限 {self.MAX_ROWS})")
            done_ops: set = set()
            did_any, none_waits, harvest_clicked = False, 0, False
            for _ in range(8):
                if ctx.should_stop():
                    return TaskResult.ABORT
                kind = self._action_kind()
                if kind is None:
                    if harvest_clicked and self._dismiss_proficiency():
                        continue
                    if not did_any:
                        ctx.log(
                            f"{self.name}:第 {row} 行完整闪烁周期未识别到右下角状态"
                            " → 改用慢速 W 碎步继续寻找")
                        break
                    
                    
                    if did_any and none_waits < self.POST_ACTION_RECHECKS:
                        none_waits += 1
                        ctx.sleep(self.POST_ACTION_RECHECK_S)
                        continue
                    break
                none_waits = 0
                if kind == "harvest":
                    if not self.DO_HARVEST or "harvest" in done_ops:
                        break
                    harvest_clicked = self._harvest_here(plot_validated=True)
                    if not harvest_clicked:
                        ctx.log(f"{self.name}:第 {row} 行收割执行被否决，未标记完成，原地重新识别")
                        continue
                    done_ops.add("harvest")
                    did_any = True
                    ctx.sleep(0.5)
                    self._dismiss_proficiency()
                    continue
                if kind == "plant":
                    if not can_plant or "plant" in done_ops:
                        break
                    can_plant = self._plant_here(
                        check_prof=harvest_clicked, plot_validated=True)
                    if not getattr(self, "_plant_click_sent", False):
                        ctx.log(f"{self.name}:第 {row} 行种植执行被否决，未标记完成，原地重新识别")
                        continue
                    done_ops.add("plant")
                    did_any = True
                    continue
                if self.DO_WATER:
                    if self._water_here(plot_validated=True):
                        break
                    ctx.log(f"{self.name}:第 {row} 行浇水执行被否决，原地重新识别")
                    continue
                break
            if row >= self.MAX_ROWS:
                ctx.log(f"{self.name}:达到行数上限 {self.MAX_ROWS} → 结束")
                break
            if not self._advance_next_row():
                ctx.log(f"{self.name}:共处理 {row} 行,结束")
                break
        nav.back_to_world(ctx)
        return TaskResult.ABORT if ctx.should_stop() else TaskResult.SUCCESS
