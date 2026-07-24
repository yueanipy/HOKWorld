'通过大地图角色姿态执行有限步坐标闭环。'
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

import numpy as np

from daily import recognizer as rec
from .core import WorldMapAtlas, WorldMapNavigator
from .pose import PlayerMapPose, locate_stable_player_map_pose, navigation_vector


Checkpoint = Callable[[str, np.ndarray | None, dict], None]


@dataclass(frozen=True)
class CoordinateNavigationConfig:
    '限制坐标闭环的精度、输入幅度和停止条件。'

    arrival_tolerance: float = 2500.0
    heading_tolerance_deg: float = 12.0
    camera_px_per_degree: float = 3.2
    max_turn_px: int = 420
    min_walk_s: float = 0.10
    max_walk_s: float = 0.28
    departure_walk_s: float = 0.72
    estimated_units_per_second: float = 24000.0
    max_iterations: int = 8
    max_stalled_iterations: int = 2
    minimum_progress: float = 80.0
    pose_timeout_s: float = 4.5
    total_timeout_s: float = 90.0


@dataclass(frozen=True)
class CoordinateNavigationStep:
    '保存一次地图纠偏前后的坐标结果。'

    index: int
    source: tuple[float, float]
    character_heading_deg: float
    distance: float
    bearing_deg: float
    turn_deg: float
    turn_px: int
    walk_s: float
    progress: float | None

    @property
    def heading_deg(self) -> float:
        '兼容旧调用，返回本步测得的角色朝向。'
        return self.character_heading_deg


@dataclass(frozen=True)
class CoordinateNavigationResult:
    '保存有限步坐标闭环的最终结果。'

    success: bool
    reason: str
    target: tuple[float, float]
    final_pose: PlayerMapPose | None
    final_distance: float | None
    steps: tuple[CoordinateNavigationStep, ...]


@dataclass(frozen=True)
class HeadingAlignmentStep:
    '保存一次镜头命令及其后的角色朝向复测结果。'

    index: int
    character_heading_before_deg: float
    error_before: float
    turn_px: int
    character_heading_after_deg: float
    error_after: float

    @property
    def heading_before(self) -> float:
        '兼容旧调用，返回输入前的角色朝向。'
        return self.character_heading_before_deg

    @property
    def heading_after(self) -> float:
        '兼容旧调用，返回输入后的角色朝向。'
        return self.character_heading_after_deg


@dataclass(frozen=True)
class HeadingAlignmentResult:
    '保存角色目标朝向闭环的最终结果。'

    success: bool
    reason: str
    target_heading_deg: float
    final_pose: PlayerMapPose | None
    final_error_deg: float | None
    steps: tuple[HeadingAlignmentStep, ...]


class CoordinateNavigationController:
    '使用角色地图姿态和独立镜头命令执行坐标移动。'

    def __init__(
            self, ctx, *, atlas: WorldMapAtlas | None = None,
            config: CoordinateNavigationConfig | None = None,
            checkpoint: Checkpoint | None = None) -> None:
        self.ctx = ctx
        self.atlas = atlas or WorldMapAtlas()
        self.config = config or CoordinateNavigationConfig()
        self.checkpoint = checkpoint

    @staticmethod
    def _pose_payload(pose: PlayerMapPose | None) -> dict:
        if pose is None:
            return {"pose": None}
        return {
            "pose": {
                "point": list(pose.point),
                "character_heading_deg": pose.character_heading_deg,
                "confidence": pose.confidence,
                "source": None if pose.source is None else list(pose.source),
                "map_scale": (
                    None if pose.map_location is None else pose.map_location.scale),
            }
        }

    def _emit(self, label: str, frame: np.ndarray | None, payload: dict) -> None:
        if self.checkpoint is not None:
            self.checkpoint(label, frame, payload)

    def _close_map(self) -> bool:
        '只在确认仍为地图时补一次 ESC，并等待角色 HUD。'
        if not self.ctx.press("esc"):
            return False
        if self.ctx.wait_until(
                rec.in_world_hud, timeout=2.2, interval=0.16,
                desc="坐标纠偏后关闭地图"):
            return True
        frame = self.ctx.grab_nowait()
        if frame is None or not rec.in_world_map(frame):
            return False
        self.ctx.log("坐标导航:首个 ESC 后仍是地图，补按一次关闭地图层")
        if not self.ctx.press("esc"):
            return False
        return bool(self.ctx.wait_until(
            rec.in_world_hud, timeout=2.2, interval=0.16,
            desc="坐标纠偏后关闭第二层地图"))

    def measure_pose(self, label: str = "pose") -> PlayerMapPose | None:
        '打开大地图取得稳定角色坐标，并在返回前安全回到 HUD。'
        navigator = WorldMapNavigator(
            self.ctx, atlas=self.atlas, name="坐标导航定位")
        if not navigator.open(timeout=6.0):
            self._emit(label, self.ctx.grab_nowait(), {"error": "map_open_failed"})
            return None
        pose = locate_stable_player_map_pose(
            self.ctx, atlas=self.atlas, timeout=self.config.pose_timeout_s)
        frame = self.ctx.grab_nowait()
        payload = self._pose_payload(pose)
        if pose is None:
            payload["error"] = "player_pose_not_found"
        self._emit(label, frame, payload)
        if not self._close_map():
            self._emit(
                f"{label}_close_failed", self.ctx.grab_nowait(),
                {**payload, "error": "map_close_failed"})
            return None
        return pose

    def _turn_pixels(self, turn_deg: float) -> int:
        '把角色转向需求换算为相对镜头拖动命令。'
        if abs(turn_deg) <= self.config.heading_tolerance_deg:
            return 0
        raw = int(round(turn_deg * self.config.camera_px_per_degree))
        return int(np.clip(raw, -self.config.max_turn_px, self.config.max_turn_px))

    def _walk_duration(self, distance: float) -> float:
        '根据剩余距离生成有上下限的短步时长。'
        estimate = distance / max(1.0, self.config.estimated_units_per_second)
        return float(np.clip(
            estimate, self.config.min_walk_s, self.config.max_walk_s))

    @staticmethod
    def _heading_error(current: float, target: float) -> float:
        '返回当前朝向到目标朝向的最短有符号角。'
        return float((target - current + 180.0) % 360.0 - 180.0)

    def align_character_heading_via_camera(
            self, target_heading_deg: float, *,
            initial_pose: PlayerMapPose | None = None,
            max_iterations: int = 6, calibration_step_s: float = 0.18,
            max_stalled_iterations: int = 2) -> HeadingAlignmentResult:
        '用镜头拖动和轻步反馈闭环校正角色朝向。'
        target = float(target_heading_deg) % 360.0
        pose = initial_pose or self.measure_pose("heading_initial_pose")
        if pose is None:
            return HeadingAlignmentResult(
                False, "initial_pose_failed", target, pose, None, ())
        steps: list[HeadingAlignmentStep] = []
        stalled = 0
        for index in range(1, max(1, int(max_iterations)) + 1):
            if self.ctx.should_stop():
                return HeadingAlignmentResult(
                    False, self.ctx.stop_reason or "stopped", target, pose,
                    self._heading_error(
                        pose.character_heading_deg, target), tuple(steps))
            error = self._heading_error(pose.character_heading_deg, target)
            if abs(error) <= self.config.heading_tolerance_deg:
                return HeadingAlignmentResult(
                    True, "aligned", target, pose, error, tuple(steps))
            turn_px = self._turn_pixels(error)
            self._emit("heading_before_turn", self.ctx.grab_nowait(), {
                "step": index,
                "character_heading_deg": pose.character_heading_deg,
                "target_heading": target,
                "error_deg": error,
                "turn_px": turn_px,
            })
            if not turn_px or not self.ctx.drag_camera(
                    turn_px, steps=max(4, min(16, abs(turn_px) // 28))):
                return HeadingAlignmentResult(
                    False, "camera_turn_failed", target, pose, error, tuple(steps))
            
            
            if not self.ctx.press("w", hold_s=max(0.10, calibration_step_s)):
                return HeadingAlignmentResult(
                    False, "heading_probe_input_failed", target, pose,
                    error, tuple(steps))
            self.ctx.sleep(0.10)
            next_pose = self.measure_pose(f"heading_after_turn_{index:02d}")
            if next_pose is None:
                return HeadingAlignmentResult(
                    False, "pose_lost_after_turn", target, pose,
                    error, tuple(steps))
            next_error = self._heading_error(
                next_pose.character_heading_deg, target)
            steps.append(HeadingAlignmentStep(
                index=index,
                character_heading_before_deg=pose.character_heading_deg,
                error_before=error,
                turn_px=turn_px,
                character_heading_after_deg=next_pose.character_heading_deg,
                error_after=next_error,
            ))
            improvement = abs(error) - abs(next_error)
            stalled = stalled + 1 if improvement < 2.0 else 0
            self._emit("heading_turn_result", None, {
                "step": index,
                "character_heading_before_deg": pose.character_heading_deg,
                "character_heading_after_deg": next_pose.character_heading_deg,
                "error_before": error,
                "error_after": next_error,
                "improvement_deg": improvement,
                "stalled": stalled,
            })
            pose = next_pose
            if abs(next_error) <= self.config.heading_tolerance_deg:
                return HeadingAlignmentResult(
                    True, "aligned", target, pose, next_error, tuple(steps))
            if stalled >= max(1, int(max_stalled_iterations)):
                return HeadingAlignmentResult(
                    False, "heading_no_progress", target, pose,
                    next_error, tuple(steps))
        final_error = self._heading_error(pose.character_heading_deg, target)
        return HeadingAlignmentResult(
            False, "heading_iteration_limit", target, pose,
            final_error, tuple(steps))

    def align_heading(
            self, target_heading_deg: float, **kwargs) -> HeadingAlignmentResult:
        '兼容旧调用，实际校正的是角色朝向。'
        return self.align_character_heading_via_camera(
            target_heading_deg, **kwargs)

    def navigate_to(
            self, target: tuple[float, float], *,
            initial_pose: PlayerMapPose | None = None) -> CoordinateNavigationResult:
        '向目标执行有限步闭环；连续无进展或定位失败时立即停止。'
        target = (float(target[0]), float(target[1]))
        started = self.ctx.logical_time()
        pose = initial_pose or self.measure_pose("initial_pose")
        if pose is None or pose.source is None:
            return CoordinateNavigationResult(
                False, "initial_pose_failed", target, pose, None, ())

        steps: list[CoordinateNavigationStep] = []
        stalled = 0
        previous_distance: float | None = None
        for index in range(1, self.config.max_iterations + 1):
            if self.ctx.should_stop():
                reason = self.ctx.stop_reason or "stopped"
                return CoordinateNavigationResult(
                    False, reason, target, pose, previous_distance, tuple(steps))
            if self.ctx.logical_time() - started >= self.config.total_timeout_s:
                return CoordinateNavigationResult(
                    False, "total_timeout", target, pose,
                    previous_distance, tuple(steps))

            vector = navigation_vector(pose, target)
            if vector is None:
                return CoordinateNavigationResult(
                    False, "pose_has_no_source", target, pose,
                    previous_distance, tuple(steps))
            if vector.distance <= self.config.arrival_tolerance:
                self._emit("arrived", None, {
                    **self._pose_payload(pose),
                    "target": list(target),
                    "distance": vector.distance,
                })
                return CoordinateNavigationResult(
                    True, "arrived", target, pose, vector.distance, tuple(steps))

            turn_px = self._turn_pixels(vector.turn_deg)
            walk_s = self._walk_duration(vector.distance)
            
            
            if index == 1:
                walk_s = max(walk_s, self.config.departure_walk_s)
            progress = (
                None if previous_distance is None
                else previous_distance - vector.distance)
            step = CoordinateNavigationStep(
                index=index,
                source=pose.source,
                character_heading_deg=pose.character_heading_deg,
                distance=vector.distance,
                bearing_deg=vector.bearing_deg,
                turn_deg=vector.turn_deg,
                turn_px=turn_px,
                walk_s=walk_s,
                progress=progress,
            )
            steps.append(step)
            self._emit("before_move", self.ctx.grab_nowait(), {
                "step": index,
                "target": list(target),
                "source": list(pose.source),
                "distance": vector.distance,
                "bearing_deg": vector.bearing_deg,
                "turn_deg": vector.turn_deg,
                "turn_px": turn_px,
                "walk_s": walk_s,
            })

            if turn_px and not self.ctx.drag_camera(
                    turn_px, steps=max(4, min(14, abs(turn_px) // 30))):
                return CoordinateNavigationResult(
                    False, "camera_turn_failed", target, pose,
                    vector.distance, tuple(steps))
            
            if not self.ctx.press("w", hold_s=walk_s):
                return CoordinateNavigationResult(
                    False, "walk_input_failed", target, pose,
                    vector.distance, tuple(steps))
            self.ctx.sleep(0.10)

            next_pose = self.measure_pose(f"pose_after_step_{index:02d}")
            if next_pose is None or next_pose.source is None:
                return CoordinateNavigationResult(
                    False, "pose_lost_after_move", target, pose,
                    vector.distance, tuple(steps))
            next_vector = navigation_vector(next_pose, target)
            if next_vector is None:
                return CoordinateNavigationResult(
                    False, "pose_has_no_source_after_move", target, next_pose,
                    None, tuple(steps))
            actual_progress = vector.distance - next_vector.distance
            steps[-1] = replace(step, progress=actual_progress)
            next_aligned = (
                abs(next_vector.turn_deg)
                <= max(25.0, self.config.heading_tolerance_deg * 2.0))
            if not next_aligned:
                
                
                stalled = 0
            elif actual_progress < self.config.minimum_progress:
                stalled += 1
            else:
                stalled = 0
            self._emit("step_result", None, {
                "step": index,
                "distance_before": vector.distance,
                "distance_after": next_vector.distance,
                "progress": actual_progress,
                "heading_aligned": next_aligned,
                "stalled": stalled,
            })
            pose = next_pose
            previous_distance = next_vector.distance
            if next_vector.distance <= self.config.arrival_tolerance:
                return CoordinateNavigationResult(
                    True, "arrived", target, pose,
                    next_vector.distance, tuple(steps))
            if stalled >= self.config.max_stalled_iterations:
                return CoordinateNavigationResult(
                    False, "no_coordinate_progress", target, pose,
                    next_vector.distance, tuple(steps))

        final_vector = navigation_vector(pose, target)
        final_distance = None if final_vector is None else final_vector.distance
        return CoordinateNavigationResult(
            False, "iteration_limit", target, pose, final_distance, tuple(steps))
