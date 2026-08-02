'识别录制中的已完成地图传送，并生成可安全替换的语义区间。'
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import numpy as np

from daily import recognizer as rec

from .compiler import TeleportSegment


TARGET_UI_ROI = (0.58, 0.10, 0.99, 0.90)
TARGET_ACTION_ROI = (0.70, 0.58, 0.98, 0.86)
TELEPORT_ACTIONS = ("传送", "快速前往")


def load_recordable_teleport_names() -> tuple[str, ...]:
    '读取公共地图登记的可点击传送目标名称。'
    asset_dir = Path(__file__).resolve().parents[1] / "assets" / "world_map"
    names: set[str] = set()
    for file_name in ("targets_v1.json", "special_targets_v1.json"):
        path = asset_dir / file_name
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        for item in raw.get("targets", ()):
            name = str(item.get("name") or "").strip()
            kind = str(item.get("kind") or "").strip().lower()
            if name and kind != "destination":
                names.add(name)
    return tuple(sorted(names, key=lambda name: (-len(name), name)))


class RecordedTeleportObserver:
    '只在按M后的地图阶段运行OCR，成功到达后才提交替换区间。'

    MAP_OPEN_TIMEOUT = 4.0
    EVIDENCE_TTL = 1.2
    ARRIVAL_TIMEOUT = 45.0
    HUD_CONFIRM_FRAMES = 2

    def __init__(self, names: tuple[str, ...] | None = None) -> None:
        self._names = tuple(names or load_recordable_teleport_names())
        self._lock = threading.RLock()
        self._segments: list[TeleportSegment] = []
        self._reset_locked()

    def note_map_key(self, at: float) -> None:
        '以M按下作为候选地图段起点；已有候选时不覆盖原起点。'
        with self._lock:
            if self._active:
                return
            self._reset_locked()
            self._active = True
            self._start_at = max(0.0, float(at))

    def note_click(
            self, at: float, x: float, y: float, *, button: str,
            pressed: bool) -> None:
        '只把已识别详情中的传送按钮点击记为候选传送动作。'
        if not pressed or str(button).lower() != "left":
            return
        now = max(0.0, float(at))
        point = (float(x), float(y))
        with self._lock:
            if (not self._active or not self._saw_map
                    or not self._evidence_target
                    or now - self._evidence_at > self.EVIDENCE_TTL
                    or not _inside(point, TARGET_ACTION_ROI)):
                return
            if self._evidence_point is not None:
                distance = float(np.hypot(
                    point[0] - self._evidence_point[0],
                    point[1] - self._evidence_point[1]))
                if distance > 0.12:
                    return
            self._clicked_target = self._evidence_target
            self._action_at = now
            self._saw_departure = False
            self._first_hud_at = None
            self._hud_streak = 0

    def update(self, frame: np.ndarray, at: float, *, hud: bool) -> None:
        '消费录制线程的新帧；任何未确认流程都只丢弃语义候选。'
        now = max(0.0, float(at))
        with self._lock:
            if not self._active:
                return
            start_at = self._start_at
            action_at = self._action_at
            clicked_target = self._clicked_target

        try:
            in_map = bool(rec.in_world_map(frame))
        except Exception:
            in_map = False

        if clicked_target:
            self._update_arrival(now, in_map=in_map, hud=bool(hud))
            return

        if in_map:
            detail = _read_teleport_detail(frame, self._names)
            with self._lock:
                if not self._active or self._action_at != action_at:
                    return
                self._saw_map = True
                if detail is None:
                    self._evidence_target = ""
                    self._evidence_point = None
                    self._evidence_at = 0.0
                else:
                    target, point = detail
                    self._evidence_target = target
                    self._evidence_point = point
                    self._evidence_at = now
            return

        with self._lock:
            if not self._active or self._clicked_target:
                return
            if self._saw_map and hud:
                self._reset_locked()
            elif not self._saw_map and now - start_at >= self.MAP_OPEN_TIMEOUT:
                self._reset_locked()

    def cancel(self) -> None:
        '失去前台或录制结束时取消尚未确认的地图候选。'
        with self._lock:
            self._reset_locked()

    def segments(self) -> tuple[TeleportSegment, ...]:
        with self._lock:
            return tuple(self._segments)

    def _update_arrival(self, now: float, *, in_map: bool, hud: bool) -> None:
        with self._lock:
            if not self._active or not self._clicked_target:
                return
            if now - self._action_at > self.ARRIVAL_TIMEOUT:
                self._reset_locked()
                return
            if in_map:
                self._hud_streak = 0
                self._first_hud_at = None
                return
            if not hud:
                self._saw_departure = True
                self._hud_streak = 0
                self._first_hud_at = None
                return
            if self._first_hud_at is None:
                self._first_hud_at = now
            self._hud_streak += 1
            enough_time = now - self._action_at >= 0.8
            if (self._hud_streak < self.HUD_CONFIRM_FRAMES
                    or not (self._saw_departure or enough_time)):
                return
            self._segments.append(TeleportSegment(
                start_at=self._start_at,
                action_at=self._action_at,
                end_at=float(self._first_hud_at),
                target=self._clicked_target,
            ))
            self._reset_locked()

    def _reset_locked(self) -> None:
        self._active = False
        self._start_at = 0.0
        self._saw_map = False
        self._evidence_target = ""
        self._evidence_at = 0.0
        self._evidence_point: tuple[float, float] | None = None
        self._clicked_target = ""
        self._action_at = 0.0
        self._saw_departure = False
        self._first_hud_at: float | None = None
        self._hud_streak = 0


def _read_teleport_detail(
        frame: np.ndarray, names: tuple[str, ...],
        ) -> tuple[str, tuple[float, float]] | None:
    lines = rec.ocr_lines(frame, TARGET_UI_ROI, upscale=1.35)
    cleaned = [(_clean(text), float(cx), float(cy)) for text, cx, cy in lines]
    action = next((
        (cx, cy)
        for text, cx, cy in cleaned
        if any(word in text for word in TELEPORT_ACTIONS)
        and _inside((cx, cy), TARGET_ACTION_ROI)
    ), None)
    if action is None:
        return None

    exact = {name for text, _, _ in cleaned for name in names if text == name}
    candidates = exact or {
        name for text, _, _ in cleaned for name in names if name in text}
    if not candidates:
        return None
    longest = max(len(name) for name in candidates)
    best = sorted(name for name in candidates if len(name) == longest)
    if len(best) != 1:
        return None
    return best[0], action


def _clean(text: object) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", str(text)))


def _inside(point: tuple[float, float], roi: tuple[float, float, float, float]) -> bool:
    return roi[0] <= point[0] <= roi[2] and roi[1] <= point[1] <= roi[3]
