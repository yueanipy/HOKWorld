'牧场内部小地图相对位移跟踪。'
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from world_map.minimap import MiniMapPoseRecognizer
from world_map.pose import PlayerMapPoseRecognizer


@dataclass(frozen=True)
class RanchLocalOffset:
    '保存相对起点的小地图像素位移。'

    dx: float
    dy: float
    confidence: float
    inliers: int
    matches: int


@dataclass(frozen=True)
class RanchRegionBand:
    '保存牧场区域在传送起点局部坐标中的范围。'

    min_x: float
    max_x: float
    min_y: float
    max_y: float

    def contains(
            self,
            offset: RanchLocalOffset,
            *,
            tolerance: float = 0.35,
            ) -> bool:
        return (
            self.min_x - tolerance <= offset.dx <= self.max_x + tolerance
            and self.min_y - tolerance <= offset.dy <= self.max_y + tolerance
        )


class RanchLocalRouteTracker:
    '使用牧场小地图地形跟踪内部路线，不依赖公共大地图图谱。'

    _CENTER = (
        MiniMapPoseRecognizer.CENTER[0] - MiniMapPoseRecognizer.CROP[0],
        MiniMapPoseRecognizer.CENTER[1] - MiniMapPoseRecognizer.CROP[1],
    )
    _MATCH_RADIUS = 89
    _ARROW_EXCLUDE_RADIUS = 25
    REGION_BANDS = {
        1: RanchRegionBand(-7.2, -1.55, -10.2, -6.7),
        2: RanchRegionBand(1.05, 7.98, -10.2, -6.7),
        3: RanchRegionBand(2.95, 11.66, -14.5, -12.0),
        4: RanchRegionBand(-8.86, -0.55, -14.2, -11.9),
    }

    def __init__(self, anchor_frame: np.ndarray) -> None:
        anchor = MiniMapPoseRecognizer.minimap_crop(anchor_frame)
        if anchor is None:
            raise ValueError("无法读取牧场起点小地图")
        self._mask = self._build_mask(anchor.shape[:2])
        self._orb = cv2.ORB_create(nfeatures=800, fastThreshold=7)
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self._anchor_points, self._anchor_descriptors = (
            self._orb.detectAndCompute(
                cv2.cvtColor(anchor, cv2.COLOR_BGR2GRAY),
                self._mask,
            )
        )
        if (
            self._anchor_descriptors is None
            or len(self._anchor_points) < 30
        ):
            raise ValueError("牧场起点小地图特征不足")

    @classmethod
    def _build_mask(cls, shape: tuple[int, int]) -> np.ndarray:
        mask = np.zeros(shape, np.uint8)
        cv2.circle(mask, cls._CENTER, cls._MATCH_RADIUS, 255, -1)
        cv2.circle(mask, cls._CENTER, cls._ARROW_EXCLUDE_RADIUS, 0, -1)
        return mask

    def locate(self, frame: np.ndarray) -> RanchLocalOffset | None:
        '返回当前画面相对起点的小地图平移量。'
        crop = MiniMapPoseRecognizer.minimap_crop(frame)
        if crop is None:
            return None
        points, descriptors = self._orb.detectAndCompute(
            cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY),
            self._mask,
        )
        if descriptors is None or len(points) < 20:
            return None

        pairs = self._matcher.knnMatch(
            self._anchor_descriptors,
            descriptors,
            k=2,
        )
        matches = [
            first
            for pair in pairs
            if len(pair) == 2
            for first, second in [pair]
            if first.distance < 0.78 * second.distance
        ]
        if len(matches) < 12:
            return None

        source = np.float32([
            self._anchor_points[item.queryIdx].pt for item in matches
        ])
        target = np.float32([points[item.trainIdx].pt for item in matches])
        matrix, selected = cv2.estimateAffinePartial2D(
            source,
            target,
            method=cv2.RANSAC,
            ransacReprojThreshold=2.0,
            maxIters=2000,
            confidence=0.995,
        )
        if matrix is None or selected is None:
            return None
        valid = selected.ravel().astype(bool)
        inliers = int(np.count_nonzero(valid))
        if inliers < 10 or inliers / len(matches) < 0.45:
            return None

        scale = float(np.hypot(matrix[0, 0], matrix[0, 1]))
        rotation = float(np.degrees(np.arctan2(matrix[0, 1], matrix[0, 0])))
        if not 0.96 <= scale <= 1.04 or abs(rotation) > 4.0:
            return None

        projected = cv2.transform(
            source.reshape(1, -1, 2),
            matrix,
        ).reshape(-1, 2)
        error = float(np.median(
            np.linalg.norm(projected - target, axis=1)[valid]
        ))
        if error > 2.5:
            return None

        confidence = float(np.clip(
            0.35
            + min(0.35, inliers * 0.012)
            + 0.18 * inliers / max(1, len(matches))
            + 0.12 * max(0.0, 1.0 - error / 2.5),
            0.0,
            1.0,
        ))
        return RanchLocalOffset(
            dx=float(matrix[0, 2]),
            dy=float(matrix[1, 2]),
            confidence=confidence,
            inliers=inliers,
            matches=len(matches),
        )

    @classmethod
    def region_from_offset(
            cls,
            offset: RanchLocalOffset | None,
            ) -> int | None:
        '根据传送起点的局部位移判断当前牧场区域。'
        if offset is None:
            return None
        for region, band in cls.REGION_BANDS.items():
            if band.contains(offset):
                return region
        return None

    @classmethod
    def region_boundary_reached(
            cls,
            offset: RanchLocalOffset | None,
            region: int,
            key: str,
            ) -> bool:
        '判断横移方向是否到达当前区域录制边界。'
        if offset is None:
            return False
        band = cls.REGION_BANDS.get(region)
        if band is None:
            return False
        tolerance = 0.15
        if key == "d":
            return offset.dx >= band.max_x - tolerance
        if key == "a":
            return offset.dx <= band.min_x + tolerance
        raise ValueError(f"不支持的牧场横移方向: {key}")

    @classmethod
    def character_heading(cls, frame: np.ndarray) -> float | None:
        '读取角色箭头朝向；该朝向不代表镜头朝向。'
        crop = MiniMapPoseRecognizer.minimap_crop(frame)
        if crop is None:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        white = cv2.inRange(
            hsv,
            np.array([0, 0, 185], np.uint8),
            np.array([179, 85, 255], np.uint8),
        )
        count, labels, stats, centers = cv2.connectedComponentsWithStats(white)
        expected = np.float64(cls._CENTER)
        candidates: list[tuple[float, float]] = []
        for index in range(1, count):
            x, y, width, height, area = (
                int(value) for value in stats[index]
            )
            center = centers[index].astype(np.float64)
            distance = float(np.linalg.norm(center - expected))
            if distance > 17.0:
                continue
            if not (
                180 <= area <= 1050
                and 15 <= width <= 55
                and 15 <= height <= 70
            ):
                continue
            component = (
                labels[y:y + height, x:x + width] == index
            ).astype(np.uint8)
            heading = PlayerMapPoseRecognizer._heading(component)
            if heading is not None:
                candidates.append((distance, float(heading)))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])[1]
