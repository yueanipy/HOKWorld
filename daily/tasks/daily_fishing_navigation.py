'使用小地图航点闭环完成云根镇云舟栈到水岸的移动。'
from __future__ import annotations

import time
from collections import deque
from math import atan2, degrees, hypot

import numpy as np

from runtime_guard import dev_log
from world_map.core import WorldMapAtlas
from world_map.minimap import MiniMapPoseRecognizer, MiniMapPoseTracker
from world_map.route_following import RouteFollower
from world_map.waypoints import (
    MovementMode,
    RoutePlan,
    RouteWaypoint,
    WaypointKind,
)



YUNGEN_PIER_TO_SHORE = RoutePlan(
    name="云根镇云舟栈至秋水湖岸",
    region="织梦原野",
    waypoints=(
        RouteWaypoint(
            source=(470110.0, -538650.0),
            kind=WaypointKind.PATH,
            movement=MovementMode.RUN,
            tolerance=650.0,
            name="渡石脱离点",
        ),
        RouteWaypoint(
            source=(469650.0, -536450.0),
            kind=WaypointKind.PATH,
            movement=MovementMode.RUN,
            tolerance=720.0,
            name="下坡中段",
        ),
        RouteWaypoint(
            source=(468890.0, -534700.0),
            kind=WaypointKind.TARGET,
            movement=MovementMode.WALK,
            tolerance=260.0,
            action="确认水岸",
            name="秋水湖岸",
        ),
    ),
)


class FishingShoreNavigator:
    '复用小地图图谱，按录像航点闭环移动并确认水岸边界。'

    STRAFE_S = 0.8
    INITIAL_TURN_PX = 300
    SAFETY_TIMEOUT_S = 15.0
    POSE_LOST_TIMEOUT_S = 2.2
    STALL_TIMEOUT_S = 2.4
    MIN_PROGRESS = 150.0
    CORRECTION_INTERVAL_S = 0.65
    HEADING_TOLERANCE_DEG = 11.0
    MAX_CORRECTION_PX = 45
    CORRECTION_MIN_TARGET_DISTANCE = 1500.0
    TARGET_STREAK = 2
    
    
    TARGET_PROGRESS = 0.93
    MAX_TARGET_CROSS_TRACK = 480.0

    def __init__(
            self, ctx, *, atlas: WorldMapAtlas | None = None,
            recognizer: MiniMapPoseRecognizer | None = None) -> None:
        self.ctx = ctx
        self.atlas = atlas or WorldMapAtlas()
        self.recognizer = recognizer or MiniMapPoseRecognizer(self.atlas)
        self.tracker = MiniMapPoseTracker(
            window=5, required=2, max_atlas_jump=85.0)
        self.follower = RouteFollower(
            YUNGEN_PIER_TO_SHORE, lookahead=2,
            heading_tolerance_deg=self.HEADING_TOLERANCE_DEG)

    @staticmethod
    def _bearing(
            source: tuple[float, float],
            target: tuple[float, float]) -> float:
        dx = target[0] - source[0]
        dy = target[1] - source[1]
        return float((degrees(atan2(dx, -dy)) + 360.0) % 360.0)

    @staticmethod
    def _heading_error(current: float, target: float) -> float:
        return float((target - current + 180.0) % 360.0 - 180.0)

    @staticmethod
    def _route_progress(
            source: tuple[float, float]) -> tuple[float, float]:
        '返回角色沿整条路线的投影进度和横向偏差。'
        start = YUNGEN_PIER_TO_SHORE.waypoints[0].source
        target = YUNGEN_PIER_TO_SHORE.waypoints[-1].source
        vx = target[0] - start[0]
        vy = target[1] - start[1]
        wx = source[0] - start[0]
        wy = source[1] - start[1]
        length_sq = vx * vx + vy * vy
        length = max(1.0, hypot(vx, vy))
        progress = (wx * vx + wy * vy) / max(1.0, length_sq)
        cross_track = abs(vx * wy - vy * wx) / length
        return float(progress), float(cross_track)

    def run(self) -> bool:
        '先横移脱离渡石，再按小地图坐标闭环到水岸。'
        ctx = self.ctx
        if not ctx.walk("d", self.STRAFE_S):
            return False
        ctx.sleep(0.20)
        ctx.log(
            f"每日钓鱼:已向右移动 {self.STRAFE_S:.1f} 秒脱离渡石，"
            "开始小地图航点寻岸")

        started = ctx.logical_time()
        last_pose_at = started
        last_progress_at = started
        last_correction_at = started
        best_distance = float("inf")
        target_streak = 0
        positions: deque[tuple[float, float]] = deque(maxlen=9)
        correction_errors: deque[float] = deque(maxlen=2)
        camera_turned = False

        while (
                ctx.logical_time() - started < self.SAFETY_TIMEOUT_S
                and not ctx.should_stop()):
            if ctx.paused:
                time.sleep(0.08)
                continue
            if not ctx.foreground():
                ctx.log("每日钓鱼:小地图寻岸时游戏失去前台，已停止输入")
                return False

            with ctx.hold("w") as held:
                if not held:
                    return False
                if not camera_turned:
                    if not ctx.drag_camera(self.INITIAL_TURN_PX, steps=20):
                        return False
                    camera_turned = True

                while (
                        ctx.logical_time() - started < self.SAFETY_TIMEOUT_S
                        and not ctx.should_stop()):
                    if not ctx.action_ready():
                        break
                    frame = ctx.grab_nowait()
                    if frame is None:
                        time.sleep(0.03)
                        continue

                    pose = self.tracker.update(self.recognizer.detect(frame))
                    now = ctx.logical_time()
                    if pose is None or pose.source is None:
                        if now - last_pose_at >= self.POSE_LOST_TIMEOUT_S:
                            ctx.log("每日钓鱼:连续无法定位小地图，已停止盲走")
                            return False
                        time.sleep(0.06)
                        continue

                    last_pose_at = now
                    source = pose.source
                    positions.append(source)
                    target = YUNGEN_PIER_TO_SHORE.waypoints[-1].source
                    target_distance = hypot(
                        target[0] - source[0], target[1] - source[1])
                    route_progress, cross_track = self._route_progress(source)
                    guidance = self.follower.update(source, None)

                    if target_distance + self.MIN_PROGRESS < best_distance:
                        best_distance = target_distance
                        last_progress_at = now
                    elif now - last_progress_at >= self.STALL_TIMEOUT_S:
                        ctx.log(
                            "每日钓鱼:小地图坐标长时间没有接近水岸，"
                            "已停止路线")
                        return False

                    target_streak = (
                        target_streak + 1
                        if (
                            route_progress >= self.TARGET_PROGRESS
                            and cross_track <= self.MAX_TARGET_CROSS_TRACK)
                        else 0)
                    dev_log(
                        "[daily fishing route] "
                        f"source=({source[0]:.1f},{source[1]:.1f}) "
                        f"target_distance={target_distance:.1f} "
                        f"progress={route_progress:.3f} "
                        f"cross_track={cross_track:.1f} "
                        f"waypoint={None if guidance is None else guidance.waypoint.name} "
                        f"streak={target_streak}")
                    if target_streak >= self.TARGET_STREAK:
                        break

                    if (
                            guidance is not None
                            and target_distance
                            >= self.CORRECTION_MIN_TARGET_DISTANCE
                            and len(positions) >= 7
                            and now - last_correction_at
                            >= self.CORRECTION_INTERVAL_S):
                        samples = np.float64(positions)
                        previous_values = np.median(samples[:3], axis=0)
                        current_values = np.median(samples[-3:], axis=0)
                        previous = (
                            float(previous_values[0]),
                            float(previous_values[1]))
                        current = (
                            float(current_values[0]),
                            float(current_values[1]))
                        moved = hypot(
                            current[0] - previous[0],
                            current[1] - previous[1])
                        if moved >= 220.0:
                            movement_bearing = self._bearing(
                                previous, current)
                            desired_bearing = self._bearing(
                                source, guidance.waypoint.source)
                            error = self._heading_error(
                                movement_bearing, desired_bearing)
                            if abs(error) >= self.HEADING_TOLERANCE_DEG:
                                correction_errors.append(error)
                            else:
                                correction_errors.clear()
                            same_direction = (
                                len(correction_errors) == 2
                                and correction_errors[0]
                                * correction_errors[1] > 0)
                            if same_direction:
                                stable_error = float(np.mean(
                                    correction_errors))
                                pixels = int(np.clip(
                                    round(stable_error * 1.8),
                                    -self.MAX_CORRECTION_PX,
                                    self.MAX_CORRECTION_PX))
                                if not ctx.drag_camera(
                                        pixels,
                                        steps=max(4, abs(pixels) // 12)):
                                    return False
                                dev_log(
                                    "[daily fishing route] correction "
                                    f"movement={movement_bearing:.1f} "
                                    f"desired={desired_bearing:.1f} "
                                    f"error={stable_error:.1f} px={pixels}")
                                correction_errors.clear()
                            last_correction_at = now
                    time.sleep(0.06)

            if target_streak >= self.TARGET_STREAK:
                break
            if not ctx.paused and not ctx.action_ready():
                return False

        if target_streak < self.TARGET_STREAK:
            ctx.log("每日钓鱼:小地图航点寻岸达到安全上限，未确认终点")
            return False
        ctx.sleep(0.85)
        ctx.log("每日钓鱼:小地图图谱终点已连续确认，寻岸完成")
        return True
