'自动战斗后台视觉流水线。'
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from combat.recognizer import (
    COMBAT_ROIS,
    HEALTH_COMBAT_ROIS,
    STUN_COMBAT_ROIS,
    BattleResultState,
    MonsterStunDetector,
    MonsterStunState,
    PlayerHealthState,
    RedFlashDetector,
    RedFlashState,
    UltimateAvailabilityDetector,
    UltimateState,
    detect_battle_result,
    detect_combat_health_bar,
    detect_counter_key,
    read_player_health,
    ultimate_icon_similarity,
)
from runtime_guard import dev_log


@dataclass(frozen=True)
class CombatVisionSnapshot:
    '视觉线程发布的最新不可变战斗状态。'

    sequence: int
    captured_at: float
    counter_key: str | None
    counter_score: float
    red_event_id: int
    red_event_at: float
    red_flash: RedFlashState | None
    ultimate_sequence: int
    ultimate: UltimateState
    monster_stun: MonsterStunState | None
    battle_result: BattleResultState | None
    health_sequence: int
    player_health: PlayerHealthState | None
    switch_similarity: float | None
    switch_stability: float | None
    combat_active: bool


class CombatVisionWorker:
    '只保留最新帧结果，避免视觉计算阻塞动作时间轴。'

    ULTIMATE_INTERVAL = 0.10
    BATTLE_RESULT_INTERVAL = 1.0
    HEALTH_INTERVAL = 0.10

    def __init__(
        self,
        frames,
        *,
        interval: float,
        ultimate_mode: str,
        stop_check: Callable[[], bool],
    ) -> None:
        self._frames = frames
        self._interval = max(0.015, float(interval))
        self._ultimate_mode = ultimate_mode
        self._stop_check = stop_check
        self._stop = threading.Event()
        self._active = threading.Event()
        self._active.set()
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._latest: CombatVisionSnapshot | None = None
        self._error: Exception | None = None
        self._health_tracking = False
        self._fast_ultimate = False
        self._switch_reference: np.ndarray | None = None
        self._switch_previous_icon: np.ndarray | None = None

        self._ultimate_detector = UltimateAvailabilityDetector()
        self._stun_detector = MonsterStunDetector()
        self._red_detector = RedFlashDetector()
        self._ultimate_state = UltimateState(False, 0.0, ())
        self._ultimate_sequence = 0
        self._stun_state: MonsterStunState | None = None
        self._battle_result: BattleResultState | None = None
        self._player_health: PlayerHealthState | None = None
        self._health_sequence = 0
        self._red_event_id = 0
        self._red_event_at = 0.0
        self._red_event: RedFlashState | None = None
        self._source_sequence = 0

    @property
    def error(self) -> Exception | None:
        with self._condition:
            return self._error

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="combat-vision",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self.request_stop()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.0)

    def request_stop(self) -> None:
        '只发出停止请求，供 UI/F12 路径无阻塞调用。'
        self._stop.set()
        self._active.set()
        with self._condition:
            self._condition.notify_all()

    def __enter__(self) -> "CombatVisionWorker":
        self.start()
        return self

    def __exit__(self, *_exc) -> bool:
        self.stop()
        return False

    def set_active(self, active: bool) -> None:
        if active:
            self._active.set()
        else:
            self._active.clear()
        setter = getattr(self._frames, "set_enabled", None)
        if setter is not None:
            try:
                setter(bool(active))
            except Exception as exc:
                dev_log("combat vision: 切换截图订阅状态失败", exc)

    def set_health_tracking(self, enabled: bool) -> None:
        with self._condition:
            self._health_tracking = bool(enabled)
            if not enabled:
                self._player_health = None
            self._condition.notify_all()

    def set_fast_ultimate(self, enabled: bool) -> None:
        with self._condition:
            self._fast_ultimate = bool(enabled)
            self._condition.notify_all()

    def set_switch_reference(self, icon: np.ndarray | None) -> None:
        with self._condition:
            self._switch_reference = (
                None if icon is None else np.asarray(icon).copy()
            )
            self._switch_previous_icon = None
            self._fast_ultimate = icon is not None
            self._condition.notify_all()

    def latest(self) -> CombatVisionSnapshot | None:
        with self._condition:
            return self._latest

    def wait_for_update(
        self,
        after_sequence: int,
        timeout: float,
    ) -> CombatVisionSnapshot | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while not self._stop.is_set() and not self._stop_check():
                if (
                    self._latest is not None
                    and self._latest.sequence > after_sequence
                ):
                    return self._latest
                if self._error is not None:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return None
                self._condition.wait(remaining)
        return None

    def wait_until_ready(self, timeout: float = 1.0) -> bool:
        return self.wait_for_update(0, timeout) is not None

    def _current_rois(self):
        with self._condition:
            health_tracking = self._health_tracking
        if health_tracking:
            base = HEALTH_COMBAT_ROIS
            if self._ultimate_mode == "stunned":
                return (*base, STUN_COMBAT_ROIS[-1])
            return base
        return (
            STUN_COMBAT_ROIS
            if self._ultimate_mode == "stunned"
            else COMBAT_ROIS
        )

    def _run(self) -> None:
        try:
            try:
                import win32api
                import win32process

                win32process.SetThreadPriority(
                    win32api.GetCurrentThread(),
                    win32process.THREAD_PRIORITY_BELOW_NORMAL,
                )
            except Exception as exc:
                dev_log("combat vision: 设置低线程优先级失败，继续运行", exc)

            next_ultimate_at = 0.0
            next_result_at = 0.0
            next_health_at = 0.0
            while not self._stop.is_set() and not self._stop_check():
                if not self._active.wait(0.10):
                    continue
                cycle_started = time.monotonic()
                rois = self._current_rois()
                snapshot = self._frames.get_frame(
                    self._interval,
                    rois,
                    timeout=0.30,
                )
                if snapshot is None or snapshot.frame is None:
                    continue
                self._source_sequence = max(
                    self._source_sequence + 1,
                    int(getattr(snapshot, "sequence", 0)),
                )
                frame = snapshot.frame
                now = time.monotonic()

                counter_key, counter_score = detect_counter_key(frame)
                combat_active = detect_combat_health_bar(frame)
                red_flash = self._red_detector.update(frame)
                if red_flash.triggered:
                    self._red_event_id += 1
                    self._red_event_at = float(
                        getattr(snapshot, "captured_at", now)
                    )
                    self._red_event = red_flash

                with self._condition:
                    fast_ultimate = self._fast_ultimate
                    health_tracking = self._health_tracking
                    switch_reference = self._switch_reference
                if fast_ultimate or now >= next_ultimate_at:
                    self._ultimate_state = self._ultimate_detector.update(frame)
                    self._ultimate_sequence += 1
                    next_ultimate_at = now + self.ULTIMATE_INTERVAL
                    if self._ultimate_mode == "stunned":
                        self._stun_state = self._stun_detector.update(frame)

                if self._battle_result is None and now >= next_result_at:
                    result = detect_battle_result(frame)
                    if result is not None:
                        self._battle_result = result
                    next_result_at = now + self.BATTLE_RESULT_INTERVAL

                if health_tracking and now >= next_health_at:
                    self._player_health = read_player_health(frame)
                    self._health_sequence += 1
                    next_health_at = now + self.HEALTH_INTERVAL

                switch_similarity = None
                switch_stability = None
                if (
                    switch_reference is not None
                    and self._ultimate_state.icon_image is not None
                ):
                    switch_similarity = ultimate_icon_similarity(
                        switch_reference,
                        self._ultimate_state.icon_image,
                    )
                    switch_stability = ultimate_icon_similarity(
                        self._switch_previous_icon,
                        self._ultimate_state.icon_image,
                    )
                    self._switch_previous_icon = (
                        self._ultimate_state.icon_image.copy()
                    )

                state = CombatVisionSnapshot(
                    sequence=self._source_sequence,
                    captured_at=float(
                        getattr(snapshot, "captured_at", now)
                    ),
                    counter_key=counter_key,
                    counter_score=counter_score,
                    red_event_id=self._red_event_id,
                    red_event_at=self._red_event_at,
                    red_flash=self._red_event,
                    ultimate_sequence=self._ultimate_sequence,
                    ultimate=self._ultimate_state,
                    monster_stun=self._stun_state,
                    battle_result=self._battle_result,
                    health_sequence=self._health_sequence,
                    player_health=self._player_health,
                    switch_similarity=switch_similarity,
                    switch_stability=switch_stability,
                    combat_active=combat_active,
                )
                with self._condition:
                    self._latest = state
                    self._condition.notify_all()
                remaining = self._interval - (
                    time.monotonic() - cycle_started
                )
                if remaining > 0.0:
                    self._stop.wait(remaining)
        except Exception as exc:
            if not self._stop.is_set() and not self._stop_check():
                dev_log("combat vision worker failed", exc)
                with self._condition:
                    self._error = exc
                    self._condition.notify_all()
