'固定时间轴自动战斗状态机。'
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import win32gui

from capture_broker import subscribe_capture
from combat.audio import RedCueAudioDetector
from combat.profile import CombatAction, CombatProfile, SecondarySequence
from combat.recognizer import (
    COMBAT_ROIS,
    STUN_COMBAT_ROIS,
)
from combat.vision import CombatVisionSnapshot, CombatVisionWorker
from winenv import find_game_hwnd, is_foreground
from runtime_guard import (
    SafeKeyScheduler,
    release_known_keys,
    safe_mouse_button,
)

class CombatBot:
    '执行用户配置的循环动作，并处理 X/Z 提示和大招流程。'

    FRAME_INTERVAL = 0.05
    COUNTER_REARM_FRAMES = 2
    COUNTER_RETRY_SECONDS = 0.18
    COUNTER_MAX_ATTEMPTS = 2
    COUNTER_HOLD_MS = 55
    ULTIMATE_REARM_FRAMES = 6
    ULTIMATE_READY_CONFIRM_FRAMES = 2
    ULTIMATE_COMBAT_GRACE_SECONDS = 0.18
    ULTIMATE_MIN_REARM_SECONDS = 20.0
    ULTIMATE_RELEASE_CONFIRM_FRAMES = 2
    ULTIMATE_RELEASE_CONFIRM_TIMEOUT = 1.2
    ULTIMATE_FAILED_RETRY_SECONDS = 0.8
    HERO_COLOR_DISTANCE = 0.24
    RED_AUDIO_CONFIRM_SECONDS = 0.30
    DODGE_ATTACK_DELAY_SECONDS = 0.08
    SECONDARY_ULTIMATE_SETTLE_SECONDS = 0.35
    SWITCH_VERIFY_SAMPLES = 8
    SWITCH_STABLE_READS = 2
    SWITCH_SAME_VOTES = 3
    SWITCH_SETTLE_SKIP_FRAMES = 1
    SWITCH_MAX_ATTEMPTS = 2
    SWITCH_RETRY_DELAY_SECONDS = 3.0
    SWITCH_VISUAL_WAIT_MS = 180
    SWITCH_ICON_CHANGE_SIMILARITY = 0.70
    SWITCH_ICON_SAME_SIMILARITY = 0.78
    BATTLE_RESULT_CHECK_INTERVAL = 1.0
    CAPTURE_ROIS = COMBAT_ROIS

    def __init__(
        self,
        profile: CombatProfile,
        ultimate_mode: str,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.profile = profile
        self.ultimate_mode = (
            ultimate_mode
            if ultimate_mode in ("disabled", "immediate", "stunned")
            else "immediate"
        )
        self.log = log or (lambda _message: None)
        self._stop = threading.Event()
        self._hwnd = 0
        self._key_scheduler = SafeKeyScheduler(
            stop_check=self._stop.is_set,
            foreground_check=lambda: bool(
                self._hwnd and is_foreground(self._hwnd)
            ),
            log=self.log,
        )
        self._last_foreground: bool | None = None
        self._counter_armed = True
        self._counter_absent_frames = 0
        self._last_counter_key: str | None = None
        self._counter_attempts = 0
        self._counter_last_sent_at = 0.0
        self._next_dodge_at = 0.0
        self._switch_blocked_until = 0.0
        self._current_hero = profile.initial_hero
        self._hero_max_health: dict[str, int] = {}
        self._hero_ultimate_images: dict[str, object] = {}
        self._last_ultimate_signatures: dict[str, tuple[float, ...]] = {}
        self._ultimate_latched = {hero: False for hero in profile.hero_order}
        self._ultimate_absent_frames = {hero: 0 for hero in profile.hero_order}
        self._ultimate_ready_frames = {hero: 0 for hero in profile.hero_order}
        self._ultimate_last_sent_at = {hero: 0.0 for hero in profile.hero_order}
        self._ultimate_pending_since = {hero: 0.0 for hero in profile.hero_order}
        self._ultimate_pending_deadline = {
            hero: 0.0 for hero in profile.hero_order
        }
        self._ultimate_pending_consumed_frames = {
            hero: 0 for hero in profile.hero_order
        }
        self._ultimate_retry_not_before = {
            hero: 0.0 for hero in profile.hero_order
        }
        self._ultimate_blocked_until = {
            hero: 0.0 for hero in profile.hero_order
        }
        self._ultimate_sequence_running = False
        self._ultimate_combat_seen_at = 0.0
        self._secondary_due: dict[str, float] = {}
        self._skill_due: dict[str, float] = {}
        self._adaptive_followups: list[CombatAction] = []
        self._skill_segment_index = {
            skill.key: 0 for skill in profile.skill_cooldowns
        }
        self._initial_main_skills_pending = {
            skill.key for skill in profile.skill_cooldowns
        }
        self._monster_stun_active = False
        self._red_audio_detector = RedCueAudioDetector(log=self.log)
        self._red_visual_pending_until = 0.0
        self._red_visual_score = 0.0
        self._red_audio_best_score = 0.0
        self._red_audio_ready = False
        self._red_audio_match_sequence = 0
        self._battle_result: str | None = None
        self._vision: CombatVisionWorker | None = None
        self._last_vision_sequence = 0
        self._last_red_event_id = 0
        self._last_ultimate_vision_sequence = 0
        self._vision_error_logged = False
        self._capture_rois = (
            STUN_COMBAT_ROIS
            if self.ultimate_mode == "stunned"
            else COMBAT_ROIS
        )

    def stop(self) -> None:
        self._stop.set()
        if self._vision is not None:
            request_stop = getattr(self._vision, "request_stop", None)
            if request_stop is not None:
                request_stop()
            else:
                self._vision.stop()
        self._key_scheduler.release_all()
        release_known_keys(self.log)

    def run(self) -> bool:
        self._hwnd = find_game_hwnd(prefer_foreground=True)
        if not self._hwnd:
            self.log("未找到游戏窗口，自动战斗已停止")
            return False
        if not is_foreground(self._hwnd):
            self.log("游戏不在前台，自动战斗未启动")
            return False
        if not self.profile.has_any_action:
            self.log("战斗配置没有任何动作，请先打开配置文件填写流程")
            return False

        self.log(f"已加载战斗方案：{self.profile.name}；当前英雄：{self._current_hero}")
        self.log("冷却空档：不发送默认普攻，仅执行配置流程")
        self._red_audio_detector.start()

        rotation_index = 0
        started_at = time.monotonic()

        next_action_at = started_at + self.FRAME_INTERVAL
        self._secondary_due = {
            sequence.hero: started_at
            for sequence in self.profile.secondary_sequences
        }
        for sequence in self.profile.secondary_sequences:
            cooldown_text = ",".join(
                f"{skill.key.upper()}={skill.cooldown_ms / 1000.0:g}秒"
                for skill in sequence.skill_cooldowns
            )
            self.log(
                f"{sequence.hero}将在主C首套完成后立即切入；后续间隔"
                f" {sequence.switch_after_ms / 1000.0:g} 秒"
                + (f"（技能冷却：{cooldown_text}；按最大值计时）" if cooldown_text else "")
            )

        self._skill_due = {
            skill.key: started_at
            for skill in self.profile.skill_cooldowns
        }
        try:
            with subscribe_capture(
                self._hwnd, "combat", self._capture_rois, self.FRAME_INTERVAL
            ) as frames:
                with CombatVisionWorker(
                    frames,
                    interval=self.FRAME_INTERVAL,
                    ultimate_mode=self.ultimate_mode,
                    stop_check=self._stop.is_set,
                ) as vision:
                    self._vision = vision
                    if not vision.wait_until_ready(1.0):
                        error = vision.error
                        self.log(
                            "战斗视觉线程初始化失败"
                            + (f"：{error}" if error is not None else "")
                        )
                        return False
                    if self.profile.target_lock_on_start:
                        if not self._lock_target(vision):
                            if self._terminal_stop_requested():
                                return self._battle_result is not None
                            self.log("目标锁定未确认，继续执行战斗流程")
                    while not self._stop.is_set():
                        if not self._foreground_ready():
                            self._stop.wait(0.10)
                            continue
                        now = time.monotonic()
                        timeout = (
                            min(0.30, max(0.0, next_action_at - now))
                            if now < next_action_at
                            else 0.0
                        )
                        vision_status = self._consume_vision(
                            vision,
                            timeout=timeout,
                        )
                        if vision_status == "stop":
                            break
                        if vision_status == "reaction":
                            next_action_at = max(
                                next_action_at,
                                time.monotonic()
                                + self.profile.reaction_recovery_ms / 1000.0,
                            )
                            continue
                        if vision_status == "ultimate":

                            next_action_at = time.monotonic()
                            continue

                        now = time.monotonic()
                        if now < next_action_at:
                            continue
                        secondary = self._secondary_after_normal_combo(
                            now, rotation_index, next_action_at)
                        if secondary is not None:
                            if not self._run_secondary(secondary, vision):
                                if self._terminal_stop_requested():
                                    break
                                continue
                            next_action_at = time.monotonic()
                            continue

                        if self.profile.skill_cooldowns:
                            action = self._adaptive_action(now)
                        else:
                            if not self.profile.rotation:
                                continue
                            action = self.profile.rotation[rotation_index]
                        action_started_at = time.monotonic()
                        if not self._perform_action(action):
                            if self._terminal_stop_requested():
                                break
                            if self.profile.skill_cooldowns:
                                self._adaptive_followups.insert(0, action)
                            self.log("当前输入未发送，保留动作并继续等待")
                            next_action_at = (
                                time.monotonic() + self.FRAME_INTERVAL
                            )
                            continue
                        next_action_at = (
                            action_started_at
                            + (
                                action.wait_ms / 1000.0
                                if action.wait_ms > 0
                                else self.FRAME_INTERVAL
                            )
                        )
                        if not self.profile.skill_cooldowns:
                            rotation_index += 1
                            if rotation_index >= len(self.profile.rotation):
                                if not self.profile.loop:
                                    self.log("固定攻击流程已完成")
                                    return True
                                rotation_index = 0
            return self._battle_result is not None
        finally:
            self._vision = None
            self._red_audio_detector.stop()
            self._key_scheduler.close()
            release_known_keys(self.log)

    def _foreground_ready(self) -> bool:
        if not self._hwnd or not win32gui.IsWindow(self._hwnd):
            self._key_scheduler.release_all()
            if self._vision is not None:
                self._vision.set_active(False)
            if not self._stop.is_set():
                self.log("游戏窗口已关闭，自动战斗停止")
                self._stop.set()
            return False
        foreground = bool(self._hwnd and is_foreground(self._hwnd))
        if foreground != self._last_foreground:
            self._last_foreground = foreground
            self.log("游戏回到前台，自动战斗继续" if foreground else "游戏不在前台，自动战斗已暂停")
            if not foreground:
                self._clear_pending_red_flash()
        if self._vision is not None:
            self._vision.set_active(foreground)
        if not foreground:
            self._key_scheduler.release_all()
        return foreground

    def _terminal_stop_requested(self) -> bool:
        '判断是否命中自动战斗允许结束的运行时状态。'
        return self._stop.is_set() or self._battle_result is not None

    def _lock_target(self, vision) -> bool:
        '首帧确认后在游戏客户区中心发送中键锁定。'
        snapshot = vision.wait_for_update(
            self._last_vision_sequence,
            timeout=0.50,
        )
        if snapshot is None:
            self.log("未取得战斗首帧，中键锁定未执行")
            return False
        self._last_vision_sequence = snapshot.sequence
        if self._handle_battle_result_state(snapshot.battle_result):
            return False
        if not self._mouse("middle", 70, point=(0.50, 0.50)):
            self.log("鼠标中键锁定发送失败")
            return False
        self.log("已在游戏中心点击鼠标中键锁定目标")
        return self._interruptible_wait(0.25, vision)

    def _handle_battle_result_state(
        self,
        state,
    ) -> bool:
        '消费视觉线程给出的胜负结果。'
        if self._battle_result is not None:
            return True
        if state is None:
            return False
        self._battle_result = state.outcome
        result_text = "得胜" if state.outcome == "victory" else "惜败"
        self.log(
            f"识别到{result_text}，自动战斗结束；"
            f"OCR {state.confidence:.2f}"
        )
        self._stop.set()
        release_known_keys(self.log)
        return True

    def _handle_counter_state(
        self,
        key: str | None,
        now: float,
    ) -> bool:
        '消费视觉线程给出的 X/Z 提示。'
        if key is None:
            self._counter_absent_frames += 1
            if self._counter_absent_frames >= self.COUNTER_REARM_FRAMES:
                self._counter_armed = True
                self._counter_attempts = 0
            return False
        self._counter_absent_frames = 0
        if key != self._last_counter_key:
            self._counter_armed = True
            self._counter_attempts = 0
        elif not self._counter_armed:
            if (
                self._counter_attempts >= self.COUNTER_MAX_ATTEMPTS
                or now - self._counter_last_sent_at
                < self.COUNTER_RETRY_SECONDS
            ):
                return False
        vk = 0x5A if key == "z" else 0x58
        if not self._press_key(vk, self.COUNTER_HOLD_MS):
            return False
        self._counter_attempts += 1
        self._counter_last_sent_at = now
        self._counter_armed = False
        self._last_counter_key = key
        attempt_text = (
            "，提示未消失后补发"
            if self._counter_attempts > 1
            else ""
        )
        self.log(
            f"识别到 {key.upper()} 提示，已立即按 {key.upper()}"
            f"{attempt_text}"
        )
        return True

    def _handle_reaction_state(
        self,
        snapshot: CombatVisionSnapshot,
        now: float,
    ) -> bool:
        '消费后台视觉状态并执行最高优先级反制。'
        if self._handle_counter_state(snapshot.counter_key, now):
            self._last_red_event_id = max(
                self._last_red_event_id,
                snapshot.red_event_id,
            )
            return True
        if snapshot.red_event_id > self._last_red_event_id:
            self._last_red_event_id = snapshot.red_event_id
            red_flash = snapshot.red_flash
            event_at = getattr(snapshot, "red_event_at", None)
            if event_at is None:
                event_at = now
            event_age = max(0.0, now - float(event_at))
            if (
                red_flash is not None
                and event_age <= self.RED_AUDIO_CONFIRM_SECONDS
            ):
                self._red_visual_pending_until = (
                    float(event_at) + self.RED_AUDIO_CONFIRM_SECONDS
                )
                self._red_visual_score = red_flash.score
                self._red_audio_best_score = 0.0
                self._red_audio_ready = False
                sequence = getattr(
                    self._red_audio_detector,
                    "match_sequence",
                    None,
                )
                self._red_audio_match_sequence = (
                    int(sequence()) if callable(sequence) else 0
                )
                request = getattr(
                    self._red_audio_detector,
                    "request_match",
                    None,
                )
                if callable(request):
                    request()
                self.log(
                    f"检测到红光候选，等待声音共同确认；"
                    f"视觉分数 {red_flash.score:.2f}"
                )
        return self._finish_pending_dodge(now)

    def _clear_pending_red_flash(self) -> None:
        '清除已离开有效前台时间窗的红光联合判定。'
        self._red_visual_pending_until = 0.0
        self._red_visual_score = 0.0
        self._red_audio_best_score = 0.0
        self._red_audio_ready = False
        self._red_audio_match_sequence = 0

    def _read_red_audio_match(self):
        '非阻塞读取后台声音结果，兼容测试中的同步检测器。'
        latest = getattr(self._red_audio_detector, "latest_match", None)
        if callable(latest):
            sequence, state = latest(self._red_audio_match_sequence)
            if state is None:
                return None
            self._red_audio_match_sequence = int(sequence)
            if not state.matched:
                request = getattr(
                    self._red_audio_detector,
                    "request_match",
                    None,
                )
                if callable(request):
                    request()
            return state
        match_recent = getattr(self._red_audio_detector, "match_recent", None)
        return match_recent() if callable(match_recent) else None

    def _finish_pending_dodge(self, now: float) -> bool:
        '完成已经由后台视觉线程触发的声音联合确认。'
        if self._red_visual_pending_until <= 0.0:
            return False
        if now > self._red_visual_pending_until:
            ready_text = (
                f"声音最高分 {self._red_audio_best_score:.2f}"
                if self._red_audio_ready
                else "声音缓冲未就绪"
            )
            self.log(f"红光候选未通过声音共同确认，未闪避；{ready_text}")
            self._clear_pending_red_flash()
            return False
        if now < self._next_dodge_at:
            return False
        audio_cue = self._read_red_audio_match()
        if audio_cue is None:
            return False
        self._red_audio_ready = self._red_audio_ready or audio_cue.ready
        self._red_audio_best_score = max(
            self._red_audio_best_score,
            audio_cue.score,
        )
        if not audio_cue.matched:
            return False
        action = CombatAction(
            self.profile.dodge_key,
            55,
            self.profile.dodge_recovery_ms,
        )
        if not self._perform_action(action):
            return False
        self._clear_pending_red_flash()
        self._next_dodge_at = now + self.profile.dodge_recovery_ms / 1000.0
        if self._stop.wait(self.DODGE_ATTACK_DELAY_SECONDS):
            return True
        attacked = self._perform_action(CombatAction("mouse_left", 45, 0))
        self.log(
            f"声音和红光共同确认，已执行 {self.profile.dodge_key.upper()}"
            + ("→左键闪避攻击；" if attacked else "，左键发送失败；")
            + f"视觉 {self._red_visual_score:.2f}，声音 {audio_cue.score:.2f}"
        )
        return True

    def _should_run_ultimate(
        self,
        state,
        now: float,
        *,
        monster_stunned: bool = False,
    ) -> bool:
        if self.ultimate_mode == "disabled":
            return False
        if self._current_hero != self.profile.initial_hero:
            return False
        if now < self._ultimate_blocked_until.get(self._current_hero, 0.0):
            return False
        if self._ultimate_release_pending(self._current_hero):
            return False
        if now < self._ultimate_retry_not_before.get(self._current_hero, 0.0):
            return False
        if (
            self._ultimate_last_sent_at.get(self._current_hero, 0.0) > 0.0
            and now
            < self._ultimate_last_sent_at[self._current_hero]
            + self.ULTIMATE_MIN_REARM_SECONDS
        ):
            return False
        sequence = self.profile.ultimate_for(self._current_hero)
        if not bool(getattr(state, "available", state)) or not sequence:
            return False
        if (
            self._ultimate_ready_frames.get(self._current_hero, 0)
            < self.ULTIMATE_READY_CONFIRM_FRAMES
        ):
            return False
        latched = self._ultimate_latched.get(self._current_hero, False)
        if latched:
            return False
        if self.ultimate_mode == "stunned" and not monster_stunned:
            return False
        return True

    @staticmethod
    def _ultimate_ready_evidence(state) -> bool:
        evidence = getattr(state, "ready_evidence", None)
        return bool(state.available if evidence is None else evidence)

    @staticmethod
    def _ultimate_consumed_evidence(state) -> bool:
        evidence = getattr(state, "consumed_evidence", None)
        return bool(not state.available if evidence is None else evidence)

    def _ultimate_release_pending(self, hero: str) -> bool:
        return self._ultimate_pending_deadline.get(hero, 0.0) > 0.0

    def _clear_ultimate_release_pending(self, hero: str) -> None:
        self._ultimate_pending_since[hero] = 0.0
        self._ultimate_pending_deadline[hero] = 0.0
        self._ultimate_pending_consumed_frames[hero] = 0

    def _begin_ultimate_release_attempt(
        self,
        hero: str,
        sent_at: float,
    ) -> None:
        if self._ultimate_release_pending(hero):
            return
        last_confirmed = self._ultimate_last_sent_at.get(hero, 0.0)
        if (
            last_confirmed > 0.0
            and sent_at < last_confirmed + self.ULTIMATE_MIN_REARM_SECONDS
        ):
            return
        self._ultimate_pending_since[hero] = sent_at
        self._ultimate_pending_deadline[hero] = (
            sent_at + self.ULTIMATE_RELEASE_CONFIRM_TIMEOUT
        )
        self._ultimate_pending_consumed_frames[hero] = 0
        self._ultimate_latched[hero] = True
        self._ultimate_absent_frames[hero] = 0
        self._ultimate_ready_frames[hero] = 0
        self._switch_blocked_until = max(
            self._switch_blocked_until,
            self._ultimate_pending_deadline[hero],
        )
        self.log("已发送大招按键，后台确认图标是否实际消耗")

    def _update_ultimate_release_confirmation(
        self,
        state,
        now: float,
    ) -> str | None:
        hero = self._current_hero
        deadline = self._ultimate_pending_deadline.get(hero, 0.0)
        if deadline <= 0.0:
            return None

        if self._ultimate_consumed_evidence(state):
            confirmed_frames = (
                self._ultimate_pending_consumed_frames.get(hero, 0) + 1
            )
            self._ultimate_pending_consumed_frames[hero] = confirmed_frames
        else:
            confirmed_frames = 0
            self._ultimate_pending_consumed_frames[hero] = 0

        if confirmed_frames >= self.ULTIMATE_RELEASE_CONFIRM_FRAMES:
            sent_at = self._ultimate_pending_since[hero]
            self._ultimate_last_sent_at[hero] = sent_at
            blocked_until = (
                sent_at
                + self.profile.ultimate_duration_ms(hero) / 1000.0
            )
            self._switch_blocked_until = max(
                self._switch_blocked_until,
                blocked_until,
            )
            self._ultimate_blocked_until[hero] = max(
                self._ultimate_blocked_until.get(hero, 0.0),
                blocked_until,
            )
            self._clear_ultimate_release_pending(hero)
            self.log("大招图标已消耗，确认释放成功并开始20秒重置计时")
            return "confirmed"

        if now >= deadline:
            self._clear_ultimate_release_pending(hero)
            self._ultimate_latched[hero] = False
            self._ultimate_absent_frames[hero] = 0
            self._ultimate_ready_frames[hero] = 0
            self._ultimate_retry_not_before[hero] = (
                now + self.ULTIMATE_FAILED_RETRY_SECONDS
            )
            self.log("大招图标未消耗，本次不进入20秒锁定")
            return "failed"
        return "pending"

    def _handle_ultimate_state(
        self,
        ultimate,
        stun_state,
        now: float,
        vision,
        *,
        combat_active: bool = True,
    ) -> bool | None:
        '消费后台大招和昏迷状态，并按配置插入大招流程。'
        if ultimate.icon_image is not None:
            self._hero_ultimate_images[self._current_hero] = (
                ultimate.icon_image.copy()
            )
        monster_stunned = False
        if self.ultimate_mode == "stunned":
            monster_stunned = bool(stun_state and stun_state.stunned)
            newly_stunned = monster_stunned and not self._monster_stun_active
            if newly_stunned and stun_state is not None:
                self.log(
                    f"确认怪物昏迷：{stun_state.evidence}；"
                    f"X分数 {stun_state.x_score:.2f}，"
                    f"黄色条分数 {stun_state.yellow_score:.2f}"
                )
                if not ultimate.available:
                    self.log("怪物已昏迷，但主C大招图标未高亮，本次不释放大招")
            self._monster_stun_active = monster_stunned
        self._update_ultimate_release_confirmation(ultimate, now)
        if combat_active:
            self._ultimate_combat_seen_at = now
        combat_recent = bool(
            self._ultimate_combat_seen_at > 0.0
            and now - self._ultimate_combat_seen_at
            <= self.ULTIMATE_COMBAT_GRACE_SECONDS
        )
        if not combat_active and not combat_recent:
            self._ultimate_ready_frames[self._current_hero] = 0
            return False
        self._update_ultimate_rearm(ultimate)
        if self._ultimate_sequence_running:
            return False
        if not self._should_run_ultimate(
            ultimate,
            now,
            monster_stunned=monster_stunned,
        ):
            return False

        sequence = self.profile.ultimate_for(self._current_hero)
        hero_at_start = self._current_hero
        last_confirmed_before = self._ultimate_last_sent_at.get(
            hero_at_start,
            0.0,
        )
        self._ultimate_latched[hero_at_start] = True
        self._ultimate_absent_frames[hero_at_start] = 0
        self._ultimate_ready_frames[hero_at_start] = 0
        self._last_ultimate_signatures[hero_at_start] = ultimate.color_signature
        self._switch_blocked_until = max(
            self._switch_blocked_until,
            now + self.ULTIMATE_RELEASE_CONFIRM_TIMEOUT,
        )
        self.log(
            f"检测到{hero_at_start}大招可用，执行"
            + ("昏迷释放流程" if self.ultimate_mode == "stunned" else "立即释放流程")
            + f"；图标 {ultimate.color_score:.2f}，外圈 {ultimate.ring_score:.2f}"
        )
        self._ultimate_sequence_running = True
        try:
            if not self._run_sequence(
                sequence,
                vision,
                allow_secondary_preempt=(
                    hero_at_start == self.profile.initial_hero),
            ):
                if self._terminal_stop_requested():
                    return None
                self.log("大招流程输入未完成，返回主C普通流程继续战斗")
                return False
        finally:
            self._ultimate_sequence_running = False
        if (
            not self._ultimate_release_pending(hero_at_start)
            and self._ultimate_last_sent_at.get(hero_at_start, 0.0)
            <= last_confirmed_before
        ):
            self._ultimate_latched[hero_at_start] = False
        return True

    def _update_ultimate_rearm(self, state) -> None:
        hero = self._current_hero
        available = (
            bool(state)
            if isinstance(state, bool)
            else bool(state.available)
        )
        strict_ready = (
            bool(state)
            if isinstance(state, bool)
            else self._ultimate_ready_evidence(state)
        )
        consumed = (
            not bool(state)
            if isinstance(state, bool)
            else self._ultimate_consumed_evidence(state)
        )
        if available:
            self._ultimate_absent_frames[hero] = 0
            if (
                not self._ultimate_latched.get(hero, False)
                and not self._ultimate_release_pending(hero)
                and (
                    strict_ready
                    or self._ultimate_ready_frames.get(hero, 0) > 0
                )
            ):
                self._ultimate_ready_frames[hero] = min(
                    self.ULTIMATE_READY_CONFIRM_FRAMES,
                    self._ultimate_ready_frames.get(hero, 0) + 1,
                )
            return
        self._ultimate_ready_frames[hero] = 0
        if not consumed:
            return
        absent = self._ultimate_absent_frames.get(hero, 0) + 1
        self._ultimate_absent_frames[hero] = absent
        if (
            absent >= self.ULTIMATE_REARM_FRAMES
            and not self._ultimate_release_pending(hero)
        ):
            self._ultimate_latched[hero] = False

    def _consume_vision(
        self,
        vision: CombatVisionWorker,
        *,
        timeout: float,
        allow_ultimate: bool = True,
    ) -> str:
        '消费最新后台识别结果，不在动作线程执行图像计算。'
        snapshot = vision.wait_for_update(
            self._last_vision_sequence,
            timeout=timeout,
        )
        if snapshot is None:
            error = vision.error
            if error is not None and not self._vision_error_logged:
                self._vision_error_logged = True
                self.log(f"战斗视觉线程异常，自动战斗停止：{error}")
                self._stop.set()
                return "stop"
            if self._finish_pending_dodge(time.monotonic()):
                return "reaction"
            return "none"

        self._last_vision_sequence = snapshot.sequence
        if self._handle_battle_result_state(snapshot.battle_result):
            return "stop"
        now = time.monotonic()
        if self._handle_reaction_state(snapshot, now):
            return "reaction"
        if (
            allow_ultimate
            and snapshot.ultimate_sequence
            > self._last_ultimate_vision_sequence
        ):
            self._last_ultimate_vision_sequence = snapshot.ultimate_sequence
            handled = self._handle_ultimate_state(
                snapshot.ultimate,
                snapshot.monster_stun,
                now,
                vision,
                combat_active=bool(
                    getattr(snapshot, "combat_active", True)
                ),
            )
            if handled is None:
                return "stop"
            if handled:
                return "ultimate"
        return "none"

    def _adaptive_action(self, now: float) -> CombatAction:
        '按配置选择可用技能，全部冷却时保持等待。'
        if self._adaptive_followups:
            return self._adaptive_followups.pop(0)
        skill = next(
            (
                skill
                for skill in self.profile.skill_cooldowns
                if now >= self._skill_due.get(skill.key, float("inf"))
            ),
            None,
        )
        if skill is not None:
            segments = self.profile.rotation_segments(skill.key)
            if segments:
                segment_index = self._skill_segment_index.get(skill.key, 0)
                segment = segments[segment_index % len(segments)]
                self._skill_segment_index[skill.key] = segment_index + 1
                self._adaptive_followups.extend(segment[1:])
                return segment[0]
            return CombatAction(skill.key, 55, 235)
        return CombatAction(
            "idle",
            0,
            self.profile.basic_attack_interval_ms,
        )

    def _run_sequence(
        self,
        actions: tuple[CombatAction, ...],
        frames,
        *,
        allow_secondary_preempt: bool = False,
        on_action: Callable[[CombatAction], None] | None = None,
    ) -> bool:
        for index, action in enumerate(actions):
            if (
                allow_secondary_preempt
                and index > 0
                and not self._initial_main_skills_pending
                and self._due_secondary(time.monotonic()) is not None
            ):
                self.log("主C大招保护时间结束，进入已到期的辅助轮次")
                return True
            if not self._wait_for_foreground():
                return False
            action_started_at = time.monotonic()
            if not self._perform_action(action):
                return False
            if on_action is not None:
                on_action(action)
            remaining_wait = self._remaining_action_wait(
                action,
                action_started_at,
            )
            if not self._interruptible_wait(remaining_wait, frames):
                return False
        return True

    def _due_secondary(self, now: float) -> SecondarySequence | None:
        if now < self._switch_blocked_until:
            return None
        due = [
            sequence
            for sequence in self.profile.secondary_sequences
            if now >= self._secondary_due.get(sequence.hero, float("inf"))
        ]
        if not due:
            return None
        return min(due, key=lambda sequence: self._secondary_due[sequence.hero])

    def _secondary_after_normal_combo(
        self,
        now: float,
        rotation_index: int,
        next_action_at: float,
    ) -> SecondarySequence | None:
        if (
            self._initial_main_skills_pending
            or self._adaptive_followups
            or rotation_index != 0
            or now < next_action_at
        ):
            return None
        return self._due_secondary(now)

    def _run_secondary(self, sequence: SecondarySequence, frames) -> bool:
        '切入唯一辅助，可用时先按T，再执行同一套连招并切回主C。'
        self.log(f"{sequence.hero}切人时间已到，执行辅助连招")
        if not self._switch_to_hero(sequence.hero, frames):
            return self._skip_secondary_round(
                sequence,
                f"{sequence.hero}切换状态未确认，跳过本轮辅助，"
                "继续主C流程",
            )
        if (
            self.ultimate_mode != "disabled"
            and self._detect_secondary_ultimate(frames)
        ):
            self.log(f"检测到{sequence.hero}大招可用，立即按 T")
            self._ultimate_latched[sequence.hero] = True
            self._ultimate_absent_frames[sequence.hero] = 0
            if not self._perform_action(CombatAction("t", 65, 350)):
                return self._skip_secondary_round(
                    sequence,
                    f"{sequence.hero}大招输入未发送，跳过本轮辅助",
                )
            settle_s = self.SECONDARY_ULTIMATE_SETTLE_SECONDS
            self.log(
                f"{sequence.hero}大招输入稳定 {settle_s:g} 秒后继续连招；"
                "完整大招时长仅限制切人"
            )
            if not self._interruptible_wait(settle_s, frames):
                return self._skip_secondary_round(
                    sequence,
                    f"{sequence.hero}大招等待未完成，跳过本轮辅助",
                )
        else:
            self.log(f"{sequence.hero}大招不可用，直接执行同一套辅助连招")
        longest_cooldown = max(
            (skill.cooldown_ms for skill in sequence.skill_cooldowns),
            default=sequence.switch_after_ms,
        )
        longest_keys = {
            skill.key
            for skill in sequence.skill_cooldowns
            if skill.cooldown_ms == longest_cooldown
        }
        cooldown_started_at = None

        def mark_cooldown_start(action: CombatAction) -> None:
            nonlocal cooldown_started_at
            if cooldown_started_at is None and action.key in longest_keys:
                cooldown_started_at = time.monotonic()

        if not self._run_sequence(
            sequence.actions,
            frames,
            on_action=mark_cooldown_start,
        ):
            return self._skip_secondary_round(
                sequence,
                f"{sequence.hero}连招未完成，跳过本轮辅助",
            )
        if not self._switch_to_hero(self.profile.initial_hero, frames):
            return self._skip_secondary_round(
                sequence,
                "切回主C状态未确认，本轮辅助结束，战斗继续运行",
            )
        self._secondary_due[sequence.hero] = (
            (cooldown_started_at or time.monotonic())
            + sequence.switch_after_ms / 1000.0
        )
        remaining = max(
            0.0,
            self._secondary_due[sequence.hero] - time.monotonic(),
        )
        self.log(
            f"{sequence.hero}连招完成，已发送{self.profile.switch_key.upper()}"
            f"切回{self.profile.initial_hero}；"
            f"最长冷却技能释放后开始计时，约 {remaining:g} 秒后进入下一轮"
        )
        return True

    def _defer_secondary(self, sequence: SecondarySequence) -> None:
        '辅助未确认时跳过当前轮次，避免立即连续发送切换键。'
        self._secondary_due[sequence.hero] = (
            time.monotonic() + sequence.switch_after_ms / 1000.0
        )

    def _skip_secondary_round(
        self,
        sequence: SecondarySequence,
        message: str,
    ) -> bool:
        '非终止性失败只推迟辅助轮次，不结束自动战斗。'
        if self._terminal_stop_requested():
            return False
        self._defer_secondary(sequence)
        self.log(message)
        return True

    def _detect_secondary_ultimate(self, frames) -> bool:
        '切人稳定后用连续新帧确认辅助大招状态。'
        ready_frames = 0
        valid_frames = 0
        last_ultimate_sequence = 0
        deadline = time.monotonic() + 1.2
        frames.set_fast_ultimate(True)
        try:
            while (
                valid_frames < 3
                and not self._stop.is_set()
                and time.monotonic() < deadline
            ):
                state = frames.wait_for_update(
                    self._last_vision_sequence,
                    timeout=min(0.30, deadline - time.monotonic()),
                )
                if state is None:
                    continue
                self._last_vision_sequence = state.sequence
                if self._handle_battle_result_state(state.battle_result):
                    return False
                if self._handle_reaction_state(state, time.monotonic()):
                    continue
                if state.ultimate_sequence == last_ultimate_sequence:
                    continue
                last_ultimate_sequence = state.ultimate_sequence
                valid_frames += 1
                if (
                    bool(getattr(state, "combat_active", True))
                    and self._ultimate_ready_evidence(state.ultimate)
                ):
                    ready_frames += 1
                if ready_frames >= 2:
                    return True
                if valid_frames - ready_frames >= 2:
                    return False
            return valid_frames >= 2 and ready_frames >= 2
        finally:
            frames.set_fast_ultimate(False)

    def _switch_to_hero(self, target: str, frames) -> bool:
        if not self._wait_for_switch_window(frames):
            return False
        transitions = 0
        while (
            self._current_hero != target
            and transitions < len(self.profile.hero_order)
        ):
            before_icon = self._hero_ultimate_images.get(self._current_hero)
            if before_icon is None:
                before_icon = self._read_ultimate_icon_image(frames)
            if before_icon is not None:
                switched, after_icon = self._switch_once_by_ultimate_image(
                    before_icon,
                    frames,
                )
                if not switched or after_icon is None:
                    return False
                self._advance_hero()
                self._hero_ultimate_images[self._current_hero] = (
                    after_icon.copy()
                )
                self.log(
                    "角色切换确认成功：大招图标已连续两帧稳定变化"
                )
                transitions += 1
                continue

            self.log("未取得大招图标，回退最大生命值OCR确认切人")
            before_max = self._hero_max_health.get(self._current_hero)
            if before_max is None:
                before_max = self._read_stable_max_health(frames)
                if before_max is None:
                    self.log("切换前无法稳定读取最大生命值，本次不发送TAB")
                    return False
                self._hero_max_health[self._current_hero] = before_max
            else:
                self.log(
                    f"使用已确认的{self._current_hero}最大生命值 "
                    f"{before_max}，跳过切换前OCR"
                )
            after_max = self._switch_once_by_health(before_max, frames)
            if after_max is None:
                return False
            self._advance_hero()
            self._hero_max_health[self._current_hero] = after_max
            self.log(
                f"角色切换确认成功：最大生命值 "
                f"{before_max} -> {after_max}"
            )
            transitions += 1
        return self._current_hero == target

    def _switch_once_by_ultimate_image(self, before_icon, frames):
        '通过大招图标变化确认一次切人，切换被打断时最多补按一次。'
        switch_action = CombatAction(
            key=self.profile.switch_key,
            hold_ms=55,
            wait_ms=self.SWITCH_VISUAL_WAIT_MS,
        )
        for attempt in range(1, self.SWITCH_MAX_ATTEMPTS + 1):
            action_started_at = time.monotonic()
            if not self._perform_action(switch_action, track_switch=False):
                return False, None
            if not self._interruptible_wait(
                self._remaining_action_wait(
                    switch_action,
                    action_started_at,
                ),
                frames,
                allow_ultimate=False,
            ):
                return False, None
            state, after_icon, similarity = self._observe_ultimate_switch(
                before_icon,
                frames,
            )
            if state == "changed":
                self.log(
                    f"TAB后大招图标相似度 {similarity:.2f}，确认角色已变化"
                )
                return True, after_icon
            if state == "unknown":
                summary = getattr(
                    self,
                    "_last_switch_observation",
                    "无有效连续帧",
                )
                self.log(
                    f"TAB后大招图标变化不稳定（{summary}）；"
                    "不补按TAB，交由主流程跳过本轮辅助"
                )
                return False, None
            if attempt < self.SWITCH_MAX_ATTEMPTS:
                self.log(
                    f"TAB后大招图标相似度 {similarity:.2f}，"
                    "等待3秒让被攻击僵直结束"
                )
                if not self._interruptible_wait(
                    self.SWITCH_RETRY_DELAY_SECONDS,
                    frames,
                    allow_ultimate=False,
                ):
                    return False, None
                delayed_state, delayed_icon, delayed_similarity = (
                    self._observe_ultimate_switch(before_icon, frames)
                )
                if delayed_state == "changed":
                    self.log(
                        "角色延迟切换确认成功：大招图标相似度 "
                        f"{delayed_similarity:.2f}，不再补按TAB"
                    )
                    return True, delayed_icon
                if delayed_state == "unknown":
                    summary = getattr(
                        self,
                        "_last_switch_observation",
                        "无有效连续帧",
                    )
                    self.log(
                        f"等待后大招图标变化仍不稳定（{summary}）；"
                        "不补按TAB，交由主流程跳过本轮辅助"
                    )
                    return False, None
                self.log("等待3秒后大招图标未变化，补按一次TAB")
        self.log(
            f"连续{self.SWITCH_MAX_ATTEMPTS}次TAB后大招图标仍未变化，"
            "角色切换失败"
        )
        return False, None

    def _observe_ultimate_switch(self, before_icon, frames):
        '过滤切换过渡帧，以多帧投票确认大招图标是否变化。'
        changed_icon = None
        changed_frames = 0
        same_frames = 0
        valid_frames = 0
        last_similarity = -1.0
        similarities = []
        frames.set_switch_reference(before_icon)
        try:
            for _ in range(self.SWITCH_VERIFY_SAMPLES):
                if self._stop.is_set():
                    return "unknown", None, last_similarity
                snapshot = frames.wait_for_update(
                    self._last_vision_sequence,
                    timeout=0.25,
                )
                if snapshot is None:
                    continue
                self._last_vision_sequence = snapshot.sequence
                if self._handle_battle_result_state(
                    snapshot.battle_result
                ):
                    return "unknown", None, last_similarity
                icon = snapshot.ultimate.icon_image
                similarity = snapshot.switch_similarity
                if icon is None or similarity is None:
                    continue
                valid_frames += 1
                last_similarity = similarity
                if valid_frames <= self.SWITCH_SETTLE_SKIP_FRAMES:
                    continue
                similarities.append(similarity)
                if last_similarity <= self.SWITCH_ICON_CHANGE_SIMILARITY:
                    changed_frames += 1
                    changed_icon = icon
                    if changed_frames >= self.SWITCH_STABLE_READS:
                        self._last_switch_observation = (
                            f"变化票{changed_frames}，相同票{same_frames}，"
                            f"范围{min(similarities):.2f}~"
                            f"{max(similarities):.2f}"
                        )
                        return "changed", icon, last_similarity
                    continue
                if last_similarity >= self.SWITCH_ICON_SAME_SIMILARITY:
                    same_frames += 1
                    if same_frames >= self.SWITCH_SAME_VOTES:
                        self._last_switch_observation = (
                            f"变化票{changed_frames}，相同票{same_frames}，"
                            f"范围{min(similarities):.2f}~"
                            f"{max(similarities):.2f}"
                        )
                        return "same", None, last_similarity
            if changed_frames >= self.SWITCH_STABLE_READS:
                return "changed", changed_icon, last_similarity
            if same_frames >= self.SWITCH_SAME_VOTES:
                return "same", None, last_similarity
            if similarities:
                score_range = (
                    f"{min(similarities):.2f}~{max(similarities):.2f}"
                )
            else:
                score_range = "无"
            self._last_switch_observation = (
                f"有效{len(similarities)}帧，变化票{changed_frames}，"
                f"相同票{same_frames}，范围{score_range}"
            )
            return "unknown", None, last_similarity
        finally:
            frames.set_switch_reference(None)

    def _read_ultimate_icon_image(self, frames):
        '从下一张共享帧读取大招图标，不创建新的截图来源。'
        frames.set_fast_ultimate(True)
        try:
            for _ in range(3):
                snapshot = frames.wait_for_update(
                    self._last_vision_sequence,
                    timeout=0.25,
                )
                if snapshot is None:
                    continue
                self._last_vision_sequence = snapshot.sequence
                icon = snapshot.ultimate.icon_image
                if icon is not None:
                    return icon
            return None
        finally:
            frames.set_fast_ultimate(False)

    def _switch_once_by_health(
        self,
        before_max: int,
        frames,
    ) -> int | None:
        '大招图标不可用时以最大生命值确认一次切人。'
        switch_action = CombatAction(
            key=self.profile.switch_key,
            hold_ms=55,
            wait_ms=405,
        )
        for attempt in range(1, self.SWITCH_MAX_ATTEMPTS + 1):
            action_started_at = time.monotonic()
            if not self._perform_action(switch_action, track_switch=False):
                return None
            if not self._interruptible_wait(
                self._remaining_action_wait(
                    switch_action,
                    action_started_at,
                ),
                frames,
                allow_ultimate=False,
            ):
                return None
            after_max = self._read_stable_max_health(
                frames,
                previous_max=before_max,
            )
            if after_max is None:
                self.log(
                    "TAB后无法稳定读取最大生命值；"
                    "为避免切换成功后再次按TAB，本次停止切换"
                )
                return None
            if after_max != before_max:
                return after_max
            if attempt < self.SWITCH_MAX_ATTEMPTS:
                self.log(
                    f"TAB后最大生命值仍为 {before_max}，"
                    "等待3秒让被攻击僵直结束"
                )
                if not self._interruptible_wait(
                    self.SWITCH_RETRY_DELAY_SECONDS,
                    frames,
                    allow_ultimate=False,
                ):
                    return None
                delayed_max = self._read_stable_max_health(
                    frames,
                    previous_max=before_max,
                )
                if delayed_max is None:
                    self.log(
                        "补按前无法稳定读取最大生命值；"
                        "为避免重复切换，本次停止切换"
                    )
                    return None
                if delayed_max != before_max:
                    self.log(
                        f"角色延迟切换确认成功：最大生命值 "
                        f"{before_max} -> {delayed_max}，不再补按TAB"
                    )
                    return delayed_max
                self.log(
                    f"等待3秒后最大生命值仍为 {before_max}，"
                    "补按一次TAB"
                )
        self.log(
            f"连续{self.SWITCH_MAX_ATTEMPTS}次TAB后最大生命值"
            f"仍为 {before_max}，角色切换失败"
        )
        return None

    def _read_stable_max_health(
        self,
        frames,
        *,
        previous_max: int | None = None,
    ) -> int | None:
        '使用连续新帧读取相同的最大生命值，过滤单帧OCR错误。'
        counts: dict[int, int] = {}
        last_health_sequence = 0
        frames.set_health_tracking(True)
        try:
            for _ in range(self.SWITCH_VERIFY_SAMPLES * 2):
                if self._stop.is_set():
                    return None
                snapshot = frames.wait_for_update(
                    self._last_vision_sequence,
                    timeout=0.35,
                )
                if snapshot is None:
                    continue
                self._last_vision_sequence = snapshot.sequence
                if self._handle_battle_result_state(
                    snapshot.battle_result
                ):
                    return None
                if snapshot.health_sequence == last_health_sequence:
                    continue
                last_health_sequence = snapshot.health_sequence
                state = snapshot.player_health
                if state is None:
                    continue
                count = counts.get(state.maximum, 0) + 1
                counts[state.maximum] = count
                if (
                    count >= self.SWITCH_STABLE_READS
                    and (
                        previous_max is None
                        or state.maximum != previous_max
                    )
                ):
                    return state.maximum
            if (
                previous_max is not None
                and counts.get(previous_max, 0)
                >= self.SWITCH_STABLE_READS
            ):
                return previous_max
            return None
        finally:
            frames.set_health_tracking(False)

    def _wait_for_switch_window(self, frames) -> bool:
        '等待当前英雄的大招保护时间结束，期间仍处理实时战斗提示。'
        while not self._stop.is_set():
            remaining = self._switch_blocked_until - time.monotonic()
            if remaining <= 0:
                return True
            if not self._interruptible_wait(remaining, frames):
                return False
        return False

    def _interruptible_wait(
        self,
        duration: float,
        frames,
        *,
        allow_ultimate: bool = True,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, duration)
        while not self._stop.is_set() and time.monotonic() < deadline:
            if not self._foreground_ready():
                paused_at = time.monotonic()
                if not self._wait_for_foreground():
                    return False
                deadline += time.monotonic() - paused_at
                continue
            remaining = deadline - time.monotonic()
            status = self._consume_vision(
                frames,
                timeout=min(0.30, max(0.001, remaining)),
                allow_ultimate=allow_ultimate,
            )
            if status == "stop":
                return False
            if status in ("reaction", "ultimate"):
                deadline += (
                    self.profile.reaction_recovery_ms / 1000.0
                )
        return not self._stop.is_set()

    def _wait_for_foreground(self) -> bool:
        '用户切走游戏时暂停，不主动抢回前台。'
        while not self._stop.is_set():
            if self._foreground_ready():
                return True
            self._stop.wait(0.10)
        return False

    def _perform_action(
        self,
        action: CombatAction,
        *,
        track_switch: bool = True,
    ) -> bool:
        if action.key == "idle":
            return True
        if action.key == "wait":
            self.log(f"等待 {action.wait_ms / 1000.0:g} 秒")
            return True
        if action.vk is not None:
            sent = self._press_key(action.vk, action.hold_ms)
        else:
            sent = self._mouse(action.mouse_button or "", action.hold_ms)
        if sent:
            self.log(f"执行按键 {action.key.upper()}")
            if action.key == "t":
                sent_at = time.monotonic()
                self._begin_ultimate_release_attempt(
                    self._current_hero,
                    sent_at,
                )
            if action.key == self.profile.switch_key and track_switch:
                self._advance_hero()
            if self._current_hero == self.profile.initial_hero:
                cooldown = next(
                    (
                        skill.cooldown_ms
                        for skill in self.profile.skill_cooldowns
                        if skill.key == action.key
                    ),
                    None,
                )
                if cooldown is not None:
                    self._initial_main_skills_pending.discard(action.key)
                    self._skill_due[action.key] = (
                        time.monotonic() + cooldown / 1000.0
                    )
        return sent

    @staticmethod
    def _remaining_action_wait(
        action: CombatAction,
        action_started_at: float,
    ) -> float:
        '返回从按键按下时刻计算后仍需等待的时间。'
        deadline = action_started_at + action.wait_ms / 1000.0
        return max(0.0, deadline - time.monotonic())

    def _advance_hero(self) -> None:
        heroes = self.profile.hero_order
        try:
            index = heroes.index(self._current_hero)
        except ValueError:
            index = -1
        self._current_hero = heroes[(index + 1) % len(heroes)]
        self.log(f"切换当前英雄：{self._current_hero}")

    def _press_key(self, vk: int, hold_ms: int) -> bool:
        if vk == 0x10:
            return self._key_scheduler.press_scan_code(
                0x2A,
                hold_s=max(0.08, hold_ms / 1000.0),
            )
        return self._key_scheduler.press_mapped_key(
            vk,
            hold_s=max(0.01, hold_ms / 1000.0),
        )

    def _mouse(self, button: str, hold_ms: int, point=None) -> bool:
        return safe_mouse_button(
            button,
            stop_check=self._stop.is_set,
            foreground_check=lambda: bool(self._hwnd and is_foreground(self._hwnd)),
            log=self.log,
            hold_s=max(0.01, hold_ms / 1000.0),
            hwnd=self._hwnd if point is not None else 0,
            point=point,
        )
