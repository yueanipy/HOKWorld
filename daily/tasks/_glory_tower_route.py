'烹饪/制药每日任务共用的荣耀塔落点后视觉导航。'
from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass

import daily.recognizer as rec
from daily.base import DailyTask, TaskResult
from world_map import teleport_to
from runtime_guard import dev_log


@dataclass(frozen=True)
class TowerRouteSpec:
    '从荣耀塔渡石落点到一个制作台的局部路线。'

    target_word: str
    turn_total_px: int
    coarse_pulses: int
    near_words: tuple[str, ...] = ()
    recovery_side: str = "a"
    coarse_step_s: float = 0.34
    fine_pulses: int = 5
    fine_step_s: float = 0.11
    continuous_stop_word: str = ""
    continuous_walk_timeout_s: float = 15.0
    turn_landmark_word: str = ""
    post_landmark_turn_px: int = 0
    turn_landmark_timeout_s: float = 2.0
    telescope_pre_turn_steps: int = 0
    telescope_pre_turn_step_s: float = 0.09
    telescope_pre_turn_pause_s: float = 0.08
    telescope_turn_step_px: int = 0
    telescope_turn_max_px: int = 0
    telescope_turn_stages_px: tuple[int, ...] = ()
    telescope_move_after_first_stage: bool = False
    telescope_turn_timeout_s: float = 2.0
    
    
    telescope_failure_timeout_s: float = 0.0
    telescope_post_turn_walk_s: float = 0.0
    telescope_recovery_side: str = ""
    telescope_recovery_step_s: float = 0.08
    telescope_recovery_scan_px: tuple[int, ...] = ()
    climb_overshoot_recovery: bool = False
    climb_recovery_back_steps: int = 2
    climb_recovery_side: str = "d"
    climb_recovery_side_steps: int = 2
    climb_recovery_step_s: float = 0.09
    interact_on_arrival: bool = False
    narrow_alchemy_prompt: bool = False
    select_lower_prompt_on_stack: bool = False


class GloryTowerRouteTask(DailyTask):
    '两个每日任务的共享状态机；子类只提供 route、taskid 和 name。'

    route: TowerRouteSpec | None = None

    ROI_ALL = (0.0, 0.0, 1.0, 1.0)
    HUD_KEYWORDS = ("抓拍", "好友", "切换", "输入", "高处")

    def _navigate_to_spawn(self) -> bool:
        '烹饪和制药共用全地图具名传送。'
        stable = 0

        def arrived(frame) -> bool:
            nonlocal stable
            text = rec.ocr_text(frame, self.ROI_ALL)
            landmark = "渡石" in text or "荣耀塔" in text
            ready = landmark and self._world_hud_ready_from_text(text)
            stable = stable + 1 if ready else 0
            return stable >= 2

        self.ctx.log(f"{self.name}:调用全地图传送点“荣耀塔”")
        return teleport_to(
            self.ctx, "荣耀塔", timeout=45.0,
            arrival_predicate=arrived,
        )

    @classmethod
    def _world_hud_ready_from_text(cls, text: str) -> bool:
        hits = sum(1 for word in cls.HUD_KEYWORDS if word in text)
        map_bottom = (("切换地图" in text or "缩放地图" in text) and
                      ("返回" in text or "返回所在处" in text))
        return hits >= 2 and not map_bottom

    @classmethod
    def _world_hud_ready(cls, frame) -> bool:
        '制作完成后的通用 HUD 正向门，供制药和烹饪共同退出页面。'
        if frame is None:
            return False
        return cls._world_hud_ready_from_text(rec.ocr_text(frame, cls.ROI_ALL))

    def _target_in_frame(self, frame) -> bool:
        spec = self.route
        if not spec:
            return False
        if spec.narrow_alchemy_prompt:
            return bool(rec.alchemy_interaction_state(frame, spec.target_word)["found"])
        prompt = self._interaction_text(frame)
        return self._target_prompt_actionable(frame, prompt)

    def _target_prompt_actionable(self, frame, prompt: str) -> bool:
        '目标文字是否属于真正可交互的按钮，而不是 NPC 自动说话。'
        spec = self.route
        if not spec or spec.target_word not in prompt:
            return False
        if spec.narrow_alchemy_prompt:
            return bool(rec.alchemy_interaction_state(frame, spec.target_word)["found"])
        return True

    def _interaction_text(self, frame) -> str:
        '路线目标提示文本；制作台可启用更宽的双交互识别范围。'
        spec = self.route
        if spec and spec.select_lower_prompt_on_stack:
            return rec.crafting_interaction_state(frame, spec.target_word)["text"]
        if spec and spec.narrow_alchemy_prompt:
            return rec.alchemy_interaction_text(frame)
        return rec.center_interaction_text(frame)

    def _prepare_arrival_interaction(self) -> bool:
        '若制作台位于人物交互项下方，停止后向下滚一格选中制作台。'
        spec = self.route
        if not spec or not spec.select_lower_prompt_on_stack:
            return True
        ctx = self.ctx
        frame = getattr(self, "_arrival_frame", None)
        self._arrival_frame = None
        if frame is None:
            frame = ctx.grab()
        if frame is None:
            ctx.log(f"{self.name}:按 F 前无法截图确认制作台交互项")
            return False
        state = rec.crafting_interaction_state(frame, spec.target_word)
        if not state["found"]:
            ctx.log(f"{self.name}:停止后未再次识别到“{spec.target_word}”，不盲按 F")
            dev_log(f"[glory route] {self.task_id} 按 F 前制作台提示消失 "
                    f"target={spec.target_word!r} text={state['text']!r}")
            return False
        if not state["stacked_above"]:
            return True

        ctx.log(f"{self.name}:{spec.target_word}上方存在人物对话 → 滚轮向下一格选择制作台")
        dev_log(f"[glory route] {self.task_id} 双交互提示，{spec.target_word}位于下方 "
                f"point={state['target_pt']} text={state['text']!r}")
        if not ctx.scroll(-1, state["target_pt"]):
            return False
        ctx.sleep(0.25)
        after = ctx.grab()
        if (after is None
                or not rec.crafting_interaction_state(after, spec.target_word)["found"]):
            ctx.log(f"{self.name}:滚轮后{spec.target_word}提示消失，不按 F")
            return False
        return True

    @staticmethod
    def _is_telescope_prompt(prompt: str) -> bool:
        'OCR 可能漏掉“使用”二字，只要完整识别到核心词“望远镜”就触发第二次转向。'
        return "望远镜" in prompt

    def _confirm_target(self, first_frame=None) -> bool:
        '目标一旦出现就原地复查，绝不在复查期间继续 W。'
        ctx = self.ctx
        hits = 0
        frame = first_frame
        for index in range(3):
            if frame is None:
                frame = ctx.grab()
            if frame is not None and self._target_in_frame(frame):
                hits += 1
                if hits >= 2:
                    return True
            else:
                hits = 0
            if index < 2:
                ctx.sleep(0.14)
                frame = None
        return False

    def _scan_target(self, timeout: float) -> bool:
        ctx = self.ctx
        end = ctx.logical_time() + max(0.0, timeout)
        while ctx.logical_time() < end and not ctx.should_stop():
            frame = ctx.grab()
            if frame is not None and self._target_in_frame(frame):
                return self._confirm_target(frame)
            ctx.sleep(0.16)
        return False

    def _scan_target_once(self, timeout: float) -> bool:
        '恢复走位使用的单帧目标闸门。'
        ctx = self.ctx
        spec = self.route
        end = ctx.logical_time() + max(0.0, timeout)
        while ctx.logical_time() < end and not ctx.should_stop():
            frame = ctx.grab()
            if frame is None:
                ctx.sleep(0.08)
                continue
            if spec and spec.narrow_alchemy_prompt:
                interaction = rec.alchemy_interaction_state(
                    frame, spec.target_word, wide=True)
                prompt = interaction["text"]
                found = bool(interaction["found"])
            else:
                prompt = self._interaction_text(frame)
                found = self._target_prompt_actionable(frame, prompt)
            if found:
                self._arrival_frame = frame
                if spec and spec.narrow_alchemy_prompt:
                    dev_log(f"[glory route] {self.task_id} W释放后宽区兜底命中 "
                            f"target={spec.target_word!r} prompt={prompt!r}")
                return True
            ctx.sleep(0.08)
        return False

    def _turn_from_spawn(self) -> bool:
        '按录像方向分块转镜头；每块后都允许目标识别打断，不以转动耗时判完成。'
        ctx = self.ctx
        spec = self.route
        if spec is None:
            return False
        remaining = abs(int(spec.turn_total_px))
        sign = 1 if spec.turn_total_px >= 0 else -1
        while remaining > 0:
            step = min(300, remaining) * sign
            if not ctx.drag_camera(step, steps=max(8, abs(step) // 25)):
                return False
            remaining -= abs(step)
            ctx.sleep(0.10)
            frame = ctx.grab()
            
            if (not spec.turn_landmark_word and frame is not None
                    and self._target_in_frame(frame)):
                return self._confirm_target(frame)
        dev_log(f"[glory route] {self.task_id} 初始镜头转动完成 dx={spec.turn_total_px}")
        if spec.turn_landmark_word:
            return self._confirm_turn_landmark_then_continue(spec)
        return True

    def _confirm_turn_landmark_then_continue(self, spec: TowerRouteSpec) -> bool:
        '初始转角完成后以新帧确认地标，命中后再执行路线专属附加转角。'
        ctx = self.ctx
        end = ctx.logical_time() + max(0.0, spec.turn_landmark_timeout_s)
        while ctx.logical_time() < end and not ctx.should_stop():
            frame = ctx.grab()
            text = rec.ocr_text(frame, self.ROI_ALL) if frame is not None else ""
            if spec.turn_landmark_word in text:
                ctx.log(f"{self.name}:识别到“{spec.turn_landmark_word}”，继续偏转视角"
                        f" {spec.post_landmark_turn_px:+d}px")
                if not spec.post_landmark_turn_px:
                    return True
                ok = ctx.drag_camera(
                    spec.post_landmark_turn_px,
                    steps=max(8, abs(spec.post_landmark_turn_px) // 25),
                )
                if ok:
                    dev_log(f"[glory route] {self.task_id} 地标后视角偏转完成 "
                            f"word={spec.turn_landmark_word!r} "
                            f"dx={spec.post_landmark_turn_px}")
                return bool(ok)
            ctx.sleep(0.14)
        ctx.log(f"{self.name}:初始视角偏转后未识别到“{spec.turn_landmark_word}”，停止路线")
        return False

    def _recover_from_telescope(self) -> bool:
        '望远镜是两侧共有的边缘地标：只做一次退回平台内侧的小纠偏，不把它当到达。'
        ctx = self.ctx
        spec = self.route
        if spec is None:
            return False
        ctx.log(f"{self.name}:识别到望远镜边缘地标，退回平台内侧纠偏")
        if not ctx.press("s", hold_s=0.18):
            return False
        return ctx.press(spec.recovery_side, hold_s=0.08)

    def _recover_climb_overshoot(self, stop_word: str, *, release_climb: bool) -> bool:
        '制作台第二阶段纠偏；只有画面出现 C 时才先按 C 脱离攀爬。'
        ctx = self.ctx
        spec = self.route
        if spec is None:
            return False
        if release_climb:
            ctx.log(f"{self.name}:第二阶段识别到 C，先脱离攀爬再开始纠偏")
            dev_log(f"[glory route] {self.task_id} C 触发恢复开始，W 已释放")
            if not ctx.press("c"):
                return False
            ctx.sleep(0.20)
            if self._scan_target_once(0.55):
                ctx.log(f"{self.name}:按 C 脱离攀爬后识别到“{stop_word}”")
                dev_log(f"[glory route] {self.task_id} 爬墙恢复命中 phase=C")
                return True
        else:
            ctx.log(f"{self.name}:第二阶段达到 3 秒且未出现 C，跳过 C 直接碎步纠偏")
            dev_log(f"[glory route] {self.task_id} 3 秒超时恢复开始，"
                    "c_visible=False，跳过 C，W 已释放")

        step_s = max(0.05, float(spec.climb_recovery_step_s))
        back_steps = max(0, int(spec.climb_recovery_back_steps))
        for index in range(back_steps):
            if not ctx.press("s", hold_s=step_s):
                return False
            dev_log(f"[glory route] {self.task_id} 爬墙恢复 S 碎步 "
                    f"step={index + 1}/{back_steps} hold={step_s:.2f}s")
            ctx.sleep(0.06)
            if self._scan_target_once(0.55):
                ctx.log(f"{self.name}:后退第 {index + 1} 步后识别到“{stop_word}”")
                dev_log(f"[glory route] {self.task_id} 爬墙恢复命中 "
                        f"phase=S step={index + 1}/{back_steps}")
                return True

        side = spec.climb_recovery_side.strip().lower()
        side_steps = max(0, int(spec.climb_recovery_side_steps))
        if side not in ("a", "d") or side_steps <= 0:
            return False
        for index in range(side_steps):
            if not ctx.press(side, hold_s=step_s):
                return False
            dev_log(f"[glory route] {self.task_id} 爬墙恢复 {side.upper()} 碎步 "
                    f"step={index + 1}/{side_steps} hold={step_s:.2f}s")
            ctx.sleep(0.06)
            if self._scan_target_once(0.55):
                ctx.log(f"{self.name}:{side.upper()} 第 {index + 1} 步后识别到“{stop_word}”")
                dev_log(f"[glory route] {self.task_id} 爬墙恢复命中 "
                        f"phase={side.upper()} step={index + 1}/{side_steps}")
                return True
        ctx.log(f"{self.name}:完成 C、S×{back_steps}、{side.upper()}×{side_steps} 纠偏后"
                f"仍未识别到“{stop_word}”，停止本任务")
        return False

    def _climb_stage_frame_state(self, frame, stop_word: str) -> tuple[str, str]:
        '制作台第二阶段的单一视觉优先级：C 高于制作台提示。'
        if frame is None:
            return "search", ""
        if rec.climb_key_visible(frame):
            return "climb", ""
        spec = self.route
        if spec and spec.narrow_alchemy_prompt:
            interaction = rec.alchemy_interaction_state(frame, stop_word)
            prompt = interaction["text"]
            actionable = bool(interaction["found"])
        else:
            prompt = self._interaction_text(frame)
            actionable = stop_word in prompt
        if actionable:
            self._arrival_frame = frame
            return "target", prompt
        if stop_word in prompt:
            dev_log(f"[glory route] {self.task_id} {stop_word}文字候选无 F 键帽，"
                    f"继续第二阶段 prompt={prompt!r}")
        return "search", prompt

    def _run_climb_aware_second_stage(
            self, stop_word: str, stages: tuple[int, ...],
            movement_timeout_s: float, failure_timeout_s: float) -> bool:
        '制作台通用的攀爬感知第二阶段。'
        ctx = self.ctx
        max_turn = sum(abs(dx) for dx in stages)
        turned = 0

        
        entry_frame = ctx.grab_nowait()
        entry_state, entry_prompt = self._climb_stage_frame_state(entry_frame, stop_word)
        dev_log(f"[glory route] {self.task_id} {stop_word}第二阶段入口 "
                f"state={entry_state} prompt={entry_prompt!r}")
        if entry_state == "climb":
            return self._recover_climb_overshoot(stop_word, release_climb=True)
        if entry_state == "target":
            ctx.log(f"{self.name}:第二阶段入口已识别到“{stop_word}”，立即交互")
            return True

        
        first_dx = stages[0]
        first_remaining = abs(first_dx)
        first_sign = 1 if first_dx > 0 else -1
        while first_remaining > 0 and not ctx.should_stop():
            if ctx.paused:
                time.sleep(0.08)
                continue
            if not ctx.action_ready():
                return False
            chunk = min(100, first_remaining) * first_sign
            if not ctx.drag_camera(chunk, steps=max(3, min(5, abs(chunk) // 20))):
                return False
            first_remaining -= abs(chunk)
            turned += abs(chunk)
            time.sleep(0.04)
            frame = ctx.grab_nowait()
            state, prompt = self._climb_stage_frame_state(frame, stop_word)
            dev_log(f"[glory route] {self.task_id} {stop_word}第二阶段首层 "
                    f"turned={turned}/{max_turn}px state={state} prompt={prompt!r}")
            if state == "climb":
                return self._recover_climb_overshoot(stop_word, release_climb=True)
            if state == "target":
                ctx.log(f"{self.name}:首层转向识别到“{stop_word}”，立即停止并交互")
                return True

        if len(stages) <= 1:
            return self._recover_climb_overshoot(stop_word, release_climb=False)

        phase_started_at: float | None = None
        state = "search"
        prompt = ""
        stage_index = 1
        stage_remaining = abs(stages[stage_index])
        stage_sign = 1 if stages[stage_index] > 0 else -1

        
        with ctx.hold("w") as held:
            if not held:
                return False
            phase_started_at = ctx.logical_time()
            dev_log(f"[glory route] {self.task_id} {stop_word}第二阶段 W 开始 "
                    f"walk_limit={movement_timeout_s:.2f}s failure_limit={failure_timeout_s:.2f}s")
            while not ctx.should_stop():
                if ctx.paused:
                    time.sleep(0.08)
                    continue
                if not ctx.action_ready():
                    return False
                elapsed = ctx.logical_time() - phase_started_at
                if elapsed >= movement_timeout_s:
                    break

                frame = ctx.grab_nowait()
                state, prompt = self._climb_stage_frame_state(frame, stop_word)
                if state != "search":
                    break

                if stage_index >= len(stages):
                    time.sleep(0.05)
                    continue
                chunk = min(100, stage_remaining) * stage_sign
                if not ctx.drag_camera(chunk, steps=max(3, min(5, abs(chunk) // 20))):
                    return False
                stage_remaining -= abs(chunk)
                turned += abs(chunk)
                dev_log(f"[glory route] {self.task_id} {stop_word}第二阶段 W边走边转 "
                        f"elapsed={elapsed:.2f}s stage={stage_index + 1}/{len(stages)} "
                        f"turned={turned}/{max_turn}px dx={chunk} prompt={prompt!r}")
                if stage_remaining <= 0:
                    stage_index += 1
                    if stage_index < len(stages):
                        stage_remaining = abs(stages[stage_index])
                        stage_sign = 1 if stages[stage_index] > 0 else -1
                time.sleep(0.04)

        
        elapsed = max(0.0, ctx.logical_time() - phase_started_at)
        dev_log(f"[glory route] {self.task_id} {stop_word}第二阶段 W 已释放 "
                f"elapsed={elapsed:.2f}s state={state} turned={turned}/{max_turn}px")
        if state == "climb":
            return self._recover_climb_overshoot(stop_word, release_climb=True)
        if state == "target":
            ctx.log(f"{self.name}:移动中识别到“{stop_word}”，已释放 W 并立即交互")
            return True

        
        while not ctx.should_stop() and ctx.action_ready():
            frame = ctx.grab_nowait()
            state, prompt = self._climb_stage_frame_state(frame, stop_word)
            elapsed = max(0.0, ctx.logical_time() - phase_started_at)
            if state == "climb":
                dev_log(f"[glory route] {self.task_id} W释放后识别到C elapsed={elapsed:.2f}s")
                return self._recover_climb_overshoot(stop_word, release_climb=True)
            if state == "target":
                ctx.log(f"{self.name}:W 释放后识别到“{stop_word}”，立即交互")
                return True
            if elapsed >= failure_timeout_s:
                break
            time.sleep(0.05)

        if ctx.should_stop() or not ctx.action_ready():
            return False
        dev_log(f"[glory route] {self.task_id} {stop_word}第二阶段达到3秒且无C，直接碎步恢复")
        return self._recover_climb_overshoot(stop_word, release_climb=False)

    def _turn_from_telescope_until_target(self, stop_word: str) -> bool:
        '命中望远镜后执行可选碎步，再分层转镜头；命中目标立即停止。'
        ctx = self.ctx
        spec = self.route
        if spec is None:
            return False

        
        stages = tuple(int(dx) for dx in spec.telescope_turn_stages_px if int(dx))
        if not stages and spec.telescope_turn_step_px and spec.telescope_turn_max_px:
            step = int(spec.telescope_turn_step_px)
            remaining = abs(int(spec.telescope_turn_max_px))
            sign = 1 if step > 0 else -1
            generated: list[int] = []
            while remaining > 0:
                dx = min(abs(step), remaining) * sign
                generated.append(dx)
                remaining -= abs(dx)
            stages = tuple(generated)
        if not stages:
            ctx.log(f"{self.name}:识别到望远镜，但未配置望远镜后镜头纠偏")
            return False
        max_turn = sum(abs(dx) for dx in stages)
        movement_timeout_s = max(0.1, float(spec.telescope_turn_timeout_s))
        failure_timeout_s = max(
            movement_timeout_s,
            float(spec.telescope_failure_timeout_s or movement_timeout_s),
        )
        pre_turn_steps = max(0, int(spec.telescope_pre_turn_steps))
        pre_turn_step_s = max(0.05, float(spec.telescope_pre_turn_step_s))
        pre_turn_pause_s = max(0.0, float(spec.telescope_pre_turn_pause_s))
        turned = 0
        stage_index = 0
        delayed_movement_budget = bool(
            spec.telescope_move_after_first_stage and len(stages) > 1)
        if delayed_movement_budget:
            timing = (f"首层原地转向不计时；W 硬上限 {movement_timeout_s:.1f} 秒，"
                      f"从 W 按下开始 {failure_timeout_s:.1f} 秒才允许判定失败，"
                      "后三层边走边转，W 结束后的剩余时间只原地识别")
        else:
            timing = f"全程原地转向，时间上限 {movement_timeout_s:.1f} 秒"
        pre_step_plan = (f"转向前先前进 {pre_turn_steps} 个小碎步；"
                         if pre_turn_steps else "")
        ctx.log(
            f"{self.name}:识别到“使用望远镜”，已立即停止移动并进入第二阶段；"
            f"{pre_step_plan}{timing}，最多转向 {max_turn}px 寻找“{stop_word}”"
        )

        
        
        for step_index in range(pre_turn_steps):
            if ctx.should_stop() or not ctx.action_ready():
                ctx.log(f"{self.name}:望远镜后碎步输入条件失效，已停止路线")
                return False
            if not ctx.press("w", hold_s=pre_turn_step_s):
                return False
            dev_log(f"[glory route] {self.task_id} 望远镜后转向前 W 小碎步 "
                    f"step={step_index + 1}/{pre_turn_steps} hold={pre_turn_step_s:.2f}s")
            if pre_turn_pause_s > 0:
                ctx.sleep(pre_turn_pause_s)

        if spec.climb_overshoot_recovery:
            return self._run_climb_aware_second_stage(
                stop_word, stages, movement_timeout_s, failure_timeout_s)

        
        frame = ctx.grab_nowait()
        prompt = self._interaction_text(frame) if frame is not None else ""
        if stop_word in prompt:
            ctx.log(f"{self.name}:转动前已识别到“{stop_word}”，立即停止")
            dev_log(f"[glory route] {self.task_id} 望远镜后转向命中 "
                    f"turned=0px prompt={prompt!r}")
            return True

        
        
        
        phase_started_at: float | None = None
        walk_deadline: float | None = None
        failure_deadline: float | None = None
        if not delayed_movement_budget:
            phase_started_at = ctx.logical_time()
            walk_deadline = phase_started_at + movement_timeout_s
            failure_deadline = phase_started_at + failure_timeout_s
        detected = False
        climb_detected = False
        
        
        with ExitStack() as movement_stack:
            w_held = False
            while (stage_index < len(stages)
                   and (walk_deadline is None or ctx.logical_time() < walk_deadline)
                   and not ctx.should_stop()):
                if ctx.paused:
                    time.sleep(0.08)
                    continue
                if not ctx.foreground():
                    ctx.log(f"{self.name}:第二阶段游戏失去前台，已停止转向")
                    return False
                if not ctx.action_ready():
                    ctx.log(f"{self.name}:第二阶段输入权限失效，已停止转向")
                    return False

                stage_dx = stages[stage_index]
                stage_remaining = abs(stage_dx)
                stage_sign = 1 if stage_dx > 0 else -1
                move_with_w = bool(
                    spec.telescope_move_after_first_stage and stage_index > 0)
                if move_with_w and not w_held:
                    held = movement_stack.enter_context(ctx.hold("w"))
                    if not held:
                        return False
                    w_held = True
                    phase_started_at = ctx.logical_time()
                    walk_deadline = phase_started_at + movement_timeout_s
                    failure_deadline = phase_started_at + failure_timeout_s
                    dev_log(f"[glory route] {self.task_id} 第二阶段持续 W 开始，"
                            f"stage={stage_index + 1}/{len(stages)} "
                            f"walk_budget={movement_timeout_s:.2f}s "
                            f"failure_budget={failure_timeout_s:.2f}s（均从本次 W 按下开始）")

                
                
                while (stage_remaining > 0
                       and (walk_deadline is None or ctx.logical_time() < walk_deadline)
                       and not ctx.should_stop()):
                    if not ctx.action_ready():
                        break
                    chunk = min(100, stage_remaining) * stage_sign
                    drag_steps = max(3, min(5, abs(chunk) // 20))
                    if not ctx.drag_camera(chunk, steps=drag_steps):
                        return False
                    stage_remaining -= abs(chunk)
                    turned += abs(chunk)
                    time.sleep(0.04)

                    
                    frame = ctx.grab_nowait()
                    prompt = self._interaction_text(frame) if frame is not None else ""
                    mode = "W边走边转" if move_with_w else "原地转向"
                    dev_log(f"[glory route] {self.task_id} 望远镜后{mode} "
                            f"stage={stage_index + 1}/{len(stages)} "
                            f"stage_progress={abs(stage_dx) - stage_remaining}/{abs(stage_dx)}px "
                            f"turned={turned}/{max_turn}px dx={chunk} prompt={prompt!r}")
                    if stop_word in prompt:
                        detected = True
                        break
                    if (spec.climb_overshoot_recovery and frame is not None
                            and rec.climb_key_visible(frame)):
                        climb_detected = True
                        climb_elapsed = (0.0 if phase_started_at is None else
                                         ctx.logical_time() - phase_started_at)
                        dev_log(f"[glory route] {self.task_id} 第二阶段移动中识别到 C，"
                                f"elapsed={climb_elapsed:.2f}s")
                        break

                if detected:
                    ctx.log(f"{self.name}:第二阶段识别到“{stop_word}”，已停止 W 和剩余转向")
                    dev_log(f"[glory route] {self.task_id} 望远镜后转向命中 "
                            f"turned={turned}px prompt={prompt!r}")
                    return True
                if climb_detected:
                    ctx.log(f"{self.name}:第二阶段移动中识别到 C，已立即释放 W")
                    break
                if stage_remaining > 0:
                    break
                stage_index += 1
                if not ctx.paused and not ctx.action_ready():
                    ctx.log(f"{self.name}:第二阶段输入权限失效，已停止转向")
                    return False

            
            
            if (delayed_movement_budget and stage_index >= len(stages) and w_held
                    and walk_deadline is not None):
                while (ctx.logical_time() < walk_deadline and not ctx.should_stop()
                       and ctx.action_ready()):
                    frame = ctx.grab_nowait()
                    prompt = self._interaction_text(frame) if frame is not None else ""
                    if stop_word in prompt:
                        ctx.log(f"{self.name}:持续 W 剩余时间内识别到“{stop_word}”，已立即抬键")
                        dev_log(f"[glory route] {self.task_id} 第二阶段持续 W 命中 "
                                f"walk_budget={movement_timeout_s:.2f}s prompt={prompt!r}")
                        return True
                    if (spec.climb_overshoot_recovery and frame is not None
                            and rec.climb_key_visible(frame)):
                        climb_detected = True
                        ctx.log(f"{self.name}:持续 W 时识别到 C，已立即释放 W")
                        break
                    time.sleep(0.05)
                dev_log(f"[glory route] {self.task_id} 第二阶段持续 W 预算结束 "
                        f"walk_budget={movement_timeout_s:.2f}s turned={turned}/{max_turn}px")

            
            
            post_walk_s = max(0.0, float(spec.telescope_post_turn_walk_s))
            if stage_index >= len(stages) and post_walk_s > 0 and ctx.action_ready():
                if not w_held:
                    held = movement_stack.enter_context(ctx.hold("w"))
                    if not held:
                        return False
                    w_held = True
                    if phase_started_at is None:
                        phase_started_at = ctx.logical_time()
                        walk_deadline = phase_started_at + movement_timeout_s
                        failure_deadline = phase_started_at + failure_timeout_s
                post_deadline = ctx.logical_time() + post_walk_s
                if walk_deadline is not None:
                    post_deadline = min(post_deadline, walk_deadline)
                while (ctx.logical_time() < post_deadline and not ctx.should_stop()
                       and ctx.action_ready()):
                    frame = ctx.grab_nowait()
                    prompt = self._interaction_text(frame) if frame is not None else ""
                    if stop_word in prompt:
                        ctx.log(f"{self.name}:追加前进时识别到“{stop_word}”，已立即抬起 W")
                        dev_log(f"[glory route] {self.task_id} 第二阶段追加 W 命中 "
                                f"budget={post_walk_s:.2f}s prompt={prompt!r}")
                        return True
                    time.sleep(0.05)
                dev_log(f"[glory route] {self.task_id} 第二阶段追加 W 完成 "
                        f"budget={post_walk_s:.2f}s")

        movement_elapsed = (0.0 if phase_started_at is None else
                            max(0.0, ctx.logical_time() - phase_started_at))
        ctx.log(
            f"{self.name}:望远镜后分层转向 {turned}/{max_turn}px，"
            f"W 已在 {movement_elapsed:.1f}/{movement_timeout_s:.1f} 秒内释放，"
            f"仍未识别到“{stop_word}”"
        )

        
        
        if ctx.should_stop() or not ctx.action_ready():
            return False

        
        
        if spec.climb_overshoot_recovery and climb_detected:
            return self._recover_climb_overshoot(stop_word, release_climb=True)

        if (spec.climb_overshoot_recovery and delayed_movement_budget
                and phase_started_at is not None and failure_deadline is not None):
            wait_attempt = 0
            wait_climb_detected = False
            while (ctx.logical_time() < failure_deadline and not ctx.should_stop()
                   and ctx.action_ready()):
                frame = ctx.grab_nowait()
                prompt = self._interaction_text(frame) if frame is not None else ""
                wait_attempt += 1
                if stop_word in prompt:
                    ctx.log(f"{self.name}:W 释放后的失败等待期识别到“{stop_word}”")
                    dev_log(f"[glory route] {self.task_id} 第二阶段失败等待命中 "
                            f"attempt={wait_attempt} prompt={prompt!r}")
                    return True
                if frame is not None and rec.climb_key_visible(frame):
                    wait_climb_detected = True
                    dev_log(f"[glory route] {self.task_id} W 释放后识别到 C，"
                            f"elapsed={ctx.logical_time() - phase_started_at:.2f}s")
                    break
                time.sleep(0.06)

            
            failure_elapsed = min(
                failure_timeout_s,
                max(0.0, ctx.logical_time() - phase_started_at),
            )
            climb_frame = ctx.grab_nowait()
            climb_visible = bool(
                climb_frame is not None and rec.climb_key_visible(climb_frame))
            dev_log(f"[glory route] {self.task_id} 第二阶段超时攀爬检查 "
                    f"walk_elapsed={movement_elapsed:.2f}s "
                    f"failure_elapsed={failure_elapsed:.2f}s "
                    f"c_visible={climb_visible or wait_climb_detected}")
            
            c_detected = bool(wait_climb_detected or climb_visible)
            if c_detected or failure_elapsed >= failure_timeout_s - 0.05:
                return self._recover_climb_overshoot(
                    stop_word, release_climb=c_detected)

        recovery_side = spec.telescope_recovery_side.strip().lower()
        recovery_step_s = max(0.05, float(spec.telescope_recovery_step_s))
        if recovery_side:
            if not ctx.press(recovery_side, hold_s=recovery_step_s):
                return False
            dev_log(f"[glory route] {self.task_id} 第二阶段未命中，"
                    f"仅补一次 {recovery_side.upper()}={recovery_step_s:.2f}s")
            time.sleep(0.08)

        frame = ctx.grab_nowait()
        prompt = self._interaction_text(frame) if frame is not None else ""
        if stop_word in prompt:
            ctx.log(f"{self.name}:横向小碎步后识别到“{stop_word}”")
            return True

        for scan_index, dx in enumerate(spec.telescope_recovery_scan_px, start=1):
            if ctx.should_stop() or not ctx.action_ready():
                return False
            if not ctx.drag_camera(dx, steps=max(3, min(6, abs(dx) // 20))):
                return False
            time.sleep(0.05)
            frame = ctx.grab_nowait()
            prompt = self._interaction_text(frame) if frame is not None else ""
            dev_log(f"[glory route] {self.task_id} 第二阶段失败恢复原地回扫 "
                    f"scan={scan_index}/{len(spec.telescope_recovery_scan_px)} "
                    f"dx={dx} prompt={prompt!r}")
            if stop_word in prompt:
                ctx.log(f"{self.name}:失败恢复回扫识别到“{stop_word}”")
                return True
        return False

    def _walk_continuously_until(self, stop_word: str, timeout_s: float) -> bool:
        '持续按住 W 并识别中央提示；命中后先抬键，再返回成功。'
        ctx = self.ctx
        start = ctx.logical_time()
        detected = False
        telescope_detected = False
        last_prompt = None
        ctx.log(f"{self.name}:视角调整完成，持续按住 W，识别到“{stop_word}”立即停止")

        while ctx.logical_time() - start < timeout_s and not ctx.should_stop():
            if ctx.paused:
                time.sleep(0.08)
                continue
            if not ctx.foreground():
                ctx.log(f"{self.name}:持续行进时游戏失去前台，已抬起 W 并停止路线")
                return False

            
            frame = ctx.grab_nowait()
            
            
            prompt = rec.telescope_interaction_text(frame) if frame is not None else ""
            if stop_word in prompt:
                ctx.log(f"{self.name}:识别到“{stop_word}”，停止行进")
                return True
            if self._is_telescope_prompt(prompt):
                return self._turn_from_telescope_until_target(stop_word)

            with ctx.hold("w") as held:
                if not held:
                    return False
                while (ctx.logical_time() - start < timeout_s
                       and not ctx.should_stop()):
                    if not ctx.action_ready():
                        break
                    frame = ctx.grab_nowait()
                    if frame is None:
                        time.sleep(0.03)
                        continue
                    prompt = rec.telescope_interaction_text(frame)
                    if prompt != last_prompt:
                        dev_log(f"[glory route] {self.task_id} 持续 W prompt={prompt!r}")
                        last_prompt = prompt
                    if stop_word in prompt:
                        detected = True
                        break
                    if self._is_telescope_prompt(prompt):
                        telescope_detected = True
                        break
                    time.sleep(0.08)

            
            if detected:
                ctx.log(f"{self.name}:识别到“{stop_word}”，已立即抬起 W")
                return True
            if telescope_detected:
                ctx.log(f"{self.name}:识别到“望远镜”，已立即抬起 W，再进入第二阶段转向")
                dev_log(f"[glory route] {self.task_id} 望远镜命中，第一阶段 W 已释放")
                return self._turn_from_telescope_until_target(stop_word)
            if not ctx.paused and not ctx.action_ready():
                ctx.log(f"{self.name}:持续行进输入权限失效，已停止路线")
                return False

        if ctx.should_stop():
            return False
        ctx.log(f"{self.name}:持续前进 {timeout_s:.0f} 秒仍未识别到“{stop_word}”，已停止")
        return False

    def _walk_to_target(self) -> bool:
        ctx = self.ctx
        spec = self.route
        if spec is None:
            return False
        if spec.continuous_stop_word:
            return self._walk_continuously_until(
                spec.continuous_stop_word, spec.continuous_walk_timeout_s)
        if self._scan_target(0.35):
            return True

        stuck = 0
        telescope_recovered = False
        near = False
        for index in range(spec.coarse_pulses):
            if ctx.should_stop():
                return False
            before = ctx.grab()
            if before is not None and self._target_in_frame(before):
                return self._confirm_target(before)
            if not ctx.press("w", hold_s=spec.coarse_step_s):
                return False
            ctx.sleep(0.10)
            after = ctx.grab()
            if after is None:
                continue
            prompt = self._interaction_text(after)
            if spec.target_word in prompt and self._confirm_target(after):
                return True
            if self._is_telescope_prompt(prompt) and not telescope_recovered:
                telescope_recovered = True
                if not self._recover_from_telescope():
                    return False
                continue

            motion = rec.scene_motion_score(before, after) if before is not None else 0.0
            stuck = stuck + 1 if motion < 0.18 else 0
            dev_log(f"[glory route] {self.task_id} 粗步 {index + 1}/{spec.coarse_pulses} "
                    f"motion={motion:.3f} prompt={prompt!r}")
            if stuck >= 2:
                
                turn = 90 if spec.turn_total_px >= 0 else -90
                ctx.log(f"{self.name}:连续短步未产生画面位移，小幅调整视角")
                if not ctx.drag_camera(turn, steps=8):
                    return False
                stuck = 0

            if index % 2 == 1 and spec.near_words:
                full_text = rec.ocr_text(after, self.ROI_ALL)
                if any(word in full_text for word in spec.near_words):
                    near = True
                    ctx.log(f"{self.name}:识别到制作台附近地标，切换为细步搜索")
                    break

        
        if self._scan_target(2.2 if near else 1.2):
            return True

        for index in range(spec.fine_pulses):
            if ctx.should_stop():
                return False
            if not ctx.press("w", hold_s=spec.fine_step_s):
                return False
            ctx.sleep(0.10)
            frame = ctx.grab()
            if frame is not None:
                prompt = self._interaction_text(frame)
                dev_log(f"[glory route] {self.task_id} 细步 {index + 1}/{spec.fine_pulses} "
                        f"prompt={prompt!r}")
                if spec.target_word in prompt and self._confirm_target(frame):
                    return True
                if self._is_telescope_prompt(prompt) and not telescope_recovered:
                    telescope_recovered = True
                    if not self._recover_from_telescope():
                        return False
            if self._scan_target(0.35):
                return True

        
        for dx in (-150, 300, -150):
            if not ctx.drag_camera(dx, steps=10):
                return False
            if self._scan_target(0.55):
                return True
        ctx.log(f"{self.name}:局部路线达到安全上限，未确认“{spec.target_word}”，停止移动")
        return False

    def run(self) -> str:
        ctx = self.ctx
        self._arrival_frame = None
        if self.route is None:
            ctx.log(f"{self.name}:缺少荣耀塔路线配置")
            return TaskResult.FAIL
        if not self._navigate_to_spawn():
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
        if not self._turn_from_spawn():
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
        if not self._walk_to_target():
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
        reached = self.route.continuous_stop_word or self.route.target_word
        if self.route.interact_on_arrival:
            
            if not self._prepare_arrival_interaction():
                return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
            
            ctx.sleep(0.12)
            dev_log(f"[glory route] {self.task_id} 到达交互点，准备按 F target={reached!r}")
            if not ctx.press("f", hold_s=0.08):
                ctx.log(f"{self.name}:已识别“{reached}”，但按 F 失败")
                return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
            ctx.log(f"{self.name}:已识别“{reached}”，停止转动和行进并按 F")
        elif self.route.continuous_stop_word:
            ctx.log(f"{self.name}:已识别“{reached}”并停止行进（本阶段不按 F）")
        else:
            ctx.log(f"{self.name}:已连续确认“{reached}”，到达制作台（本阶段不按 F）")
        return TaskResult.SUCCESS
