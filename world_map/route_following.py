'根据连续地图姿态计算稀疏航点跟随指令。'
from __future__ import annotations

from dataclasses import dataclass
from math import atan2, degrees, hypot

from .waypoints import (
    MovementMode,
    RoutePlan,
    RouteWaypoint,
    WaypointKind,
    select_lookahead_waypoint,
)


@dataclass(frozen=True)
class RouteGuidance:
    '保存路线执行器下一步需要完成的纯计算结果。'

    waypoint_index: int
    waypoint: RouteWaypoint
    distance: float
    bearing_deg: float
    character_turn_deg: float | None
    should_move: bool
    movement: MovementMode


class RouteFollower:
    '维护航点进度，不直接截图或发送输入。'

    def __init__(
            self, route: RoutePlan, *, start_index: int = -1,
            lookahead: int = 20, heading_tolerance_deg: float = 12.0) -> None:
        self.route = route
        self.current_index = max(-1, int(start_index))
        self.lookahead = max(1, int(lookahead))
        self.heading_tolerance_deg = max(1.0, float(heading_tolerance_deg))
        self.finished = self.current_index >= len(route.waypoints) - 1

    def update(
            self, source: tuple[float, float],
            character_heading_deg: float | None,
            ) -> RouteGuidance | None:
        '根据当前位置推进已到达点并返回下一条导航指令。'
        if self.finished:
            return None
        while True:
            selected = select_lookahead_waypoint(
                source, self.route, self.current_index,
                lookahead=self.lookahead)
            if selected is None:
                self.finished = True
                return None
            waypoint = selected.waypoint
            distance, bearing = self._vector(source, waypoint.source)
            turn = (
                None if character_heading_deg is None
                else self._heading_error(character_heading_deg, bearing))

            if waypoint.kind is WaypointKind.ORIENTATION:
                if turn is not None and abs(turn) <= self.heading_tolerance_deg:
                    self.current_index = selected.index
                    if self.current_index >= len(self.route.waypoints) - 1:
                        self.finished = True
                        return None
                    continue
                return RouteGuidance(
                    selected.index, waypoint, distance, bearing, turn,
                    False, waypoint.movement)

            if distance <= waypoint.arrival_tolerance():
                self.current_index = selected.index
                if self.current_index >= len(self.route.waypoints) - 1:
                    self.finished = True
                    return None
                continue
            return RouteGuidance(
                selected.index, waypoint, distance, bearing, turn,
                waypoint.kind is not WaypointKind.TELEPORT,
                waypoint.movement)

    @staticmethod
    def _vector(
            source: tuple[float, float], target: tuple[float, float],
            ) -> tuple[float, float]:
        dx = float(target[0]) - float(source[0])
        dy = float(target[1]) - float(source[1])
        distance = float(hypot(dx, dy))
        bearing = float((degrees(atan2(dx, -dy)) + 360.0) % 360.0)
        return distance, bearing

    @staticmethod
    def _heading_error(current: float, target: float) -> float:
        return float((target - current + 180.0) % 360.0 - 180.0)
