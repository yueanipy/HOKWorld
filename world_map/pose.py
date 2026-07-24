'识别地上大地图中的角色位置箭头。'
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from math import atan2, degrees

import cv2
import numpy as np

from fishing.template_bank import normalize
from .core import MapLocation, WorldMapAtlas


@dataclass(frozen=True)
class PlayerArrowCandidate:
    '保存一个大地图角色箭头候选。'

    point: tuple[float, float]
    character_heading_deg: float
    confidence: float
    bbox: tuple[int, int, int, int]
    area: int
    cyan_pixels: int
    solidity: float
    shape_distance: float

    @property
    def heading_deg(self) -> float:
        '兼容旧调用，箭头角度始终是角色朝向。'
        return self.character_heading_deg


@dataclass(frozen=True)
class PlayerMapPose:
    '保存角色的屏幕位置、角色朝向和可选地表坐标。'

    point: tuple[float, float]
    character_heading_deg: float
    confidence: float
    source: tuple[float, float] | None
    map_location: MapLocation | None

    @property
    def heading_deg(self) -> float:
        '兼容旧调用，地图姿态不包含镜头朝向。'
        return self.character_heading_deg


@dataclass(frozen=True)
class NavigationVector:
    '保存从当前角色坐标指向目标坐标的二维导航量。'

    distance: float
    bearing_deg: float
    turn_deg: float


class PlayerMapPoseRecognizer:
    '使用颜色和轮廓识别固定渲染尺寸的青白角色箭头。'

    
    
    SEARCH_ROI = (0.10, 0.18, 0.86, 0.82)
    WHITE_LOWER = np.array([0, 0, 195], np.uint8)
    WHITE_UPPER = np.array([179, 70, 255], np.uint8)
    CYAN_LOWER = np.array([76, 50, 115], np.uint8)
    CYAN_UPPER = np.array([108, 255, 255], np.uint8)
    
    
    ARROW_CONTOUR = np.array(
        [[0, 0], [26, 45], [28, 23], [51, 15]], np.int32).reshape(-1, 1, 2)

    def candidates(self, frame: np.ndarray) -> list[PlayerArrowCandidate]:
        '返回按置信度排列的角色箭头候选。'
        if frame is None or frame.size == 0:
            return []
        image = normalize(frame)
        height, width = image.shape[:2]
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        white = cv2.inRange(hsv, self.WHITE_LOWER, self.WHITE_UPPER)
        count, labels, stats, centers = cv2.connectedComponentsWithStats(white)
        x_min, y_min, x_max, y_max = self.SEARCH_ROI
        found: list[PlayerArrowCandidate] = []

        for index in range(1, count):
            x, y, box_w, box_h, area = (int(value) for value in stats[index])
            cx, cy = (float(value) for value in centers[index])
            if not (x_min * width <= cx <= x_max * width
                    and y_min * height <= cy <= y_max * height):
                continue
            if not (430 <= area <= 1450 and 34 <= box_w <= 68 and 34 <= box_h <= 72):
                continue
            fill = area / float(box_w * box_h)
            if not 0.25 <= fill <= 0.56:
                continue

            component = (labels[y:y + box_h, x:x + box_w] == index).astype(np.uint8)
            contours, _ = cv2.findContours(
                component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            contour = max(contours, key=cv2.contourArea)
            contour_area = float(cv2.contourArea(contour))
            hull = cv2.convexHull(contour)
            hull_area = float(cv2.contourArea(hull))
            if hull_area <= 0.0:
                continue
            solidity = contour_area / hull_area
            if not 0.44 <= solidity <= 0.84:
                continue
            hull_perimeter = cv2.arcLength(hull, True)
            hull_vertices = len(cv2.approxPolyDP(hull, 0.065 * hull_perimeter, True))
            if not 3 <= hull_vertices <= 5:
                continue
            shape_distance = float(cv2.matchShapes(
                self.ARROW_CONTOUR, contour, cv2.CONTOURS_MATCH_I1, 0.0))
            if shape_distance > 0.29:
                continue

            margin = 14
            px0, py0 = max(0, x - margin), max(0, y - margin)
            px1 = min(width, x + box_w + margin)
            py1 = min(height, y + box_h + margin)
            cyan = cv2.inRange(
                hsv[py0:py1, px0:px1], self.CYAN_LOWER, self.CYAN_UPPER)
            cyan_pixels = int(cv2.countNonZero(cyan))
            if cyan_pixels < 260:
                continue

            heading = self._heading(component)
            if heading is None:
                continue
            confidence = self._confidence(
                area, box_w, box_h, fill, solidity, hull_vertices,
                cyan_pixels, shape_distance)
            found.append(PlayerArrowCandidate(
                point=(cx / width, cy / height),
                character_heading_deg=heading,
                confidence=confidence,
                bbox=(x, y, box_w, box_h),
                area=area,
                cyan_pixels=cyan_pixels,
                solidity=solidity,
                shape_distance=shape_distance,
            ))

        found.sort(key=lambda item: item.confidence, reverse=True)
        return found

    def detect(
            self, frame: np.ndarray, *, atlas: WorldMapAtlas | None = None,
            location: MapLocation | None = None) -> PlayerMapPose | None:
        '识别最佳箭头，并在提供图谱时换算为地表坐标。'
        found = self.candidates(frame)
        if not found:
            return None
        best = found[0]
        
        if len(found) > 1 and best.confidence - found[1].confidence < 0.035:
            return None

        map_location = location
        source = None
        if atlas is not None:
            image = normalize(frame)
            map_location = map_location or atlas.locate(image)
            if map_location is None:
                return None
            source = atlas.screen_to_source_point(
                image, best.point, location=map_location)
        return PlayerMapPose(
            point=best.point,
            character_heading_deg=best.character_heading_deg,
            confidence=best.confidence,
            source=source,
            map_location=map_location,
        )

    @staticmethod
    def _heading(component: np.ndarray) -> float | None:
        '以箭头尖端方向计算北为零、顺时针增加的角度。'
        ys, xs = np.nonzero(component)
        if len(xs) < 30:
            return None
        points = np.column_stack((xs, ys)).astype(np.float32)
        mean, eigenvectors, _ = cv2.PCACompute2(points, mean=None)
        axis = eigenvectors[0].astype(np.float64)
        centered = points.astype(np.float64) - mean.reshape(1, 2)
        projection = centered @ axis
        low, high = np.quantile(projection, [0.18, 0.82])

        
        normal = np.array([-axis[1], axis[0]], np.float64)
        cross = centered @ normal
        low_width = float(np.ptp(cross[projection <= low]))
        high_width = float(np.ptp(cross[projection >= high]))
        if abs(low_width - high_width) < 1.0:
            return None
        direction = -axis if low_width < high_width else axis
        return float((degrees(atan2(direction[0], -direction[1])) + 360.0) % 360.0)

    @staticmethod
    def _confidence(
            area: int, width: int, height: int, fill: float, solidity: float,
            hull_vertices: int, cyan_pixels: int, shape_distance: float) -> float:
        '根据实录箭头的几何范围计算候选置信度。'
        area_score = max(0.0, 1.0 - abs(area - 930.0) / 650.0)
        size_score = max(0.0, 1.0 - (abs(width - 50.0) + abs(height - 52.0)) / 45.0)
        fill_score = max(0.0, 1.0 - abs(fill - 0.38) / 0.22)
        solidity_score = max(0.0, 1.0 - abs(solidity - 0.64) / 0.28)
        vertex_score = 1.0 if hull_vertices in (3, 4) else 0.78
        cyan_score = min(1.0, cyan_pixels / 900.0)
        shape_score = max(0.0, 1.0 - shape_distance / 0.29)
        return float(np.clip(
            0.17 * area_score + 0.15 * size_score + 0.12 * fill_score
            + 0.16 * solidity_score + 0.10 * vertex_score + 0.13 * cyan_score
            + 0.17 * shape_score,
            0.0, 1.0))


class PlayerMapPoseTracker:
    '使用连续地图帧确认角色坐标，拒绝单帧地图图标误检。'

    def __init__(self, *, window: int = 5, required: int = 3,
                 source_tolerance: float = 2500.0,
                 heading_tolerance: float = 28.0) -> None:
        self.window = max(3, int(window))
        self.required = min(self.window, max(2, int(required)))
        self.source_tolerance = float(source_tolerance)
        self.heading_tolerance = float(heading_tolerance)
        self._poses: deque[PlayerMapPose] = deque(maxlen=self.window)

    def reset(self) -> None:
        '清除地图关闭、缩放动画或角色移动前的历史。'
        self._poses.clear()

    def update(self, pose: PlayerMapPose | None) -> PlayerMapPose | None:
        '加入新识别结果，并在位置和方向稳定后返回融合姿态。'
        if pose is None or pose.source is None:
            return None
        self._poses.append(pose)
        if len(self._poses) < self.required:
            return None
        points = np.asarray([item.source for item in self._poses], np.float64)
        center = np.median(points, axis=0)
        distances = np.linalg.norm(points - center, axis=1)
        accepted = distances <= self.source_tolerance
        if int(np.count_nonzero(accepted)) < self.required:
            
            self._poses.clear()
            self._poses.append(pose)
            return None
        if not bool(accepted[-1]):
            
            self._poses.clear()
            self._poses.append(pose)
            return None
        selected = [item for item, keep in zip(self._poses, accepted) if keep]
        angles = np.radians([item.character_heading_deg for item in selected])
        mean_angle = float(np.arctan2(np.mean(np.sin(angles)), np.mean(np.cos(angles))))
        heading = (degrees(mean_angle) + 360.0) % 360.0
        differences = np.abs(
            (np.degrees(angles) - heading + 180.0) % 360.0 - 180.0)
        if float(np.max(differences)) > self.heading_tolerance:
            return None
        screen = np.median(np.asarray([item.point for item in selected]), axis=0)
        confidence = float(np.mean([item.confidence for item in selected]))
        return PlayerMapPose(
            point=(float(screen[0]), float(screen[1])),
            character_heading_deg=float(heading),
            confidence=confidence,
            source=(float(center[0]), float(center[1])),
            map_location=pose.map_location,
        )


def detect_player_map_pose(
        frame: np.ndarray, *, atlas: WorldMapAtlas | None = None,
        location: MapLocation | None = None) -> PlayerMapPose | None:
    '使用默认识别器获取大地图角色位置。'
    return PlayerMapPoseRecognizer().detect(frame, atlas=atlas, location=location)


def locate_stable_player_map_pose(
        ctx, *, atlas: WorldMapAtlas | None = None, timeout: float = 4.0,
        sample_interval: float = 0.12) -> PlayerMapPose | None:
    '从已经打开的大地图连续取帧并返回稳定角色姿态。'
    atlas = atlas or WorldMapAtlas()
    recognizer = PlayerMapPoseRecognizer()
    tracker = PlayerMapPoseTracker()
    deadline = ctx.logical_time() + max(0.0, float(timeout))
    while ctx.logical_time() < deadline and not ctx.should_stop():
        frame = ctx.grab()
        pose = recognizer.detect(frame, atlas=atlas) if frame is not None else None
        stable = tracker.update(pose)
        if stable is not None:
            return stable
        ctx.sleep(max(0.02, float(sample_interval)))
    return None


def navigation_vector(
        pose: PlayerMapPose, target: tuple[float, float]) -> NavigationVector | None:
    '计算距离、目标方位及角色朝向需要改变的角度。'
    if pose.source is None:
        return None
    dx = float(target[0]) - pose.source[0]
    dy = float(target[1]) - pose.source[1]
    distance = float(np.hypot(dx, dy))
    bearing = float((degrees(atan2(dx, -dy)) + 360.0) % 360.0)
    turn = float(
        (bearing - pose.character_heading_deg + 180.0) % 360.0 - 180.0)
    return NavigationVector(distance=distance, bearing_deg=bearing, turn_deg=turn)
