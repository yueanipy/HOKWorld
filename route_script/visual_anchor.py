'生成并比较不含截图的路线小地图特征锚点。'
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from world_map.minimap import MiniMapPoseRecognizer


MIN_FEATURES = 32
MIN_GOOD_MATCHES = 8
MIN_INLIERS = 8
RATIO_TEST = 0.78


@dataclass(frozen=True)
class VisualAnchor:
    '保存小地图关键点和二进制描述子，不保存原始画面。'

    points: np.ndarray
    descriptors: np.ndarray


@dataclass(frozen=True)
class VisualMatch:
    '保存一次特征锚点对比结果。'

    available: bool
    matched: bool
    center_offset: float | None
    good_matches: int
    inliers: int


def build_visual_anchor(frame: np.ndarray) -> VisualAnchor | None:
    '从当前小地图生成轻量 ORB 特征。'
    crop = MiniMapPoseRecognizer.minimap_crop(frame)
    if crop is None or crop.size == 0:
        return None
    keypoints, descriptors = _detect(crop)
    if descriptors is None or len(keypoints) < MIN_FEATURES:
        return None
    points = np.asarray([item.pt for item in keypoints], dtype=np.float32)
    descriptors = np.ascontiguousarray(descriptors, dtype=np.uint8)
    points.setflags(write=False)
    descriptors.setflags(write=False)
    return VisualAnchor(points, descriptors)


def compare_visual_anchor(
        anchor: VisualAnchor, frame: np.ndarray, *,
        max_center_offset: float = 14.0) -> VisualMatch:
    '比较当前小地图和录制锚点，返回中心位置偏移。'
    crop = MiniMapPoseRecognizer.minimap_crop(frame)
    if crop is None or crop.size == 0:
        return VisualMatch(False, False, None, 0, 0)
    keypoints, descriptors = _detect(crop)
    if (descriptors is None or len(keypoints) < MIN_FEATURES
            or len(anchor.points) < MIN_FEATURES
            or len(anchor.descriptors) < MIN_FEATURES):
        return VisualMatch(False, False, None, 0, 0)

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(descriptors, anchor.descriptors, k=2)
    good = [first for first, second in pairs
            if first.distance < RATIO_TEST * second.distance]
    if len(good) < MIN_GOOD_MATCHES:
        return VisualMatch(False, False, None, len(good), 0)

    current_points = np.float32([keypoints[item.queryIdx].pt for item in good])
    reference_points = np.float32([anchor.points[item.trainIdx] for item in good])
    matrix, inlier_mask = cv2.estimateAffinePartial2D(
        current_points,
        reference_points,
        method=cv2.RANSAC,
        ransacReprojThreshold=3.0,
        maxIters=3000,
        confidence=0.995,
    )
    if matrix is None or inlier_mask is None:
        return VisualMatch(False, False, None, len(good), 0)
    inliers = int(np.count_nonzero(inlier_mask))
    if inliers < MIN_INLIERS or inliers / len(good) < 0.30:
        return VisualMatch(False, False, None, len(good), inliers)

    scale = float(np.hypot(matrix[0, 0], matrix[0, 1]))
    rotation = float(np.degrees(np.arctan2(matrix[0, 1], matrix[0, 0])))
    if not 0.86 <= scale <= 1.14 or abs(rotation) > 8.0:
        return VisualMatch(True, False, None, len(good), inliers)

    center = np.float32([[[
        MiniMapPoseRecognizer.CENTER[0] - MiniMapPoseRecognizer.CROP[0],
        MiniMapPoseRecognizer.CENTER[1] - MiniMapPoseRecognizer.CROP[1],
    ]]])
    mapped = cv2.transform(center, matrix)[0, 0]
    offset = float(np.linalg.norm(mapped - center[0, 0]))
    return VisualMatch(
        True,
        offset <= max(1.0, float(max_center_offset)),
        offset,
        len(good),
        inliers,
    )


def save_visual_anchor(path: Path | str, anchor: VisualAnchor) -> None:
    '原子保存一个特征锚点。'
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("wb") as handle:
            np.savez_compressed(
                handle,
                points=np.asarray(anchor.points, dtype=np.float32),
                descriptors=np.asarray(anchor.descriptors, dtype=np.uint8),
            )
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_visual_anchor(path: Path | str) -> VisualAnchor:
    '读取并校验一个特征锚点。'
    with np.load(Path(path), allow_pickle=False) as data:
        points = np.asarray(data["points"], dtype=np.float32)
        descriptors = np.asarray(data["descriptors"], dtype=np.uint8)
    if (points.ndim != 2 or points.shape[1] != 2
            or descriptors.ndim != 2 or descriptors.shape[1] != 32
            or len(points) != len(descriptors)
            or len(points) < MIN_FEATURES):
        raise ValueError("路线视觉锚点格式无效")
    points = np.ascontiguousarray(points)
    descriptors = np.ascontiguousarray(descriptors)
    points.setflags(write=False)
    descriptors.setflags(write=False)
    return VisualAnchor(points, descriptors)


def replace_visual_anchor_set(
        route_directory: Path | str, route_name: str,
        anchors: tuple[tuple[str, VisualAnchor], ...]) -> None:
    '原子替换一条路线的自动视觉锚点，不影响用户截图。'
    root = Path(route_directory) / "snapshots" / str(route_name)
    target = root / "anchors"
    temporary = root / f".anchors_{os.getpid()}_{time.time_ns()}"
    backup = root / f".anchors_backup_{os.getpid()}_{time.time_ns()}"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        for relative, anchor in anchors:
            filename = Path(relative).name
            save_visual_anchor(temporary / filename, anchor)
        if target.exists():
            target.rename(backup)
        temporary.rename(target)
        if backup.exists():

            shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        if not target.exists() and backup.exists():
            backup.rename(target)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _detect(crop: np.ndarray):
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    center = (
        MiniMapPoseRecognizer.CENTER[0] - MiniMapPoseRecognizer.CROP[0],
        MiniMapPoseRecognizer.CENTER[1] - MiniMapPoseRecognizer.CROP[1],
    )
    mask = np.zeros((height, width), np.uint8)
    cv2.circle(mask, center, MiniMapPoseRecognizer.MATCH_RADIUS, 255, -1)
    cv2.circle(mask, center, 28, 0, -1)
    detector = cv2.ORB_create(
        nfeatures=500,
        scaleFactor=1.2,
        nlevels=8,
        edgeThreshold=8,
        fastThreshold=12,
    )
    return detector.detectAndCompute(gray, mask)



