'定义公共路线航点和前视选择规则。'
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import hypot


class WaypointKind(str, Enum):
    '区分路线点的到达精度和行为。'

    TELEPORT = "teleport"
    PATH = "path"
    TARGET = "target"
    ORIENTATION = "orientation"


class MovementMode(str, Enum):
    '描述航点之间允许使用的移动状态。'

    WALK = "walk"
    RUN = "run"
    CLIMB = "climb"
    SWIM = "swim"
    GLIDE = "glide"


@dataclass(frozen=True)
class RouteWaypoint:
    '保存一个地表坐标航点。'

    source: tuple[float, float]
    kind: WaypointKind = WaypointKind.PATH
    movement: MovementMode = MovementMode.WALK
    tolerance: float | None = None
    action: str = ""
    name: str = ""

    @property
    def required(self) -> bool:
        '返回该点是否禁止被前视选择跳过。'
        return self.kind is not WaypointKind.PATH or bool(self.action)

    def arrival_tolerance(self) -> float:
        '返回点位类型对应的默认到达半径。'
        if self.tolerance is not None:
            return max(1.0, float(self.tolerance))
        if self.kind is WaypointKind.PATH:
            return 2200.0
        if self.kind is WaypointKind.ORIENTATION:
            return 3200.0
        return 900.0


@dataclass(frozen=True)
class RoutePlan:
    '保存可复用的稀疏航点路线。'

    name: str
    waypoints: tuple[RouteWaypoint, ...]
    region: str = ""

    def __post_init__(self) -> None:
        if not self.waypoints:
            raise ValueError("路线至少需要一个航点")


@dataclass(frozen=True)
class LookaheadSelection:
    '保存一次前视航点选择结果。'

    index: int
    waypoint: RouteWaypoint
    distance: float
    skipped_path_points: int


@dataclass(frozen=True)
class RouteChoice:
    '保存当前位置对应的最近可接入路线。'

    route: RoutePlan
    entry_index: int
    distance: float


def select_lookahead_waypoint(
        current: tuple[float, float], route: RoutePlan, current_index: int,
        *, lookahead: int = 20) -> LookaheadSelection | None:
    '在不跳过必经点的前提下选择后续最近航点。'
    if current_index >= len(route.waypoints) - 1:
        return None
    start = max(-1, int(current_index)) + 1
    stop = min(len(route.waypoints), start + max(1, int(lookahead)))
    candidates: list[tuple[int, RouteWaypoint, float]] = []
    for index in range(start, stop):
        waypoint = route.waypoints[index]
        distance = hypot(
            waypoint.source[0] - float(current[0]),
            waypoint.source[1] - float(current[1]))
        candidates.append((index, waypoint, distance))
        if waypoint.required:
            break
    if not candidates:
        return None
    index, waypoint, distance = min(candidates, key=lambda item: item[2])
    return LookaheadSelection(
        index=index,
        waypoint=waypoint,
        distance=float(distance),
        skipped_path_points=max(0, index - start),
    )


def choose_nearest_route_entry(
        current: tuple[float, float], routes: tuple[RoutePlan, ...],
        *, max_entry_points: int = 4) -> RouteChoice | None:
    '从已验证路线开头选择距离当前位置最近的接入点。'
    choices: list[RouteChoice] = []
    for route in routes:
        limit = min(len(route.waypoints), max(1, int(max_entry_points)))
        for index, waypoint in enumerate(route.waypoints[:limit]):
            if index > 0 and waypoint.required:
                break
            distance = hypot(
                waypoint.source[0] - float(current[0]),
                waypoint.source[1] - float(current[1]))
            choices.append(RouteChoice(route, index, float(distance)))
    return min(choices, key=lambda item: item.distance) if choices else None
