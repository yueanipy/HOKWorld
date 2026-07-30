'牧场每日任务：完成区域一动物窝，并走到区域二外侧路径锚点。'
from __future__ import annotations

import time
from math import atan2, degrees, hypot

from daily import navigation as nav
from daily import ranch_recognizer as ranch_rec
from daily import recognizer as rec
from daily.ranch_route import RanchLocalOffset, RanchLocalRouteTracker
from daily.base import DailyTask, TaskResult
from daily.config import DailyConfig
from runtime_guard import dev_log



class RanchTask(DailyTask):
    task_id = "ranch"
    name = "牧场"

    FIRST_CAMERA_TURN_PX = -560
    FIRST_STALL_ALIGN_TURN_PX = 427
    FIRST_STALL_ALIGN_STEP_S = 0.15
    FIRST_STALL_POST_TURN_STEP_S = 0.16
    FIRST_STALL_ACTION_GAP_S = 1.0
    FIRST_STALL_GROUND_CONFIRMATIONS = 2
    FIRST_STALL_GROUND_CONFIRM_INTERVAL_S = 0.10
    FIRST_WALK_TIMEOUT_S = 7.0
    FIRST_STALL_TARGET = (-6.5, -9.2)
    FIRST_STALL_CORRIDOR_PX = 2.6
    FIRST_STALL_PROGRESS_STOP = 0.60
    FIRST_STALL_STEP_S = 0.06
    FIRST_STALL_STEP_SETTLE_S = 0.08
    FIRST_STALL_MAX_STEPS = 14
    FIRST_STALL_OVERSHOOT_PROGRESS = 1.12
    FIRST_STALL_CLEAR_CONFIRMATIONS = 2
    FIRST_STALL_PARTIAL_MAX_CHECKS = 3
    FIRST_STALL_RECOVERY_STEPS = 10
    CAMERA_CORRECT_PX_PER_DEG = 6.0
    CAMERA_CORRECT_MAX_PX = 240
    SIDE_TAP_S = 0.11
    SIDE_SETTLE_S = 0.22
    REGION_TWO_MIN_D_STEPS = 8
    REGION_TWO_MAX_D_STEPS = 14
    REGION_TWO_GRASS_MIN_D_STEPS = 12
    REGION_TWO_GRASS_MAX_D_STEPS = 18
    REGION_GRASS_TARGET_X = 11.3
    REGION_GRASS_MIN_STEPS = 2
    REGION_GRASS_CONFIRMATIONS = 2
    REGION_GRASS_CONFIRM_INTERVAL_S = 0.10
    REGION_GRASS_CONFIRM_MAX_DX = 0.65
    REGION_GRASS_CONFIRM_MAX_DY = 0.65
    REGION_TWO_ENTRY_X = 1.60
    REGION_THREE_ENTRY_Y = -11.75
    REGION_TWO_TO_THREE_EXTRA_W_S = 0.04
    REGION_TWO_TO_THREE_EXTRA_SETTLE_S = 0.10
    REGION_FOUR_ENTRY_X = -0.55
    REGION_ENTRY_SETTLE_CHECKS = 3
    REGION_ENTRY_INACTIVE_CONFIRMATIONS = 2
    REGION_ENTRY_SETTLE_S = 0.12
    STALL_MIN_AXIS_DELTA = 0.35
    REGION_TRAVERSE_TIMEOUT_S = 45.0
    REGION_TRANSITION_TIMEOUT_S = 30.0
    REGION_GRASS_TIMEOUT_S = 20.0
    TERMINAL_CLEAR_CONFIRMATIONS = 2
    TERMINAL_CLEAR_INTERVAL_S = 0.10
    SUMMON_CONFIRM_TIMEOUT_S = 3.0
    INTIMACY_APPEAR_TIMEOUT_S = 2.5
    INTIMACY_CLOSE_ATTEMPTS = 2
    INTIMACY_CLOSE_TIMEOUT_S = 2.5
    STALL_STATE_TIMEOUT_S = 2.5
    ACTION_SETTLE_S = 2.0
    FIRST_STALL_ACTION_TRANSITIONS = 3
    FIRST_STALL_FEED_COST_MIN = 100
    PT_SAFE_LEFT_CLICK = (0.585, 0.848)
    PT_INTIMACY_BLANKS = (
        (0.84, 0.72),
        (0.68, 0.86),
    )

    LOCAL_ROUTE_MAX_LOST = 2

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._first_stall_control_baseline: float | None = None
        self._summon_pressed = False
        self._current_region: int | None = None
        configured_region = getattr(ctx, "ranch_open_region", None)
        self.max_open_region = (
            max(1, min(4, int(configured_region)))
            if configured_region is not None
            else DailyConfig().ranch_open_region()
        )


    def _abort_or_fail(self) -> str:
        return TaskResult.ABORT if self.ctx.should_stop() else TaskResult.FAIL

    def _finish_at_open_region(self) -> str:
        '在用户配置的最高开放区域边界正常结束。'
        dev_log(
            f"[daily] 牧场:已遍历至最高开放区域{self.max_open_region}")
        self.ctx.log(
            f"牧场:区域一至区域{self.max_open_region}处理完成，"
            "已越过末区边界且脚下无窝框")
        return TaskResult.SUCCESS

    def _turn_to_first_stall(self) -> bool:
        if not self.ctx.center_camera():
            self.ctx.log("牧场:传送后镜头回正失败")
            return False
        self.ctx.sleep(0.25)
        if not self.ctx.drag_camera(self.FIRST_CAMERA_TURN_PX, steps=16):
            self.ctx.log("牧场:传送后镜头左转失败")
            return False
        self.ctx.sleep(0.35)
        return True

    def _align_first_stall_view(self) -> bool:
        '分阶段完成第一窝补步和镜头转向。'
        ctx = self.ctx
        if ctx.tap("w", self.FIRST_STALL_ALIGN_STEP_S) is False:
            ctx.log("牧场:第一窝转向前补步发送失败")
            return False
        ctx.sleep(self.FIRST_STALL_ACTION_GAP_S)
        if not ctx.drag_camera(self.FIRST_STALL_ALIGN_TURN_PX, steps=16):
            ctx.log("牧场:到达第一窝后的正面视角转动失败")
            return False
        ctx.sleep(self.FIRST_STALL_ACTION_GAP_S)
        if ctx.tap("w", self.FIRST_STALL_POST_TURN_STEP_S) is False:
            ctx.log("牧场:第一窝转向后补步发送失败")
            return False
        ctx.sleep(self.FIRST_STALL_ACTION_GAP_S)
        dev_log(
            "[daily] 牧场第一窝:补步、转向和后补步均已分阶段完成 "
            f"px={self.FIRST_STALL_ALIGN_TURN_PX} "
            f"post_w={self.FIRST_STALL_POST_TURN_STEP_S} "
            f"gap={self.FIRST_STALL_ACTION_GAP_S}")
        return True

    def _confirm_first_stall_arrival(self) -> bool:
        '停步后确认窝状态持续，允许脚下框因动画单帧消失。'
        ctx = self.ctx
        ground_hits = 0
        control_hits = 0
        for index in range(self.FIRST_STALL_GROUND_CONFIRMATIONS):
            frame = ctx.grab()
            ground_box, control_changed = (
                self._first_stall_evidence(
                    frame,
                    self._first_stall_control_baseline,
                )
                if frame is not None
                else (False, False)
            )
            dev_log(
                "[daily] 牧场第一窝:操作前双重条件复核 "
                f"{index + 1}/{self.FIRST_STALL_GROUND_CONFIRMATIONS} "
                f"ground={ground_box} control={control_changed}")
            ground_hits += int(ground_box)
            control_hits += int(control_changed)
            if index + 1 < self.FIRST_STALL_GROUND_CONFIRMATIONS:
                ctx.sleep(self.FIRST_STALL_GROUND_CONFIRM_INTERVAL_S)
        confirmed = (
            ground_hits >= 1
            and control_hits >= 1
        )
        if not confirmed:
            ctx.log(
                "牧场:操作前复核未再次命中脚下框或窝操作状态，"
                "停止后续操作")
        return confirmed

    @classmethod
    def _first_stall_progress(cls, offset: RanchLocalOffset) -> tuple[float, float]:
        target_x, target_y = cls.FIRST_STALL_TARGET
        target_length_sq = target_x * target_x + target_y * target_y
        progress = (
            offset.dx * target_x + offset.dy * target_y
        ) / target_length_sq
        cross_track = abs(
            offset.dx * target_y - offset.dy * target_x
        ) / hypot(target_x, target_y)
        return float(progress), float(cross_track)

    @staticmethod
    def _heading_error(current: float, target: float) -> float:
        return float((target - current + 180.0) % 360.0 - 180.0)

    @classmethod
    def _target_heading(cls, offset: RanchLocalOffset) -> float:
        '由小地图背景平移计算下一次 W 需要的角色朝向。'
        error_x = cls.FIRST_STALL_TARGET[0] - offset.dx
        error_y = cls.FIRST_STALL_TARGET[1] - offset.dy
        player_dx = -error_x
        player_dy = -error_y
        return float((degrees(atan2(player_dx, -player_dy)) + 360.0) % 360.0)

    def _first_stall_arrived(
            self,
            frame,
            control_baseline: float | None,
            ) -> bool:
        '脚下窝框与任意窝操作状态同时出现时确认到达。'
        ground_box, control_changed = self._first_stall_evidence(
            frame,
            control_baseline,
        )
        return ground_box and control_changed

    @staticmethod
    def _first_stall_evidence(
            frame,
            control_baseline: float | None,
            ) -> tuple[bool, bool]:
        '使用同一帧返回脚下框和任意窝操作状态。'
        ground_box, strong_lower_edge = ranch_rec.stall_ground_evidence(frame)
        control_changed = ranch_rec.stall_control_changed(
            frame,
            control_baseline,
        )
        return (
            ground_box or (strong_lower_edge and control_changed),
            control_changed,
        )

    def _recover_first_stall(
            self,
            tracker: RanchLocalRouteTracker,
            control_baseline: float | None,
            ) -> bool:
        '镜头或路线偏移后按局部坐标重新对准第一窝。'
        ctx = self.ctx
        near_target_adjusted = False
        for attempt in range(1, self.FIRST_STALL_RECOVERY_STEPS + 1):
            if ctx.should_stop():
                return False
            frame = ctx.grab()
            if frame is None:
                return False
            offset = tracker.locate(frame)
            if self._first_stall_arrived(
                    frame, control_baseline):
                ctx.log("牧场:局部坐标修正后确认到达第一窝")
                dev_log("[daily] 牧场第一窝:局部修正后视觉确认到达")
                return True
            if offset is None:
                ctx.sleep(0.08)
                continue

            distance = hypot(
                self.FIRST_STALL_TARGET[0] - offset.dx,
                self.FIRST_STALL_TARGET[1] - offset.dy,
            )
            if distance <= 1.3:
                if not near_target_adjusted:
                    near_target_adjusted = True
                    ctx.log("牧场:已到第一窝局部坐标，微调位置并复查操作圆圈")
                    if ctx.tap("w", 0.06) is False:
                        ctx.log("牧场:第一窝局部坐标前进修正发送失败")
                        return False
                    ctx.sleep(0.30)
                    continue
                ctx.center_camera()
                ctx.sleep(0.30)
                frame = ctx.grab()
                offset = tracker.locate(frame) if frame is not None else None
                if (
                    frame is not None
                    and self._first_stall_arrived(
                        frame, control_baseline)
                ):
                    return True
                if ctx.tap("s", 0.06) is False:
                    ctx.log("牧场:第一窝局部坐标后退修正发送失败")
                    return False
                ctx.sleep(0.22)
                continue

            heading = tracker.character_heading(frame)
            if heading is not None:
                target_heading = self._target_heading(offset)
                error = self._heading_error(heading, target_heading)
                if abs(error) > 9.0:
                    correction = int(round(
                        error * self.CAMERA_CORRECT_PX_PER_DEG
                    ))
                    correction = max(
                        -self.CAMERA_CORRECT_MAX_PX,
                        min(self.CAMERA_CORRECT_MAX_PX, correction),
                    )
                    ctx.log(
                        f"牧场:镜头受阻路线修正 {attempt}/"
                        f"{self.FIRST_STALL_RECOVERY_STEPS}，"
                        f"角色朝向={heading:.1f}° 目标={target_heading:.1f}°")
                    if not ctx.drag_camera(correction, steps=8):
                        return False
                    ctx.sleep(0.16)

            if ctx.tap("w", 0.16 if distance > 3.0 else 0.10) is False:
                ctx.log("牧场:第一窝恢复路线前进发送失败")
                return False
            ctx.sleep(0.12)

        ctx.log("牧场:第一窝局部坐标修正到达上限")
        return False

    def _step_to_first_stall(
            self,
            tracker: RanchLocalRouteTracker | None,
            walk_origin: RanchLocalOffset | None,
            control_baseline: float | None,
            ) -> bool:
        '到达接近点后使用 W 碎步逐帧确认第一窝。'
        ctx = self.ctx
        for step in range(self.FIRST_STALL_MAX_STEPS + 1):
            if ctx.should_stop():
                return False
            clear_streak = 0
            partial_checks = 0
            check_index = 0
            frame = None
            while clear_streak < self.FIRST_STALL_CLEAR_CONFIRMATIONS:
                frame = ctx.grab()
                if frame is None:
                    return False
                check_index += 1
                ground_box, control_changed = self._first_stall_evidence(
                    frame,
                    control_baseline,
                )
                if ground_box and control_changed:
                    ctx.log(f"牧场:W 碎步 {step} 次后确认到达第一窝")
                    dev_log(
                        f"[daily] 牧场第一窝:W 碎步 {step} 次后视觉确认到达")
                    return True
                if ground_box or control_changed:
                    partial_checks += 1
                    clear_streak = 0
                    if partial_checks >= self.FIRST_STALL_PARTIAL_MAX_CHECKS:
                        ctx.log(
                            "牧场:第一窝部分条件复查后仍未同时满足，"
                            "继续一次 W 碎步")
                        dev_log(
                            "[daily] 牧场第一窝:部分视觉条件持续，继续碎步 "
                            f"ground={ground_box} control={control_changed}")
                        break
                else:
                    clear_streak += 1
                if clear_streak < self.FIRST_STALL_CLEAR_CONFIRMATIONS:
                    ctx.sleep(self.FIRST_STALL_STEP_SETTLE_S)

            if tracker is not None and walk_origin is not None:
                assert frame is not None
                offset = tracker.locate(frame)
                if offset is not None:
                    relative = RanchLocalOffset(
                        dx=offset.dx - walk_origin.dx,
                        dy=offset.dy - walk_origin.dy,
                        confidence=offset.confidence,
                        inliers=offset.inliers,
                        matches=offset.matches,
                    )
                    progress, _ = self._first_stall_progress(relative)
                    if progress >= self.FIRST_STALL_OVERSHOOT_PROGRESS:
                        ctx.log(
                            "牧场:已越过第一窝坐标容差但视觉条件未同时满足，"
                            "停止继续向前")
                        dev_log(
                            "[daily] 牧场第一窝:碎步到达越界保护 "
                            f"progress={progress:.2f}")
                        return False

            if step >= self.FIRST_STALL_MAX_STEPS:
                break
            if ctx.tap("w", self.FIRST_STALL_STEP_S) is False:
                ctx.log("牧场:第一窝视觉碎步发送失败")
                return False
            ctx.sleep(self.FIRST_STALL_STEP_SETTLE_S)

        ctx.log("牧场:W 碎步达到上限仍未同时识别窝操作状态和脚下方框")
        return False

    def _walk_to_first_stall(
            self,
            tracker: RanchLocalRouteTracker | None,
            ) -> bool:
        '持续按 W 到坐标接近点，碰撞和短时停滞不提前松键。'
        ctx = self.ctx
        deadline = ctx.logical_time() + self.FIRST_WALK_TIMEOUT_S
        near_target = False
        baseline_frame = ctx.grab()
        control_baseline = None
        self._first_stall_control_baseline = control_baseline
        walk_origin = (
            tracker.locate(baseline_frame)
            if tracker is not None and baseline_frame is not None
            else None
        )
        dev_log(
            "[daily] 牧场第一窝:开始持续 W，"
            "使用脚下框+喂食/催产/收获状态停步")
        while ctx.logical_time() < deadline and not ctx.should_stop():
            foreground_lost = False
            with ctx.hold("w") as held:
                if not held:
                    return False
                while ctx.logical_time() < deadline and not ctx.should_stop():
                    if ctx.paused or not ctx.foreground():
                        foreground_lost = True
                        break
                    frame = ctx.grab_nowait()
                    offset = (
                        tracker.locate(frame)
                        if frame is not None and tracker is not None
                        else None
                    )
                    if frame is not None:
                        ground_box, control_changed = (
                            self._first_stall_evidence(
                                frame,
                                control_baseline,
                            )
                        )
                        if ground_box and control_changed:
                            ctx.log("牧场:第一窝双重视觉条件命中，停止 W")
                            dev_log("[daily] 牧场第一窝:双重视觉条件命中，停止 W")
                            return True
                    if frame is not None and tracker is not None:
                        if offset is not None and walk_origin is not None:
                            relative = RanchLocalOffset(
                                dx=offset.dx - walk_origin.dx,
                                dy=offset.dy - walk_origin.dy,
                                confidence=offset.confidence,
                                inliers=offset.inliers,
                                matches=offset.matches,
                            )
                            progress, cross_track = self._first_stall_progress(
                                relative
                            )
                            if (
                                progress >= self.FIRST_STALL_PROGRESS_STOP
                                and cross_track
                                <= self.FIRST_STALL_CORRIDOR_PX
                            ):
                                near_target = True
                                ctx.log(
                                    "牧场:已到第一窝坐标接近点，松开持续 W，"
                                    f"进度={progress:.2f}")
                                dev_log(
                                    "[daily] 牧场第一窝:进入 W 碎步阶段 "
                                    f"progress={progress:.2f}")
                                break
                    time.sleep(0.08)
            if foreground_lost and not ctx.wait_foreground(timeout=None):
                return False
            if near_target:
                return self._step_to_first_stall(
                    tracker,
                    walk_origin,
                    control_baseline,
                )
        if (
            tracker is not None
            and self._recover_first_stall(
                tracker,
                control_baseline,
            )
        ):
            return True
        ctx.log("牧场:连续 W 与局部路线修正后仍未识别第一窝")
        return False

    def _dismiss_intimacy(self) -> bool:
        '关闭亲密度浮层；未出现属于正常状态，出现后必须确认关闭。'
        ctx = self.ctx
        if self._summon_pressed:
            appeared = ctx.wait_until(
                ranch_rec.intimacy_overlay,
                timeout=self.INTIMACY_APPEAR_TIMEOUT_S,
                interval=0.15,
            )
        else:
            frame = ctx.grab()
            appeared = (
                frame is not None
                and ranch_rec.intimacy_overlay(frame)
            )
        if not appeared:
            dev_log("[daily] 牧场:召集后未出现亲密度浮层，继续当前窝")
            return True

        for attempt in range(1, self.INTIMACY_CLOSE_ATTEMPTS + 1):
            current_frame = ctx.grab()
            if (
                current_frame is not None
                and not ranch_rec.intimacy_overlay(current_frame)
            ):
                dev_log(
                    "[daily] 牧场:亲密度浮层在点击前已经关闭 "
                    f"attempt={attempt}")
                return True
            blank_point = self.PT_INTIMACY_BLANKS[
                min(attempt - 1, len(self.PT_INTIMACY_BLANKS) - 1)
            ]
            ctx.log(
                "牧场:亲密度提升浮层，"
                f"点击边缘空白处 {attempt}/{self.INTIMACY_CLOSE_ATTEMPTS}")
            if not ctx.click(blank_point):
                return False
            closed = ctx.wait_until(
                lambda image: not ranch_rec.intimacy_overlay(image),
                timeout=self.INTIMACY_CLOSE_TIMEOUT_S,
                interval=0.15,
                desc="关闭牧场亲密度浮层",
            )
            if closed:
                dev_log(
                    "[daily] 牧场:亲密度浮层关闭成功 "
                    f"attempt={attempt}")
                return True
            frame = ctx.grab()
            if frame is not None and not ranch_rec.intimacy_overlay(frame):
                dev_log(
                    "[daily] 牧场:亲密度浮层在超时边界后已关闭 "
                    f"attempt={attempt}")
                return True
            if attempt < self.INTIMACY_CLOSE_ATTEMPTS:
                ctx.log("牧场:亲密度浮层仍存在，依据新帧重试一次")
        ctx.log("牧场:两次点击后亲密度浮层仍未关闭，停止当前任务")
        return False

    def _summon_animals(
            self,
            initial: ranch_rec.RanchObservation | None = None,
            ) -> bool:
        ctx = self.ctx
        self._summon_pressed = False
        if initial is None:
            for _attempt in range(3):
                initial_frame = ctx.grab()
                if initial_frame is not None:
                    initial = ranch_rec.observe(initial_frame)
                    break
                ctx.sleep(0.10)
        if initial is None:
            ctx.log("牧场:召集前连续三次未取得画面")
            return False
        if initial.summon_countdown:
            ctx.log("牧场:召集动物已处于倒计时，不重复按 T")
            dev_log("[daily] 牧场第一窝:召集倒计时已存在，跳过 T")
            return True
        if not initial.summon_available:
            for attempt in range(1, 4):
                ctx.sleep(0.12)
                frame = ctx.grab()
                if frame is None:
                    continue
                refreshed = ranch_rec.observe(frame)
                if refreshed.summon_countdown:
                    ctx.log("牧场:复查时确认召集动物已处于倒计时")
                    return True
                if refreshed.summon_available:
                    initial = refreshed
                    dev_log(
                        "[daily] 牧场第一窝:首次未识别召集提示，"
                        f"第 {attempt} 次复查命中")
                    break
        if not initial.summon_available:
            ctx.log("牧场:连续复查仍没有召集动物提示，跳过 T 并检查右下角状态")
            dev_log("[daily] 牧场第一窝:三次复查未识别召集提示")
            return True

        def summon_confirmed(image):
            if ranch_rec.intimacy_overlay(image):
                return True
            return ranch_rec.observe(image).summon_countdown

        for attempt in (1, 2):
            if ctx.press("t") is False:
                ctx.log("牧场:召集动物提示存在，但按 T 失败")
                dev_log("[daily] 牧场第一窝:召集提示命中但 ctx.press('t') 返回 False")
                return False
            self._summon_pressed = True
            ctx.log(f"牧场:已识别召集动物提示并按 T，第 {attempt} 次")
            confirmed = ctx.wait_until(
                summon_confirmed,
                timeout=self.SUMMON_CONFIRM_TIMEOUT_S,
                interval=0.15,
                desc="确认牧场召集动物",
            )
            if confirmed:
                dev_log(
                    "[daily] 牧场第一窝:召集提示命中，"
                    f"第 {attempt} 次按 T 后确认成功")
                break
            frame = ctx.grab()
            current = ranch_rec.observe(frame) if frame is not None else None
            if current is not None and current.summon_countdown:
                break
            if attempt == 2:
                ctx.log("牧场:两次按 T 后仍未确认召集状态")
                return False
        return True

    def _wait_stall_observation(
            self,
            timeout: float | None = None,
            ) -> ranch_rec.RanchObservation | None:
        '等待浮层结束后的右下角牧场状态稳定出现。'
        locked_confirmations = 0

        def known_state(frame):
            nonlocal locked_confirmations
            observation = ranch_rec.observe(frame)
            if observation.action == "unknown":
                locked_confirmations = 0
                return None
            if observation.action == "locked":
                locked_confirmations += 1
                return observation if locked_confirmations >= 2 else None
            locked_confirmations = 0
            return observation

        result = self.ctx.wait_until(
            known_state,
            timeout=timeout or self.STALL_STATE_TIMEOUT_S,
            interval=0.12,
            desc="识别牧场右下角动作状态",
        )
        return result if isinstance(result, ranch_rec.RanchObservation) else None

    def _handle_feed_dialog(self) -> bool:
        ctx = self.ctx
        frame = ctx.grab()
        if frame is None or not ranch_rec.feed_dialog_open(frame):
            return False
        if ranch_rec.feed_add_active(frame):
            ctx.log("牧场:当前区域饲料已默认全选，点击添加")
            if not ctx.click(ranch_rec.feed_add_point(frame)):
                ctx.log("牧场:添加饲料按钮点击发送失败")
                return False
            closed = ctx.wait_until(
                lambda image: not ranch_rec.feed_dialog_open(image),
                timeout=5.0,
                interval=0.25,
                desc="牧场添加饲料",
            )
            ctx.sleep(self.ACTION_SETTLE_S)
            return bool(closed)

        ctx.log("牧场:添加饲料按钮为灰色，本窝无需添加")
        if not ctx.click(ranch_rec.PT_FEED_CLOSE):
            ctx.log("牧场:关闭灰色饲料面板点击发送失败")
            return False
        closed = ctx.wait_until(
            lambda image: not ranch_rec.feed_dialog_open(image),
            timeout=4.0,
            interval=0.25,
            desc="关闭不可添加饲料界面",
        )
        return bool(closed)

    def _handle_stall(
            self,
            observation: ranch_rec.RanchObservation,
            ) -> bool:
        '仅按当前催产或手型模板处理动物窝。'
        ctx = self.ctx
        if observation.action in (
                "feed", "waiting", "inactive", "locked",
                ):
            ctx.log(f"牧场:当前窝状态 {observation.action}，无需点击")
            return True
        if observation.action == "unknown":
            ctx.log("牧场:当前窝无有效操作依据，保持不点击")
            return True

        for attempt in (1, 2):
            if ctx.should_stop():
                return False
            action = observation.action
            if not ctx.click(self.PT_SAFE_LEFT_CLICK):
                return False
            ctx.sleep(self.ACTION_SETTLE_S)

            frame = ctx.grab()
            if frame is None:
                return False
            if ranch_rec.feed_dialog_open(frame):
                return self._handle_feed_dialog()
            current = ranch_rec.observe(frame)
            if current.action != action:
                ctx.log(
                    f"牧场:{action} 操作后模板已消失，"
                    f"新状态={current.action}")
                return True
            observation = current
            if attempt == 1:
                ctx.log(f"牧场:{action} 模板仍高亮，依据新帧重试一次")
        ctx.log(f"牧场:{observation.action} 操作两次后仍未确认状态变化")
        return False

    def _handle_first_stall_feed(
            self,
            observation: ranch_rec.RanchObservation,
            *,
            region: int = 1,
            ) -> bool:
        '当前区域第一窝喂食消耗绝对数值超过阈值时打开面板。'
        ctx = self.ctx
        feed_cost = observation.feed_cost
        feed_control = bool(getattr(observation, "feed_control", False))
        if feed_cost is None and feed_control:
            ctx.log(
                f"牧场:区域{region}第一窝已确认喂食状态，"
                "但消耗数字未识别，安全尝试打开一次饲料面板")
        elif (
            feed_cost is None
            or feed_cost <= self.FIRST_STALL_FEED_COST_MIN
        ):
            ctx.log(
                f"牧场:区域{region}第一窝喂食消耗未超过 "
                f"{self.FIRST_STALL_FEED_COST_MIN} 或未出现，"
                "不点击喂食")
            return True
        else:
            ctx.log(
                f"牧场:区域{region}第一窝喂食消耗={feed_cost}，"
                "点击一次并等待状态转换")
        if not ctx.click(self.PT_SAFE_LEFT_CLICK):
            return False
        ctx.sleep(self.ACTION_SETTLE_S)
        frame = ctx.grab()
        if frame is None:
            return False
        if ranch_rec.feed_dialog_open(frame):
            return self._handle_feed_dialog()
        current = ranch_rec.observe(frame)
        if (
            current.feed_cost is None
            or current.feed_cost <= self.FIRST_STALL_FEED_COST_MIN
        ):
            ctx.log(
                f"牧场:区域{region}第一窝点击后喂食消耗已消失，"
                "确认状态已转换")
            return True
        ctx.log(
            f"牧场:区域{region}第一窝点击后饲料面板未出现，"
            f"喂食消耗仍为 {current.feed_cost}")
        return False

    def _resolve_first_stall_actions(
            self,
            observation: ranch_rec.RanchObservation,
            *,
            region: int,
            ) -> ranch_rec.RanchObservation | None:
        '先处理首窝连续催产或手型状态，再返回喂食前状态。'
        current = observation
        for transition in range(1, self.FIRST_STALL_ACTION_TRANSITIONS + 1):
            if current.action not in ("breed", "hand"):
                return current
            self.ctx.log(
                f"牧场:区域{region}第一窝先处理 {current.action}，"
                f"状态转换 {transition}/{self.FIRST_STALL_ACTION_TRANSITIONS}")
            if not self._handle_stall(current):
                return None
            current = self._wait_stall_observation()
            if current is None:
                self.ctx.log(
                    f"牧场:区域{region}第一窝动作后"
                    "未识别到稳定状态")
                return None
        if current.action in ("breed", "hand"):
            self.ctx.log(
                f"牧场:区域{region}第一窝连续动作超过安全上限，"
                "不进入喂食")
            return None
        return current

    def _observe(self) -> ranch_rec.RanchObservation | None:
        frame = self.ctx.grab()
        return ranch_rec.observe(frame) if frame is not None else None

    def _move_to_next_stall(
            self, *, max_steps: int = 8,
            ) -> ranch_rec.RanchObservation | None:
        '横移时命中任意可操作模板即停止，不绑定窝位和动作类型。'
        ctx = self.ctx
        for step in range(1, max_steps + 1):
            if ctx.should_stop():
                return None
            if ctx.tap("d", self.SIDE_TAP_S) is False:
                ctx.log("牧场:前往下一窝的 D 碎步发送失败")
                return None
            ctx.sleep(self.SIDE_SETTLE_S)
            observation = self._observe()
            if observation is None:
                return None
            if observation.action in ("breed", "hand"):
                ctx.log(
                    f"牧场:D 碎步 {step} 次命中 "
                    f"{observation.action} 模板，到达下一窝")
                return observation
        ctx.log("牧场:D 碎步到达上限仍未确认下一窝")
        return None

    def _complete_region_one(
            self,
            first: ranch_rec.RanchObservation,
            tracker: RanchLocalRouteTracker | None = None,
            *,
            terminal: bool = False,
            ) -> bool:
        '按当前状态连续完成区域一各动物窝。'
        ctx = self.ctx
        if not first.marker_key:
            ctx.log("牧场:第一窝动物浮标 OCR 未命中，继续召集和模板识别")
            dev_log("[daily] 牧场第一窝:浮标未命中但不阻断区域一流程")
        if not self._summon_animals(first):
            return False
        if not self._dismiss_intimacy():
            ctx.log("牧场:亲密度浮层未安全关闭，不进入右下角状态识别")
            return False
        first = self._wait_stall_observation()
        if first is None:
            ctx.log("牧场:召集后未识别到稳定的右下角状态")
            return False
        ctx.log(f"牧场:第一窝召集后状态={first.action}，开始完成当前窝")
        current = self._resolve_first_stall_actions(first, region=1)
        if current is None:
            return False
        if not self._handle_first_stall_feed(current):
            return False

        current = self._wait_stall_observation()
        if current is None:
            ctx.log("牧场:第一窝喂食后未识别到稳定状态")
            return False

        if tracker is not None:
            if not self._traverse_region(
                    1,
                    tracker,
                    key="d",
                    initial=current,
                    terminal=terminal,
                    ):
                return False
            ctx.log("牧场:区域一召集和逐窝状态驱动流程已完成")
            return True

        for stall_number in (2, 3):
            observation = self._move_to_next_stall()
            if observation is None:
                return False
            ctx.log(
                f"牧场:第{stall_number}个可操作窝状态="
                f"{observation.action}")
            if not self._handle_stall(observation):
                return False
            current = self._wait_stall_observation()
            if current is None:
                ctx.log(
                    f"牧场:第{stall_number}个可操作窝完成后"
                    "未识别到稳定状态")
                return False
        ctx.log("牧场:区域一召集和状态驱动操作流程已完成")
        return True

    def _set_region(
            self,
            tracker: RanchLocalRouteTracker,
            frame,
            ) -> int | None:
        '用同一帧局部坐标更新区域编号，只在区域变化时记录。'
        offset = tracker.locate(frame)
        return self._set_region_from_offset(offset)

    def _set_region_from_offset(
            self,
            offset: RanchLocalOffset | None,
            ) -> int | None:
        '记录已经由同帧定位得到的区域，避免重复计算特征。'
        region = RanchLocalRouteTracker.region_from_offset(offset)
        if region is not None and region != self._current_region:
            self._current_region = region
            self.ctx.log(
                f"牧场:局部坐标确认进入区域{region} "
                f"(dx={offset.dx:.1f}, dy={offset.dy:.1f})")
            dev_log(
                f"[daily] 牧场区域定位:region={region} "
                f"dx={offset.dx:.2f} dy={offset.dy:.2f} "
                f"confidence={offset.confidence:.3f}")
        return region

    def _complete_region_first_stall(
            self,
            region: int,
            first: ranch_rec.RanchObservation,
            ) -> bool:
        '完成指定区域第一窝当前动作，并在动作转换后处理喂食。'
        ctx = self.ctx
        first_has_stall_state = ranch_rec.stall_arrival_state_visible(first)
        if first.action == "locked" and not first_has_stall_state:
            ctx.log(f"牧场:区域{region}尚未解锁，不点击并继续后续路线")
            return True
        if (
            not first_has_stall_state
            and (
                getattr(first, "outside", False)
                or first.action == "inactive"
            )
        ):
            if region >= 3 and getattr(first, "outside", False):
                ctx.log(
                    f"牧场:区域{region}入口坐标与更换动物状态同时命中，"
                    "按未开放区域继续路线")
                return True
            ctx.log(
                f"牧场:区域{region}只识别到入口或更换动物，"
                "没有首窝右下角状态依据")
            return False

        current = first
        if current.action == "unknown":
            current = self._wait_stall_observation()
            if current is None:
                ctx.log(f"牧场:区域{region}第一窝未识别到稳定状态")
                return False
        current_has_stall_state = ranch_rec.stall_arrival_state_visible(current)
        if current.action == "locked" and not current_has_stall_state:
            ctx.log(
                f"牧场:区域{region}第一窝确认未解锁，不点击")
            return True
        if (
            not current_has_stall_state
            and (
                getattr(current, "outside", False)
                or current.action == "inactive"
            )
        ):
            if region >= 3 and getattr(current, "outside", False):
                ctx.log(
                    f"牧场:区域{region}连续确认更换动物状态，"
                    "当前窝不点击但继续走完用户所选区域")
                return True
            ctx.log(f"牧场:区域{region}第一窝没有有效窝状态依据")
            return False
        ctx.log(f"牧场:区域{region}第一窝当前状态={current.action}")

        current = self._resolve_first_stall_actions(
            current,
            region=region,
        )
        if current is None:
            return False
        if (
            current.action == "feed"
            and current.feed_cost is None
        ):
            for attempt in range(1, self.REGION_ENTRY_SETTLE_CHECKS + 1):
                ctx.sleep(self.REGION_ENTRY_SETTLE_S)
                refreshed = self._observe()
                if refreshed is None:
                    continue
                current = refreshed
                if (
                    current.action != "feed"
                    or current.feed_cost is not None
                ):
                    ctx.log(
                        f"牧场:区域{region}第一窝纯喂食状态"
                        f"第 {attempt} 次复查完成，"
                        f"状态={current.action} 消耗={current.feed_cost}")
                    break

        if not self._handle_first_stall_feed(current, region=region):
            return False
        final = self._wait_stall_observation()
        if final is None:
            ctx.log(
                f"牧场:区域{region}第一窝喂食后"
                "未识别到稳定状态")
            return False
        ctx.log(
            f"牧场:区域{region}第一窝完成，最终状态={final.action}")
        return True

    def _complete_region_two_first_stall(
            self,
            first: ranch_rec.RanchObservation,
            ) -> bool:
        '兼容区域二首窝调用。'
        return self._complete_region_first_stall(2, first)

    @staticmethod
    def _stall_token(observation: ranch_rec.RanchObservation) -> str:
        '提取对少量 OCR 尾字误差不敏感的动物窝标识。'
        return observation.marker_key[:3]

    def _grab_region_state(
            self,
            tracker: RanchLocalRouteTracker,
            ) -> tuple[
                RanchLocalOffset | None,
                ranch_rec.RanchObservation | None,
                object | None,
            ]:
        '从同一帧读取局部坐标和动物窝状态。'
        for attempt in range(self.LOCAL_ROUTE_MAX_LOST + 1):
            frame = self.ctx.grab()
            if frame is None:
                return None, None, None
            offset = tracker.locate(frame)
            if offset is not None:
                return offset, ranch_rec.observe(frame), frame
            if attempt < self.LOCAL_ROUTE_MAX_LOST:
                self.ctx.sleep(0.08)
        return None, None, None

    def _new_region_stall(
            self,
            observation: ranch_rec.RanchObservation,
            offset: RanchLocalOffset,
            *,
            last_token: str,
            last_axis: float | None,
        ) -> bool:
        '判断横移后是否进入了尚未处理的新动物窝。'
        if not ranch_rec.stall_arrival_state_visible(observation):
            return False
        token = self._stall_token(observation)
        if token and token != last_token:
            return True
        return (
            last_axis is None
            or abs(offset.dx - last_axis) >= self.STALL_MIN_AXIS_DELTA
        )

    def _handle_region_stall(
            self,
            region: int,
            observation: ranch_rec.RanchObservation,
            ) -> bool:
        '按识别结果处理区域内非首窝，非操作状态只记录。'
        self.ctx.log(
            f"牧场:区域{region}识别到新窝，状态={observation.action}")
        if observation.action in ("breed", "hand"):
            return self._handle_stall(observation)
        if observation.locked:
            self.ctx.log(f"牧场:区域{region}当前窝未解锁，不点击")
        else:
            self.ctx.log(
                f"牧场:区域{region}当前窝无需收获或采集，继续横移")
        return True

    def _terminal_boundary_clear(
            self,
            tracker: RanchLocalRouteTracker,
            region: int,
            key: str,
            first_offset: RanchLocalOffset,
            first_frame,
            ) -> bool:
        '确认仍在终止区域边界且脚下连续无窝框。'
        ctx = self.ctx
        offset = first_offset
        frame = first_frame
        for index in range(self.TERMINAL_CLEAR_CONFIRMATIONS):
            if index:
                ctx.sleep(self.TERMINAL_CLEAR_INTERVAL_S)
                frame = ctx.grab()
                if frame is None:
                    return False
                offset = tracker.locate(frame)
                if offset is None:
                    return False
            current_region = RanchLocalRouteTracker.region_from_offset(offset)
            coordinate_boundary = (
                RanchLocalRouteTracker.region_boundary_reached(
                    offset,
                    region,
                    key,
                )
            )
            region_band = RanchLocalRouteTracker.REGION_BANDS.get(region)
            within_route_band = bool(
                region_band is not None
                and region_band.min_y - 0.60
                <= offset.dy
                <= region_band.max_y + 0.60
            )
            classification_valid = (
                current_region == region
                or (current_region is None and within_route_band)
            )
            at_boundary = (
                classification_valid
                and coordinate_boundary
            )
            ground_box = ranch_rec.stall_ground_box_visible(frame)
            dev_log(
                f"[daily] 牧场区域{region}终止复核 "
                f"{index + 1}/{self.TERMINAL_CLEAR_CONFIRMATIONS} "
                f"current_region={current_region} "
                f"dx={offset.dx:.2f} dy={offset.dy:.2f} "
                f"coordinate_boundary={coordinate_boundary} "
                f"within_route_band={within_route_band} "
                f"classification_valid={classification_valid} "
                f"boundary={at_boundary} ground={ground_box}")
            if not at_boundary or ground_box:
                return False
        return True

    def _traverse_region(
            self,
            region: int,
            tracker: RanchLocalRouteTracker,
            *,
            key: str,
            initial: ranch_rec.RanchObservation,
            terminal: bool = False,
            ) -> bool:
        '遍历当前区域到指定边界，逐窝识别并执行当前操作。'
        ctx = self.ctx
        offset, current, frame = self._grab_region_state(tracker)
        if offset is None or current is None or frame is None:
            ctx.log(f"牧场:区域{region}遍历前局部坐标读取失败")
            return False
        self._set_region_from_offset(offset)
        last_token = self._stall_token(initial) or self._stall_token(current)
        last_axis: float | None = offset.dx

        if RanchLocalRouteTracker.region_boundary_reached(
                offset, region, key):
            if not terminal:
                ctx.log(f"牧场:区域{region}已位于录制边界")
                return True
            if self._terminal_boundary_clear(
                    tracker,
                    region,
                    key,
                    offset,
                    frame,
                    ):
                ctx.log(
                    f"牧场:区域{region}边界连续确认脚下无框")
                return True

        clock = getattr(ctx, "logical_time", time.monotonic)
        deadline = clock() + self.REGION_TRAVERSE_TIMEOUT_S
        step = 0
        while clock() < deadline:
            if ctx.should_stop():
                return False
            step += 1
            if ctx.tap(key, self.SIDE_TAP_S) is False:
                ctx.log(f"牧场:区域{region}横移输入发送失败")
                return False
            ctx.sleep(self.SIDE_SETTLE_S)
            offset, observation, frame = self._grab_region_state(tracker)
            if offset is None or observation is None or frame is None:
                ctx.log(f"牧场:区域{region}横移时局部坐标连续丢失")
                return False
            self._set_region_from_offset(offset)

            if self._new_region_stall(
                    observation,
                    offset,
                    last_token=last_token,
                    last_axis=last_axis,
                    ):
                if not self._handle_region_stall(region, observation):
                    return False
                token = self._stall_token(observation)
                if token:
                    last_token = token
                last_axis = offset.dx

                offset, current, frame = self._grab_region_state(tracker)
                if offset is None or current is None or frame is None:
                    ctx.log(
                        f"牧场:区域{region}动作后局部坐标读取失败")
                    return False
                self._set_region_from_offset(offset)

            if RanchLocalRouteTracker.region_boundary_reached(
                    offset, region, key):
                if (
                    terminal
                    and not self._terminal_boundary_clear(
                        tracker,
                        region,
                        key,
                        offset,
                        frame,
                    )
                ):
                    ctx.log(
                        f"牧场:区域{region}边界尚未连续确认无框，"
                        "继续横移")
                    continue
                ctx.log(
                    f"牧场:区域{region}遍历完成，"
                    f"{key.upper()} 碎步={step}，"
                    f"dx={offset.dx:.1f}, dy={offset.dy:.1f}")
                return True

        ctx.log(
            f"牧场:区域{region}横移安全超时，"
            "尚未满足区域边界结束条件")
        return False

    def _move_to_region_entry(
            self,
            tracker: RanchLocalRouteTracker,
            region: int,
            *,
            key: str,
            ) -> ranch_rec.RanchObservation | None:
        '按局部坐标进入目标区域，停步后再分析首窝状态。'
        ctx = self.ctx
        clock = getattr(ctx, "logical_time", time.monotonic)
        deadline = clock() + self.REGION_TRANSITION_TIMEOUT_S
        step = 0
        coordinate_gate_reached = False
        extra_w_done = False
        while clock() < deadline:
            if ctx.should_stop():
                return None
            offset, observation, frame = self._grab_region_state(tracker)
            if offset is None or observation is None:
                ctx.log(f"牧场:前往区域{region}时局部坐标连续丢失")
                return None
            current_region = self._set_region_from_offset(offset)
            coordinate_reached = self._region_entry_coordinate_reached(
                offset,
                region,
                key,
            )
            coordinate_gate_reached = (
                coordinate_gate_reached or coordinate_reached
            )
            feed_cost = getattr(observation, "feed_cost", None)
            feed_control = bool(getattr(observation, "feed_control", False))
            locked = bool(getattr(observation, "locked", False))
            outside = bool(getattr(observation, "outside", False))
            dev_log(
                f"[daily] 牧场区域{region}入口步进 step={step} "
                f"key={key.upper()} dx={offset.dx:.2f} dy={offset.dy:.2f} "
                f"classified={current_region} action={observation.action} "
                f"feed_cost={feed_cost} "
                f"feed_control={feed_control} "
                f"locked={locked} outside={outside} "
                f"coordinate_reached={coordinate_reached} "
                f"coordinate_gate={coordinate_gate_reached}")
            if coordinate_gate_reached:
                if (
                        region == 3
                        and key == "w"
                        and self.REGION_TWO_TO_THREE_EXTRA_W_S > 0.0
                        and not extra_w_done
                        ):
                    if ctx.tap(
                            "w",
                            self.REGION_TWO_TO_THREE_EXTRA_W_S,
                            ) is False:
                        ctx.log("牧场:区域二前往区域三的附加W发送失败")
                        return None
                    extra_w_done = True
                    ctx.sleep(self.REGION_TWO_TO_THREE_EXTRA_SETTLE_S)
                    nudged_offset, nudged_observation, nudged_frame = (
                        self._grab_region_state(tracker)
                    )
                    if (
                            nudged_offset is not None
                            and nudged_observation is not None
                            ):
                        offset = nudged_offset
                        observation = nudged_observation
                        frame = nudged_frame
                        current_region = self._set_region_from_offset(offset)
                        feed_cost = getattr(observation, "feed_cost", None)
                        feed_control = bool(
                            getattr(observation, "feed_control", False))
                        locked = bool(getattr(observation, "locked", False))
                        outside = bool(getattr(observation, "outside", False))
                    dev_log(
                        "[daily] 牧场区域二到区域三完成附加W "
                        f"duration={self.REGION_TWO_TO_THREE_EXTRA_W_S:.2f}s "
                        f"dx={offset.dx:.2f} dy={offset.dy:.2f} "
                        f"classified={current_region} "
                        f"action={observation.action} "
                        f"feed_cost={feed_cost} "
                        f"locked={locked}")
                settled = self._settle_region_entry_observation(
                    observation,
                    allow_outside=region >= 3,
                )
                if settled is not None:
                    ctx.log(
                        f"牧场:区域{region}坐标门已通过且首窝状态已确认，"
                        f"停止 {key.upper()}，碎步={step}，"
                        f"dx={offset.dx:.1f}, dy={offset.dy:.1f}，"
                        f"状态={settled.action}")
                    return settled
                dev_log(
                    f"[daily] 牧场区域{region}:坐标门已通过，"
                    "但尚无喂食/催产/收获或连续未解锁依据，继续移动")
            step += 1
            if ctx.tap(key, self.SIDE_TAP_S) is False:
                ctx.log(f"牧场:前往区域{region}的移动输入发送失败")
                return None
            ctx.sleep(self.SIDE_SETTLE_S)
        ctx.log(f"牧场:前往区域{region}安全超时")
        return None

    @classmethod
    def _region_entry_coordinate_reached(
            cls,
            offset: RanchLocalOffset,
            region: int,
            key: str,
            ) -> bool:
        '判断区域入口坐标，区域三优先防止W持续越界。'
        if region == 2 and key == "d":
            return (
                offset.dx >= cls.REGION_TWO_ENTRY_X
                and -10.5 <= offset.dy <= -6.4
            )
        if region == 3 and key == "w":
            return offset.dy <= cls.REGION_THREE_ENTRY_Y
        if region == 4 and key == "a":
            return (
                offset.dx <= cls.REGION_FOUR_ENTRY_X
                and -14.7 <= offset.dy <= -11.2
            )
        return RanchLocalRouteTracker.region_from_offset(offset) == region

    def _settle_region_entry_observation(
            self,
            initial: ranch_rec.RanchObservation,
            *,
            allow_outside: bool = False,
            ) -> ranch_rec.RanchObservation | None:
        '优先返回首窝状态，连续确认后才接受未开放状态。'
        current = initial
        if ranch_rec.stall_arrival_state_visible(current):
            return current
        unavailable = (
            current.action == "locked"
            or (allow_outside and getattr(current, "outside", False))
        )
        if not unavailable:
            return None

        unavailable_confirmations = 1
        unavailable_candidate = current
        for _attempt in range(self.REGION_ENTRY_SETTLE_CHECKS):
            self.ctx.sleep(self.REGION_ENTRY_SETTLE_S)
            refreshed = self._observe()
            if refreshed is None:
                continue
            current = refreshed
            if ranch_rec.stall_arrival_state_visible(current):
                return current
            unavailable = (
                current.action == "locked"
                or (allow_outside and getattr(current, "outside", False))
            )
            if not unavailable:
                return None
            unavailable_confirmations += 1
            unavailable_candidate = current

        if (
            unavailable_confirmations
            >= self.REGION_ENTRY_INACTIVE_CONFIRMATIONS
        ):
            return unavailable_candidate
        return None

    def _local_offset(
            self,
            tracker: RanchLocalRouteTracker,
            ) -> RanchLocalOffset | None:
        '短时复查牧场局部坐标，连续丢失时停止移动。'
        for attempt in range(self.LOCAL_ROUTE_MAX_LOST + 1):
            frame = self.ctx.grab()
            if frame is None:
                return None
            offset = tracker.locate(frame)
            if offset is not None:
                return offset
            if attempt < self.LOCAL_ROUTE_MAX_LOST:
                self.ctx.sleep(0.08)
        return None

    def _move_to_region_two_grass(
            self,
            tracker: RanchLocalRouteTracker,
            ) -> bool:
        '从区域二右边界横移到区域二外草地转向点。'
        ctx = self.ctx
        clock = getattr(ctx, "logical_time", time.monotonic)
        deadline = clock() + self.REGION_GRASS_TIMEOUT_S
        step = 0
        candidate_hits = 0
        candidate_offset: RanchLocalOffset | None = None
        while clock() < deadline:
            if ctx.should_stop():
                return False
            offset = self._local_offset(tracker)
            if offset is None:
                ctx.log("牧场:前往区域二外草地时局部坐标连续丢失")
                return False
            target_candidate = (
                step >= self.REGION_GRASS_MIN_STEPS
                and -10.3 <= offset.dy <= -7.6
                and offset.dx >= self.REGION_GRASS_TARGET_X
            )
            if target_candidate:
                stable = (
                    candidate_offset is not None
                    and abs(offset.dx - candidate_offset.dx)
                    <= self.REGION_GRASS_CONFIRM_MAX_DX
                    and abs(offset.dy - candidate_offset.dy)
                    <= self.REGION_GRASS_CONFIRM_MAX_DY
                )
                candidate_hits = candidate_hits + 1 if stable else 1
                candidate_offset = offset
                dev_log(
                    "[daily] 牧场区域二外草地坐标候选 "
                    f"hits={candidate_hits}/"
                    f"{self.REGION_GRASS_CONFIRMATIONS} "
                    f"dx={offset.dx:.2f} dy={offset.dy:.2f}")
                if candidate_hits < self.REGION_GRASS_CONFIRMATIONS:
                    ctx.sleep(self.REGION_GRASS_CONFIRM_INTERVAL_S)
                    continue
                ctx.log(
                    "牧场:连续确认到达区域二外草地转向点，"
                    f"dx={offset.dx:.1f}, dy={offset.dy:.1f}")
                return True
            else:
                candidate_hits = 0
                candidate_offset = None
            step += 1
            if ctx.tap("d", self.SIDE_TAP_S) is False:
                ctx.log(
                    "牧场:前往区域二外草地的 D 输入发送失败")
                return False
            ctx.sleep(self.SIDE_SETTLE_S)
        ctx.log("牧场:前往区域二外草地安全超时")
        return False

    def _move_to_region_two(
            self,
            tracker: RanchLocalRouteTracker | None = None,
            ) -> ranch_rec.RanchObservation | None:
        '优先按局部坐标到达区域二，未建图时保留视觉回退。'
        ctx = self.ctx
        for step in range(1, self.REGION_TWO_MAX_D_STEPS + 1):
            if ctx.should_stop():
                return None
            if ctx.tap("d", self.SIDE_TAP_S) is False:
                ctx.log("牧场:前往区域二的 D 碎步发送失败")
                return None
            ctx.sleep(self.SIDE_SETTLE_S)
            frame = ctx.grab() if tracker is not None else None
            observation = (
                ranch_rec.observe(frame)
                if frame is not None
                else self._observe()
            )
            if observation is None:
                return None
            if tracker is not None and frame is not None:
                region = self._set_region(tracker, frame)
                if (
                    region == 2
                    and observation.action != "unknown"
                    and not observation.outside
                ):
                    ctx.log(
                        f"牧场:D 碎步 {step} 次按局部坐标到达区域二，"
                        f"当前状态={observation.action}")
                    return observation
            if step >= self.REGION_TWO_MIN_D_STEPS and observation.locked:
                self._current_region = 2
                ctx.log(
                    f"牧场:D 碎步 {step} 次按解锁文字回退确认区域二")
                return observation
        ctx.log("牧场:局部坐标和解锁文字均未确认到达区域二")
        return None

    def _move_to_region_two_outside(self) -> bool:
        ctx = self.ctx
        for step in range(1, self.REGION_TWO_GRASS_MAX_D_STEPS + 1):
            if ctx.should_stop():
                return False
            if ctx.tap("d", self.SIDE_TAP_S) is False:
                ctx.log("牧场:前往区域二外草地的 D 碎步发送失败")
                return False
            ctx.sleep(self.SIDE_SETTLE_S)
            observation = self._observe()
            if observation is None:
                return False
            if (
                step >= self.REGION_TWO_GRASS_MIN_D_STEPS
                and observation.outside
            ):
                ctx.log(
                    f"牧场:D 碎步 {step} 次到达区域二外草地；"
                    "黑色更换动物状态不点击")
                return True
        ctx.log("牧场:未识别到区域二外草地锚点")
        return False

    def run(self) -> str:
        ctx = self.ctx
        if not nav.enter_manage_map(ctx):
            return TaskResult.FAIL
        frame = ctx.grab()
        node = ranch_rec.ranch_node_point(frame) if frame is not None else None
        if not nav.teleport_via_node(ctx, node or ranch_rec.PT_RANCH_NODE):
            return TaskResult.FAIL
        if not ctx.wait_until(
                rec.homeland_loaded,
                timeout=30.0,
                interval=0.35,
                desc="牧场传送完成"):
            return TaskResult.FAIL
        landing = ctx.grab()
        first_route_tracker: RanchLocalRouteTracker | None = None
        if landing is not None:
            try:
                first_route_tracker = RanchLocalRouteTracker(landing)
            except ValueError as exc:
                ctx.log(f"牧场:局部坐标初始化失败，无法安全判断开放区域: {exc}")
        if first_route_tracker is None:
            ctx.log("牧场:未建立局部坐标，停止任务以避免越过用户开放区域")
            return self._abort_or_fail()
        if (
            not self._turn_to_first_stall()
            or not self._walk_to_first_stall(first_route_tracker)
        ):
            return self._abort_or_fail()
        if not self._confirm_first_stall_arrival():
            return self._abort_or_fail()

        
        if not self._align_first_stall_view():
            return self._abort_or_fail()
        first = self._observe()
        if first is None:
            return self._abort_or_fail()
        frame = ctx.grab()
        if frame is not None:
            self._set_region(first_route_tracker, frame)
        if not self._complete_region_one(
                first,
                first_route_tracker,
                terminal=(self.max_open_region == 1),
                ):
            return self._abort_or_fail()
        if self.max_open_region == 1:
            return self._finish_at_open_region()

        region_two = self._move_to_region_entry(
            first_route_tracker,
            2,
            key="d",
        )
        if region_two is None:
            return self._abort_or_fail()
        if not self._complete_region_first_stall(2, region_two):
            return self._abort_or_fail()
        if not self._traverse_region(
                2,
                first_route_tracker,
                key="d",
                initial=region_two,
                terminal=(self.max_open_region == 2),
                ):
            return self._abort_or_fail()
        if self.max_open_region == 2:
            return self._finish_at_open_region()
        if not self._move_to_region_two_grass(first_route_tracker):
            return self._abort_or_fail()

        region_three = self._move_to_region_entry(
            first_route_tracker,
            3,
            key="w",
        )
        if region_three is None:
            return self._abort_or_fail()
        if not self._complete_region_first_stall(3, region_three):
            return self._abort_or_fail()
        if not self._traverse_region(
                3,
                first_route_tracker,
                key="a",
                initial=region_three,
                terminal=(self.max_open_region == 3),
                ):
            return self._abort_or_fail()
        if self.max_open_region == 3:
            return self._finish_at_open_region()

        region_four = self._move_to_region_entry(
            first_route_tracker,
            4,
            key="a",
        )
        if region_four is None:
            return self._abort_or_fail()
        if not self._complete_region_first_stall(4, region_four):
            return self._abort_or_fail()
        if not self._traverse_region(
                4,
                first_route_tracker,
                key="a",
                initial=region_four,
                terminal=True,
                ):
            return self._abort_or_fail()

        return self._finish_at_open_region()
