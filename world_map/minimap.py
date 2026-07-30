'从角色 HUD 小地图识别连续位置与角色朝向。'
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import atan2, degrees

import cv2
import numpy as np

from fishing.template_bank import normalize
from .core import WorldMapAtlas
from .pose import PlayerMapPoseRecognizer


@dataclass(frozen=True)
class MiniMapPose:
    '保存小地图中的角色坐标和角色朝向。'

    source: tuple[float, float] | None
    atlas: tuple[float, float] | None
    character_heading_deg: float | None
    confidence: float
    inliers: int
    match_count: int
    reprojection_error: float | None
    atlas_scale: float | None


class MiniMapPoseRecognizer:
    '使用小地图地形特征和中心箭头估计角色姿态。'

    CENTER = (112, 107)
    RADIUS = 96
    MATCH_RADIUS = 89
    ARROW_EXCLUDE_RADIUS = 25
    CROP = (10, 8, 214, 210)

    def __init__(self, atlas: WorldMapAtlas | None = None) -> None:
        self.atlas = atlas or WorldMapAtlas()
        self._sift = cv2.SIFT_create(
            nfeatures=900, contrastThreshold=0.02, edgeThreshold=12)

    @classmethod
    def minimap_crop(cls, frame: np.ndarray) -> np.ndarray | None:
        '返回1920基准下的小地图矩形。'
        if frame is None or frame.size == 0:
            return None
        image = normalize(frame)
        x0, y0, x1, y1 = cls.CROP
        if image.shape[1] < x1 or image.shape[0] < y1:
            return None
        return image[y0:y1, x0:x1]

    def detect(
            self, frame: np.ndarray, *, locate: bool = True,
            ) -> MiniMapPose | None:
        '识别小地图；可关闭较重的全局坐标匹配。'
        crop = self.minimap_crop(frame)
        if crop is None:
            return None
        heading = self._character_heading(crop)
        location = self._locate(crop) if locate else None
        if location is None:
            if heading is None:
                return None
            return MiniMapPose(
                source=None, atlas=None, character_heading_deg=heading,
                confidence=0.35, inliers=0, match_count=0,
                reprojection_error=None, atlas_scale=None)

        atlas_point, inliers, matches, error, scale, confidence = location
        source = self.atlas.atlas_to_source_point(atlas_point)
        return MiniMapPose(
            source=source,
            atlas=atlas_point,
            character_heading_deg=heading,
            confidence=confidence if heading is not None else confidence * 0.88,
            inliers=inliers,
            match_count=matches,
            reprojection_error=error,
            atlas_scale=scale,
        )

    def _locate(
            self, crop: np.ndarray,
            ) -> tuple[tuple[float, float], int, int, float, float, float] | None:
        '把小地图地形仿射匹配到公共图谱。'
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        center = (
            self.CENTER[0] - self.CROP[0],
            self.CENTER[1] - self.CROP[1],
        )
        mask = np.zeros((height, width), np.uint8)
        cv2.circle(mask, center, self.MATCH_RADIUS, 255, -1)
        cv2.circle(mask, center, self.ARROW_EXCLUDE_RADIUS, 0, -1)
        keypoints, descriptors = self._sift.detectAndCompute(gray, mask)
        if descriptors is None or len(keypoints) < 20:
            return None
        matches = self.atlas.match_feature_descriptors(descriptors, ratio=0.75)
        if len(matches) < 6:
            return None
        source_points = np.float32([
            [
                keypoints[query_index].pt[0] + self.CROP[0],
                keypoints[query_index].pt[1] + self.CROP[1],
            ]
            for query_index, _, _ in matches
        ])
        atlas_points = np.float32([point for _, point, _ in matches])
        matrix, mask_inliers = cv2.estimateAffinePartial2D(
            source_points, atlas_points, method=cv2.RANSAC,
            ransacReprojThreshold=6.0, maxIters=3000, confidence=0.995)
        if matrix is None or mask_inliers is None:
            return None
        selected = mask_inliers.ravel().astype(bool)
        inliers = int(np.count_nonzero(selected))
        if inliers < 5 or inliers / len(matches) < 0.50:
            return None
        scale = float(np.hypot(matrix[0, 0], matrix[0, 1]))
        rotation = float(degrees(atan2(matrix[0, 1], matrix[0, 0])))
        if not 1.05 <= scale <= 1.65 or abs(rotation) > 8.0:
            return None
        projected = cv2.transform(
            source_points.reshape(1, -1, 2), matrix).reshape(-1, 2)
        errors = np.linalg.norm(projected - atlas_points, axis=1)
        error = float(np.median(errors[selected]))
        if error > 3.5:
            return None
        player_atlas = cv2.transform(
            np.float32([[[*self.CENTER]]]), matrix).reshape(2)
        confidence = float(np.clip(
            0.38 + 0.035 * min(inliers, 10)
            + 0.18 * min(1.0, inliers / max(1, len(matches)))
            + 0.14 * max(0.0, 1.0 - error / 3.5),
            0.0, 1.0))
        return (
            (float(player_atlas[0]), float(player_atlas[1])),
            inliers, len(matches), error, scale, confidence,
        )

    def _character_heading(self, crop: np.ndarray) -> float | None:
        '读取小地图中心白色角色箭头的朝向。'
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        white = cv2.inRange(
            hsv, np.array([0, 0, 185], np.uint8),
            np.array([179, 85, 255], np.uint8))
        count, labels, stats, centers = cv2.connectedComponentsWithStats(white)
        expected = np.float64([
            self.CENTER[0] - self.CROP[0],
            self.CENTER[1] - self.CROP[1],
        ])
        candidates: list[tuple[float, float]] = []
        for index in range(1, count):
            x, y, box_w, box_h, area = (int(value) for value in stats[index])
            center = centers[index].astype(np.float64)
            distance = float(np.linalg.norm(center - expected))
            if distance > 17.0:
                continue
            if not (180 <= area <= 1050 and 15 <= box_w <= 55 and 15 <= box_h <= 70):
                continue
            component = (
                labels[y:y + box_h, x:x + box_w] == index).astype(np.uint8)
            heading = PlayerMapPoseRecognizer._heading(component)
            if heading is not None:
                candidates.append((distance, heading))
        if not candidates:
            return None
        return float(min(candidates, key=lambda item: item[0])[1])


class MiniMapPoseTracker:
    '用时间一致性过滤小地图单帧误定位。'

    def __init__(
            self, *, window: int = 5, required: int = 2,
            max_atlas_jump: float = 65.0) -> None:
        self.window = max(2, int(window))
        self.required = max(2, min(int(required), self.window))
        self.max_atlas_jump = max(1.0, float(max_atlas_jump))
        self._poses: deque[MiniMapPose] = deque(maxlen=self.window)

    def reset(self) -> None:
        '清除跨传送或跨地图的历史位置。'
        self._poses.clear()

    def update(self, pose: MiniMapPose | None) -> MiniMapPose | None:
        '接收新姿态，返回通过连续性确认的当前位置。'
        if pose is None or pose.atlas is None or pose.source is None:
            return None
        if self._poses:
            previous = self._poses[-1]
            jump = float(np.linalg.norm(
                np.float64(pose.atlas) - np.float64(previous.atlas)))
            if jump > self.max_atlas_jump:
                self._poses.clear()
        self._poses.append(pose)
        if len(self._poses) < self.required:
            return None
        selected = list(self._poses)[-self.required:]
        atlas_points = np.float64([item.atlas for item in selected])
        center = np.median(atlas_points, axis=0)
        distances = np.linalg.norm(atlas_points - center, axis=1)
        if float(np.max(distances)) > self.max_atlas_jump:
            return None
        latest = selected[-1]
        source = self._source_median(selected)
        return MiniMapPose(
            source=source,
            atlas=(float(center[0]), float(center[1])),
            character_heading_deg=latest.character_heading_deg,
            confidence=float(np.mean([item.confidence for item in selected])),
            inliers=latest.inliers,
            match_count=latest.match_count,
            reprojection_error=latest.reprojection_error,
            atlas_scale=latest.atlas_scale,
        )

    @staticmethod
    def _source_median(poses: list[MiniMapPose]) -> tuple[float, float]:
        points = np.float64([item.source for item in poses])
        center = np.median(points, axis=0)
        return float(center[0]), float(center[1])
