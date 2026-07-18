'可复用的地上世界地图定位、选点和具名传送接口。'
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import cv2
import numpy as np

from fishing.template_bank import normalize
from runtime_guard import dev_log
from paths import resource_path

from daily import recognizer as rec


ASSET_DIR = resource_path("assets", "world_map")
SPECIAL_TARGETS_FILE = "special_targets_v1.json"


class MapTargetKind(str, Enum):
    NORMAL = "normal"
    
    REGIONAL = "regional"
    CHALLENGE = "challenge"
    
    POI = "poi"
    DESTINATION = "destination"


class MapTargetStatus(str, Enum):
    AVAILABLE = "available"
    COORDINATE_VERIFIED = "coordinate_verified"
    LOCKED = "locked"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    OFFSCREEN = "offscreen"


@dataclass(frozen=True)
class MapTarget:
    id: int
    name: str
    kind: MapTargetKind
    category: str
    region: str
    source: tuple[float, float]
    atlas: tuple[float, float]


@dataclass(frozen=True)
class MapLocation:
    '保存从1920宽归一化画面像素到图谱像素的变换。'

    screen_to_atlas: np.ndarray
    matches: int
    inliers: int
    inlier_ratio: float
    scale: float
    rotation_deg: float
    median_error_px: float
    confidence: float

    def atlas_to_screen(self, point: tuple[float, float]) -> tuple[float, float] | None:
        inverse = cv2.invertAffineTransform(self.screen_to_atlas)
        source = np.float32(point).reshape(1, 1, 2)
        projected = cv2.transform(source, inverse).reshape(2)
        if not np.isfinite(projected).all():
            return None
        return float(projected[0]), float(projected[1])


@dataclass(frozen=True)
class TargetObservation:
    target: MapTarget
    point: tuple[float, float] | None
    status: MapTargetStatus
    location: MapLocation


class WorldMapAtlas:
    '按需加载地上地图的SIFT特征图谱。'

    def __init__(self, asset_dir: Path | str = ASSET_DIR) -> None:
        self.asset_dir = Path(asset_dir)
        self._loaded = False
        self._points: np.ndarray | None = None
        self._descriptors: np.ndarray | None = None
        self._matcher = None
        self._source_to_atlas: np.ndarray | None = None
        self._sift = cv2.SIFT_create(nfeatures=6000, contrastThreshold=0.025)
        self.map_roi = (0.08, 0.10, 0.85, 0.84)
        self.targets: dict[str, MapTarget] = {}

    def _load(self) -> None:
        if self._loaded:
            return
        atlas_path = self.asset_dir / "atlas_v1.npz"
        targets_path = self.asset_dir / "targets_v1.json"
        metadata_path = self.asset_dir / "metadata_v1.json"
        if not (atlas_path.exists() and targets_path.exists() and metadata_path.exists()):
            raise FileNotFoundError(f"世界地图图谱资产不完整: {self.asset_dir}")
        atlas = np.load(atlas_path, allow_pickle=False)
        self._points = np.asarray(atlas["points"], np.float32)
        
        self._descriptors = np.asarray(atlas["descriptors"], np.float32)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.map_roi = tuple(float(value) for value in metadata["map_roi"])
        self._source_to_atlas = np.asarray(metadata["source_to_atlas"], np.float64)
        raw_targets = list(json.loads(
            targets_path.read_text(encoding="utf-8"))["targets"])
        special_path = self.asset_dir / SPECIAL_TARGETS_FILE
        if special_path.exists():
            raw_targets.extend(json.loads(
                special_path.read_text(encoding="utf-8"))["targets"])
        names = [str(item["name"]) for item in raw_targets]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"世界地图目标名称重复: {duplicates}")
        self.targets = {
            item["name"]: MapTarget(
                id=int(item["id"]),
                name=str(item["name"]),
                kind=MapTargetKind(item["kind"]),
                category=str(item.get("category") or ""),
                region=str(item.get("region") or ""),
                source=tuple(float(value) for value in item["source"]),
                atlas=tuple(float(value) for value in item["atlas"]),
            )
            for item in raw_targets
        }
        self._matcher = cv2.FlannBasedMatcher(
            {"algorithm": 1, "trees": 8}, {"checks": 96})
        self._matcher.add([self._descriptors])
        self._matcher.train()
        self._loaded = True
        dev_log(
            f"[world map] 图谱已加载 features={len(self._points)} "
            f"targets={len(self.targets)}")

    def target(self, name: str) -> MapTarget:
        self._load()
        try:
            return self.targets[name]
        except KeyError as exc:
            suggestions = [known for known in self.targets if name in known or known in name][:5]
            suffix = f"，相近名称: {suggestions}" if suggestions else ""
            raise KeyError(f"未注册地上世界传送目标 {name!r}{suffix}") from exc

    def destination(self, name: str, source: tuple[float, float], region: str = "") -> MapTarget:
        '为后续寻路或战斗任务创建不可点击的地图目的地。'
        point = self.source_to_atlas_point(source)
        return MapTarget(
            id=0, name=name, kind=MapTargetKind.DESTINATION,
            category="destination", region=region,
            source=(float(source[0]), float(source[1])),
            atlas=point,
        )

    def source_to_atlas_point(self, source: tuple[float, float]) -> tuple[float, float]:
        '把公开地上地图坐标转换为图谱像素。'
        self._load()
        point = np.float64([float(source[0]), float(source[1]), 1.0]) @ self._source_to_atlas.T
        return float(point[0]), float(point[1])

    def atlas_to_source_point(self, atlas: tuple[float, float]) -> tuple[float, float]:
        '把图谱像素转换回公开地上地图坐标。'
        self._load()
        inverse = cv2.invertAffineTransform(self._source_to_atlas)
        point = cv2.transform(
            np.float64(atlas).reshape(1, 1, 2), inverse).reshape(2)
        return float(point[0]), float(point[1])

    def screen_to_source_point(
            self, frame: np.ndarray, point: tuple[float, float],
            location: MapLocation | None = None) -> tuple[float, float] | None:
        '把识别到的归一化屏幕点转换为地上地图坐标。'
        image = normalize(frame)
        location = location or self.locate(image)
        if location is None:
            return None
        h, w = image.shape[:2]
        screen = np.float64([
            float(point[0]) * w,
            float(point[1]) * h,
            1.0,
        ])
        atlas = location.screen_to_atlas @ screen
        return self.atlas_to_source_point((float(atlas[0]), float(atlas[1])))

    def project_source_point(
            self, frame: np.ndarray, source: tuple[float, float],
            location: MapLocation | None = None) -> tuple[float, float] | None:
        '把任意地上地图坐标投影到当前归一化画面。'
        image = normalize(frame)
        location = location or self.locate(image)
        if location is None:
            return None
        h, w = image.shape[:2]
        target = self.destination("coordinate", source)
        return self._screen_point(location, target, w, h)

    def nearest_teleport_targets(
            self, source: tuple[float, float], limit: int = 5,
            *, include_poi: bool = True) -> list[MapTarget]:
        '按地图坐标距离排列已注册的地上传送目标。'
        self._load()
        kinds = {MapTargetKind.NORMAL, MapTargetKind.REGIONAL}
        if include_poi:
            kinds.add(MapTargetKind.POI)
        sx, sy = float(source[0]), float(source[1])
        targets = [target for target in self.targets.values() if target.kind in kinds]
        targets.sort(key=lambda target: (
            (target.source[0] - sx) ** 2 + (target.source[1] - sy) ** 2,
            target.name,
        ))
        return targets[:max(0, int(limit))]

    def resolve(self, target: str | MapTarget) -> MapTarget:
        return self.target(target) if isinstance(target, str) else target

    def locate(self, frame: np.ndarray) -> MapLocation | None:
        '不依赖OCR或固定屏幕坐标定位当前游戏地图画面。'
        if frame is None:
            return None
        self._load()
        image = normalize(frame)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        x0, y0, x1, y1 = self.map_roi
        px0, py0 = int(x0 * w), int(y0 * h)
        px1, py1 = int(x1 * w), int(y1 * h)
        keypoints, descriptors = self._sift.detectAndCompute(gray[py0:py1, px0:px1], None)
        if descriptors is None or len(keypoints) < 30:
            return None
        query_points = np.float32([
            [keypoint.pt[0] + px0, keypoint.pt[1] + py0]
            for keypoint in keypoints
        ])
        pairs = self._matcher.knnMatch(np.asarray(descriptors, np.float32), k=2)
        good = [first for first, second in pairs if first.distance < 0.68 * second.distance]
        if len(good) < 24:
            dev_log(f"[world map] 定位匹配不足 good={len(good)}")
            return None
        source = np.float32([query_points[match.queryIdx] for match in good])
        target = np.float32([self._points[match.trainIdx] for match in good])
        affine, mask = cv2.estimateAffinePartial2D(
            source, target, method=cv2.RANSAC, ransacReprojThreshold=8.0,
            maxIters=10000, confidence=0.999, refineIters=30)
        if affine is None or mask is None:
            return None
        inlier_mask = mask.ravel().astype(bool)
        inliers = int(inlier_mask.sum())
        ratio = inliers / max(1, len(good))
        scale = float(np.hypot(affine[0, 0], affine[0, 1]))
        rotation = float(np.degrees(np.arctan2(affine[1, 0], affine[0, 0])))
        predicted = cv2.transform(source.reshape(-1, 1, 2), affine).reshape(-1, 2)
        errors = np.linalg.norm(predicted - target, axis=1)
        median_error = float(np.median(errors[inlier_mask])) if inliers else float("inf")
        
        
        
        if (inliers < 18 or ratio < 0.30 or not 0.30 <= scale <= 6.00 or
                abs(rotation) > 3.0 or median_error > 7.0):
            dev_log(
                f"[world map] 定位门未通过 good={len(good)} inliers={inliers} "
                f"ratio={ratio:.3f} scale={scale:.3f} rotation={rotation:.2f} "
                f"error={median_error:.2f}")
            return None
        confidence = float(np.clip(
            0.45 * min(1.0, inliers / 80.0) +
            0.35 * min(1.0, ratio / 0.65) +
            0.20 * max(0.0, 1.0 - median_error / 8.0), 0.0, 1.0))
        return MapLocation(
            screen_to_atlas=affine.astype(np.float64),
            matches=len(good),
            inliers=inliers,
            inlier_ratio=ratio,
            scale=scale,
            rotation_deg=rotation,
            median_error_px=median_error,
            confidence=confidence,
        )

    @staticmethod
    def _screen_point(location: MapLocation, target: MapTarget,
                      width: int, height: int) -> tuple[float, float] | None:
        pixels = location.atlas_to_screen(target.atlas)
        if pixels is None:
            return None
        return pixels[0] / width, pixels[1] / height

    def observe(self, frame: np.ndarray, name: str | MapTarget,
                location: MapLocation | None = None) -> TargetObservation | None:
        '在当前画面中投影并分类一个具名目标。'
        image = normalize(frame)
        location = location or self.locate(image)
        if location is None:
            return None
        target = self.resolve(name)
        h, w = image.shape[:2]
        point = self._screen_point(location, target, w, h)
        if point is None or not (0.03 <= point[0] <= 0.97 and 0.05 <= point[1] <= 0.89):
            return TargetObservation(target, point, MapTargetStatus.OFFSCREEN, location)
        status, refined = self._classify_marker(image, target, point)
        return TargetObservation(target, refined or point, status, location)

    @staticmethod
    def _classify_marker(image: np.ndarray, target: MapTarget,
                         point: tuple[float, float]) -> tuple[MapTargetStatus, tuple[float, float] | None]:
        h, w = image.shape[:2]
        stone_kinds = (MapTargetKind.NORMAL, MapTargetKind.REGIONAL)
        radius = 62 if target.kind in stone_kinds else 72
        cx, cy = int(point[0] * w), int(point[1] * h)
        x0, x1 = max(0, cx - radius), min(w, cx + radius + 1)
        y0, y1 = max(0, cy - radius), min(h, cy + radius + 1)
        patch = image[y0:y1, x0:x1]
        if patch.size == 0:
            return MapTargetStatus.OFFSCREEN, None
        if target.kind is MapTargetKind.DESTINATION:
            return MapTargetStatus.UNKNOWN, point
        if target.kind is MapTargetKind.POI:
            
            
            if target.category == "青色传送地点列表":
                candidates = rec.find_map_teleport_icons(
                    image, point, max_distance=0.04, min_distance=0.0,
                    min_width=24, min_height=14, min_original_pixels=65)
                if candidates:
                    refined = min(candidates, key=lambda candidate: np.hypot(
                        (candidate[0] - point[0]) * w,
                        (candidate[1] - point[1]) * h,
                    ))
                    distance = float(np.hypot(
                        (refined[0] - point[0]) * w,
                        (refined[1] - point[1]) * h,
                    ))
                    if distance <= 42.0:
                        return MapTargetStatus.AVAILABLE, refined
            return MapTargetStatus.UNKNOWN, point
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)

        if target.kind in stone_kinds:
            
            
            
            
            local_radius = 24
            lx0, lx1 = max(0, radius - local_radius), min(patch.shape[1], radius + local_radius + 1)
            ly0, ly1 = max(0, radius - local_radius), min(patch.shape[0], radius + local_radius + 1)
            local = patch[ly0:ly1, lx0:lx1]
            if local.size:
                local_hsv = cv2.cvtColor(local, cv2.COLOR_BGR2HSV)
                local_neutral = cv2.inRange(
                    local_hsv, np.array([0, 0, 100], np.uint8),
                    np.array([179, 65, 230], np.uint8))
                local_edges = cv2.Canny(cv2.cvtColor(local, cv2.COLOR_BGR2GRAY), 65, 145)
                if (cv2.countNonZero(local_neutral) >= 450 and
                        cv2.countNonZero(local_edges) >= 160):
                    return MapTargetStatus.LOCKED, point

            mask = cv2.inRange(hsv, np.array([82, 100, 145], np.uint8),
                               np.array([102, 255, 255], np.uint8))
            count, _, stats, centers = cv2.connectedComponentsWithStats(mask)
            candidates = []
            for index in range(1, count):
                area = int(stats[index, cv2.CC_STAT_AREA])
                if not 18 <= area <= 2200:
                    continue
                px = (x0 + float(centers[index][0])) / w
                py = (y0 + float(centers[index][1])) / h
                distance = float(np.hypot((px - point[0]) * w, (py - point[1]) * h))
                
                
                
                
                
                if distance <= 30.0:
                    candidates.append((distance, px, py, area))
            if candidates:
                _, px, py, _ = min(candidates)
                return MapTargetStatus.AVAILABLE, (px, py)
            
            
            neutral = cv2.inRange(hsv, np.array([0, 0, 100], np.uint8),
                                  np.array([179, 65, 230], np.uint8))
            edges = cv2.Canny(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), 65, 145)
            if cv2.countNonZero(neutral) >= 80 and cv2.countNonZero(edges) >= 100:
                return MapTargetStatus.LOCKED, point
            return MapTargetStatus.UNKNOWN, point

        gold = cv2.inRange(hsv, np.array([12, 55, 125], np.uint8),
                           np.array([39, 255, 255], np.uint8))
        silver = cv2.inRange(hsv, np.array([0, 0, 135], np.uint8),
                             np.array([179, 70, 245], np.uint8))
        colored = cv2.inRange(hsv, np.array([40, 90, 120], np.uint8),
                              np.array([165, 255, 255], np.uint8))
        usable_pixels = cv2.countNonZero(gold) + cv2.countNonZero(silver)
        activity_pixels = cv2.countNonZero(colored)
        if usable_pixels >= 120 and activity_pixels <= usable_pixels * 1.8:
            
            
            
            gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
            center = gray[radius // 2:radius + radius // 2,
                          radius // 2:radius + radius // 2]
            
            
            if center.size and np.count_nonzero(center > 235) > 140:
                return MapTargetStatus.UNAVAILABLE, point
            return MapTargetStatus.AVAILABLE, point
        return MapTargetStatus.UNKNOWN, point


class WorldMapNavigator:
    '执行地图内闭环移动，不执行角色移动或战斗。'

    CENTER = (0.50, 0.48)
    
    
    SAFE_ROI = (0.22, 0.24, 0.78, 0.76)
    TARGET_UI_ROI = (0.58, 0.10, 0.99, 0.90)
    TARGET_ACTION_ROI = (0.70, 0.58, 0.98, 0.86)
    TARGET_ACTIONS = ("传送", "快速前往", "前往追踪")
    TELEPORT_ACTIONS = ("传送", "快速前往")
    BASE_LAYER = "base"
    REGIONAL_LAYER = "regional"
    LAYER_SCROLL_POINT = (0.50, 0.48)
    BASE_LAYER_SCALE = 1.25
    REGIONAL_LAYER_SCALE = 1.16
    LAYER_SCALE_TOLERANCE = 0.04
    MAX_LAYER_SCROLLS = 24
    MIN_SCALE_PROGRESS = 0.008
    FAST_SCROLL_ERROR = 0.24
    LAYER_SCROLL_SETTLE_S = 0.16
    SCALE_RATIO_MIN_PER_NOTCH = 0.82
    SCALE_RATIO_MAX_PER_NOTCH = 0.985
    SCALE_TRANSITION_RETRIES = 3
    POI_CENTER_TOLERANCE = (0.025, 0.035)
    HIGH_LOCATION_CONFIDENCE = 0.90
    
    
    
    DRAG_LOCATION_CONFIDENCE_FLOOR = 0.70
    DRAG_LOCATION_POINT_DRIFT = 0.015
    DRAG_LOCATION_SCALE_DRIFT = 0.020
    DRAG_LOCATION_HITS = 3
    LOW_CONFIDENCE_DRAG_FACTOR = 0.55
    UNCERTAIN_CHECKS_BEFORE_RECOVERY = 6
    RECOVERY_DRAG_FACTOR = 0.35
    RECOVERY_DRAG_MIN = 0.045
    RECOVERY_DRAG_MAX = 0.10
    RECOVERY_CENTER_NUDGE = 0.06
    MAX_TRANSIENT_LOCATION_CHECKS = 18

    def __init__(self, ctx, atlas: WorldMapAtlas | None = None, name: str = "世界地图") -> None:
        self.ctx = ctx
        self.atlas = atlas or WorldMapAtlas()
        self.name = name
        
        
        self._layer: str | None = None

    def _wait_map_stable(self, timeout: float) -> bool:
        '用最近五帧中的三次命中确认地图，并要求末两帧均命中。'
        deadline = self.ctx.logical_time() + max(0.0, timeout)
        recent: list[bool] = []
        while self.ctx.logical_time() < deadline and not self.ctx.should_stop():
            frame = self.ctx.grab()
            hit = bool(frame is not None and rec.in_world_map(frame))
            recent.append(hit)
            recent = recent[-5:]
            if (len(recent) >= 3 and sum(recent) >= 3
                    and len(recent) >= 2 and recent[-2:] == [True, True]):
                return True
            self.ctx.sleep(0.18)
        dev_log(f"[world map] 稳定门超时 recent={recent}")
        return False

    def open(self, timeout: float = 5.0) -> bool:
        frame = self.ctx.grab()
        if frame is not None and rec.in_world_map(frame):
            
            
            stable = self._wait_map_stable(min(timeout, 2.4))
            if stable:
                self._layer = None
            return stable

        
        
        
        for attempt in range(2):
            if attempt:
                frame = self.ctx.grab()
                if frame is not None and rec.in_world_map(frame):
                    if self._wait_map_stable(min(timeout, 2.4)):
                        self._layer = None
                        return True
                if frame is None or not rec.in_world_hud(frame):
                    hits = () if frame is None else rec.world_hud_hits(frame)
                    self.ctx.log(
                        f"{self.name}:首次 M 后未确认地图，当前也非角色 HUD{hits}，"
                        "为避免误关地图不再补按 M")
                    return False
                self.ctx.log(f"{self.name}:确认仍在角色 HUD，补按一次 M")
            if not self.ctx.press("m"):
                return False
            if self._wait_map_stable(timeout if attempt == 0 else min(timeout, 3.0)):
                
                self._layer = None
                return True

        self.ctx.log(f"等待超时:{self.name}打开")
        return False

    def _locate_with_recovery(self) -> tuple[np.ndarray, MapLocation] | None:
        
        
        
        for attempt in range(3):
            frame = self.ctx.grab()
            location = self.atlas.locate(frame)
            if location is not None:
                return frame, location
            if frame is None or not rec.in_world_map(frame):
                dev_log("[world map] 当前帧未通过地图正向门，停止定位恢复")
                return None
            if attempt < 2:
                self.ctx.sleep(0.22)
        return None

    @classmethod
    def _scale_transition_valid(cls, before: float, after: float,
                                notches: int) -> bool:
        '验证一次滚轮后的 SIFT 比例是否符合真实地图缩放方向。'
        if notches == 0 or before <= 0.0 or after <= 0.0:
            return False
        count = abs(int(notches))
        ratio = after / before
        if notches > 0:
            lower = cls.SCALE_RATIO_MIN_PER_NOTCH ** count
            upper = cls.SCALE_RATIO_MAX_PER_NOTCH ** count
            return lower <= ratio <= upper
        inverse = 1.0 / ratio
        lower = cls.SCALE_RATIO_MIN_PER_NOTCH ** count
        upper = cls.SCALE_RATIO_MAX_PER_NOTCH ** count
        return lower <= inverse <= upper

    def _read_scale_transition(self, before: MapLocation,
                               notches: int) -> tuple[np.ndarray, MapLocation] | None:
        '读取滚轮后的新位置，只接收与输入方向连续的仿射解。'
        rejected: list[float] = []
        for attempt in range(self.SCALE_TRANSITION_RETRIES):
            located = self._locate_with_recovery()
            if located is not None:
                scale = float(located[1].scale)
                if self._scale_transition_valid(before.scale, scale, notches):
                    return located
                rejected.append(scale)
            if attempt + 1 < self.SCALE_TRANSITION_RETRIES:
                self.ctx.sleep(0.16)
        dev_log(
            f"[world map] reject_scale_transition notches={notches} "
            f"before={before.scale:.6f} candidates="
            f"{','.join(f'{value:.6f}' for value in rejected) or 'none'}")
        return None

    def _ensure_target_layer(self, target: MapTarget) -> bool:
        '切到目标预定位层，远处两格、近处一格闭环并保持状态一致。'
        if target.kind is MapTargetKind.CHALLENGE:
            desired = self.REGIONAL_LAYER
            expected = self.REGIONAL_LAYER_SCALE
            scroll_budget = self.MAX_LAYER_SCROLLS
        else:
            desired = self.BASE_LAYER
            expected = self.BASE_LAYER_SCALE
            scroll_budget = self.MAX_LAYER_SCROLLS
        if desired == self._layer:
            return True
        located = self._locate_with_recovery()
        if located is None:
            self.ctx.log(f"{self.name}:无法读取当前地图缩放")
            return False
        current_location = located[1]
        scale = float(current_location.scale)
        stalled = 0
        scrolls = 0
        attempts = 0
        while scrolls <= scroll_budget:
            error = abs(scale - expected)
            if error <= self.LAYER_SCALE_TOLERANCE:
                self._layer = desired
                dev_log(
                    f"[world map] layer={desired} scale={scale:.6f} "
                    f"scrolls={scrolls} attempts={attempts} target={target.name}")
                return True
            if scrolls >= scroll_budget:
                break

            
            
            
            direction = 1 if scale > expected else -1
            batch = (2 if error >= self.FAST_SCROLL_ERROR and
                     scroll_budget - scrolls >= 2 else 1)
            notches = direction * batch
            if not self.ctx.scroll(notches, self.LAYER_SCROLL_POINT):
                self.ctx.log(f"{self.name}:第 {attempts + 1} 次地图缩放输入失败")
                return False
            attempts += 1
            scrolls += batch
            self.ctx.sleep(self.LAYER_SCROLL_SETTLE_S)
            if not self._wait_map_stable(2.4):
                self.ctx.log(f"{self.name}:缩放后未确认世界地图稳定，停止操作")
                return False
            next_located = self._read_scale_transition(current_location, notches)
            if next_located is None:
                self.ctx.log(
                    f"{self.name}:缩放后比例跳变或方向错误 "
                    f"(scale={scale:.3f}, notches={notches})，停止操作")
                return False
            next_scale = float(next_located[1].scale)
            next_error = abs(next_scale - expected)
            stalled = stalled + 1 if error - next_error < self.MIN_SCALE_PROGRESS else 0
            dev_log(
                f"[world map] normalize_layer={desired} attempt={attempts} "
                f"scrolls={scrolls} "
                f"notches={notches} scale={scale:.6f}->{next_scale:.6f}")
            if stalled >= 2:
                self.ctx.log(
                    f"{self.name}:连续两次缩放无有效进展 "
                    f"({scale:.3f}->{next_scale:.3f})，停止操作")
                return False
            current_location = next_located[1]
            scale = next_scale

        self.ctx.log(
            f"{self.name}:滚轮 {scroll_budget} 格后仍未到达 {desired} 层 "
            f"(scale={scale:.3f}, target={expected:.3f})")
        return False

    def _open_and_prepare_target(self, target: MapTarget) -> bool:
        '封装入口前置门：打开地图后先归一层级，期间禁止拖图和点击。'
        if not self.open():
            return False
        if not self._ensure_target_layer(target):
            return False
        dev_log(
            f"[world map] target_ready target={target.name} layer={self._layer}; "
            "allow_center_and_click=true")
        return True

    def _confirm_centered_coordinate(
            self, target: MapTarget, first_point: tuple[float, float],
            first_location: MapLocation) -> TargetObservation | None:
        '在已验证基础层用双帧全图投影确认任意标定坐标。'
        if (self._layer != self.BASE_LAYER or
                abs(first_location.scale - self.BASE_LAYER_SCALE) >
                self.LAYER_SCALE_TOLERANCE or
                first_location.confidence < 0.90):
            return None
        self.ctx.sleep(0.16)
        located = self._locate_with_recovery()
        if located is None:
            return None
        frame, location = located
        if (abs(location.scale - self.BASE_LAYER_SCALE) >
                self.LAYER_SCALE_TOLERANCE or location.confidence < 0.90):
            return None
        image = normalize(frame)
        h, w = image.shape[:2]
        point = self.atlas._screen_point(location, target, w, h)
        if point is None:
            return None
        close_x = abs(point[0] - self.CENTER[0]) <= self.POI_CENTER_TOLERANCE[0]
        close_y = abs(point[1] - self.CENTER[1]) <= self.POI_CENTER_TOLERANCE[1]
        drift = float(np.hypot(
            point[0] - first_point[0], point[1] - first_point[1]))
        if not close_x or not close_y or drift > 0.012:
            return None
        confirmed = (
            (first_point[0] + point[0]) * 0.5,
            (first_point[1] + point[1]) * 0.5,
        )
        dev_log(
            f"[world map] coordinate_verified target={target.name} "
            f"scale={location.scale:.6f} confidence={location.confidence:.3f} "
            f"drift={drift:.5f} point=({confirmed[0]:.4f},{confirmed[1]:.4f})")
        return TargetObservation(
            target, confirmed, MapTargetStatus.COORDINATE_VERIFIED, location)

    def center_target(self, name: str | MapTarget, max_drags: int = 9, *,
                      layer_ready: bool = False) -> TargetObservation | None:
        target = self.atlas.resolve(name)
        
        
        if not layer_ready and not self._ensure_target_layer(target):
            return None
        drag_budget = max(1, max(max_drags, 24)
                          if target.kind is MapTargetKind.POI else max_drags)
        
        
        max_checks = drag_budget + self.MAX_TRANSIENT_LOCATION_CHECKS + 1
        check_count = 0
        drag_count = 0
        stable_point: tuple[float, float] | None = None
        stable_scale: float | None = None
        stable_hits = 0
        uncertain_checks = 0
        last_drag_delta: np.ndarray | None = None
        recovery_index = 0
        input_blocked = False

        def reset_stability() -> None:
            nonlocal stable_point, stable_scale, stable_hits, uncertain_checks
            stable_point = None
            stable_scale = None
            stable_hits = 0
            uncertain_checks = 0

        def perform_drag(delta: np.ndarray, *, reason: str,
                         source_point: tuple[float, float] | None) -> bool:
            '发送一次有界拖图，并保存动作上下文供下一帧连续性判断。'
            nonlocal drag_count, last_drag_delta
            nonlocal input_blocked
            if drag_count >= drag_budget:
                return False
            bounded = np.asarray(delta, np.float64).copy()
            bounded[0] = float(np.clip(bounded[0], -0.30, 0.30))
            bounded[1] = float(np.clip(bounded[1], -0.26, 0.26))
            end = np.clip(
                np.array(self.CENTER, np.float64) + bounded,
                (0.15, 0.15), (0.85, 0.82))
            actual = end - np.array(self.CENTER, np.float64)
            if float(np.linalg.norm(actual)) < 0.015:
                return False
            if not self.ctx.drag(self.CENTER, tuple(end), duration_s=0.55):
                ready = self.ctx.action_ready()
                input_blocked = not ready
                dev_log(
                    f"[world map] {target.name} drag_input_failed "
                    f"check={check_count}/{max_checks} "
                    f"drag={drag_count}/{drag_budget} action_ready={ready} "
                    f"reason={reason}")
                return False
            drag_count += 1
            last_drag_delta = actual
            reset_stability()
            dev_log(
                f"[world map] {target.name} drag_action reason={reason} "
                f"drag={drag_count}/{drag_budget} "
                f"delta=({actual[0]:.3f},{actual[1]:.3f}) "
                f"source={source_point}")
            self.ctx.sleep(0.38)
            return True

        def recovery_delta() -> np.ndarray:
            '沿最近一次有效动作继续小移；无动作历史时用固定小幅探测。'
            nonlocal recovery_index
            if last_drag_delta is not None:
                norm = float(np.linalg.norm(last_drag_delta))
                if norm > 1e-6:
                    length = float(np.clip(
                        norm * self.RECOVERY_DRAG_FACTOR,
                        self.RECOVERY_DRAG_MIN, self.RECOVERY_DRAG_MAX))
                    return last_drag_delta / norm * length
            nudges = (
                (-self.RECOVERY_CENTER_NUDGE, 0.0),
                (0.0, self.RECOVERY_CENTER_NUDGE),
                (self.RECOVERY_CENTER_NUDGE, 0.0),
                (0.0, -self.RECOVERY_CENTER_NUDGE),
            )
            value = np.asarray(nudges[recovery_index % len(nudges)], np.float64)
            recovery_index += 1
            return value

        
        
        while check_count < max_checks:
            check_count += 1
            located = self._locate_with_recovery()
            if located is None:
                uncertain_checks += 1
                dev_log(
                    f"[world map] {target.name} 定位暂失 "
                    f"check={check_count}/{max_checks} drag={drag_count}/{drag_budget} "
                    f"uncertain={uncertain_checks}/{self.UNCERTAIN_CHECKS_BEFORE_RECOVERY}")
                if (uncertain_checks >= self.UNCERTAIN_CHECKS_BEFORE_RECOVERY and
                        drag_count < drag_budget):
                    if perform_drag(
                            recovery_delta(), reason="location_missing_recovery",
                            source_point=None):
                        continue
                    if input_blocked:
                        return None
                self.ctx.sleep(0.24)
                continue
            frame, location = located
            image = normalize(frame)
            h, w = image.shape[:2]
            point = self.atlas._screen_point(location, target, w, h)
            if point is None:
                stable_point = None
                stable_scale = None
                stable_hits = 0
                uncertain_checks += 1
                self.ctx.sleep(0.18)
                continue

            movement_only = False
            if location.confidence < self.HIGH_LOCATION_CONFIDENCE:
                expected_scale = (self.REGIONAL_LAYER_SCALE
                                  if target.kind is MapTargetKind.CHALLENGE
                                  else self.BASE_LAYER_SCALE)
                point_drift = (float("inf") if stable_point is None else
                               float(np.hypot(point[0] - stable_point[0],
                                              point[1] - stable_point[1])))
                scale_drift = (float("inf") if stable_scale is None else
                               abs(float(location.scale) - stable_scale))
                above_floor = (
                    location.confidence >= self.DRAG_LOCATION_CONFIDENCE_FLOOR and
                    abs(float(location.scale) - expected_scale) <=
                    self.LAYER_SCALE_TOLERANCE)
                stable_candidate = (
                    above_floor and
                    point_drift <= self.DRAG_LOCATION_POINT_DRIFT and
                    scale_drift <= self.DRAG_LOCATION_SCALE_DRIFT)
                stable_hits = stable_hits + 1 if stable_candidate else int(above_floor)
                stable_point = point if above_floor else None
                stable_scale = float(location.scale) if above_floor else None
                uncertain_checks = 0 if stable_candidate else uncertain_checks + 1
                dev_log(
                    f"[world map] {target.name} low_confidence_retry "
                    f"check={check_count}/{max_checks} drag={drag_count}/{drag_budget} "
                    f"point=({point[0]:.3f},{point[1]:.3f}) "
                    f"confidence={location.confidence:.3f} "
                    f"point_drift={point_drift:.5f} scale_drift={scale_drift:.5f} "
                    f"stable_hits={stable_hits}/{self.DRAG_LOCATION_HITS} "
                    f"uncertain={uncertain_checks}/{self.UNCERTAIN_CHECKS_BEFORE_RECOVERY}")
                if stable_hits < self.DRAG_LOCATION_HITS:
                    if (uncertain_checks >= self.UNCERTAIN_CHECKS_BEFORE_RECOVERY and
                            drag_count < drag_budget):
                        if perform_drag(
                                recovery_delta(), reason="ambiguous_transform_recovery",
                                source_point=None):
                            continue
                        if input_blocked:
                            return None
                    self.ctx.sleep(0.18)
                    continue
                movement_only = True
                uncertain_checks = 0
                dev_log(
                    f"[world map] {target.name} movement_only_stable "
                    f"confidence={location.confidence:.3f} "
                    f"point=({point[0]:.3f},{point[1]:.3f})")
            else:
                reset_stability()

            x0, y0, x1, y1 = self.SAFE_ROI
            dev_log(
                f"[world map] {target.name} drag={drag_count} check={check_count} "
                f"point=({point[0]:.3f},{point[1]:.3f}) "
                f"confidence={location.confidence:.3f}")
            if target.kind in (MapTargetKind.POI, MapTargetKind.DESTINATION):
                close_x = abs(point[0] - self.CENTER[0]) <= self.POI_CENTER_TOLERANCE[0]
                close_y = abs(point[1] - self.CENTER[1]) <= self.POI_CENTER_TOLERANCE[1]
                if close_x and close_y:
                    if movement_only:
                        
                        
                        if last_drag_delta is not None:
                            direction = np.array(
                                [-last_drag_delta[1], last_drag_delta[0]], np.float64)
                            norm = float(np.linalg.norm(direction))
                            delta = (direction / norm * self.RECOVERY_CENTER_NUDGE
                                     if norm > 1e-6 else recovery_delta())
                        else:
                            delta = recovery_delta()
                        if perform_drag(
                                delta, reason="center_low_confidence_recovery",
                                source_point=point):
                            continue
                        if input_blocked:
                            return None
                        self.ctx.sleep(0.18)
                        continue
                    confirmed = self._confirm_centered_coordinate(
                        target, point, location)
                    if confirmed is not None:
                        return confirmed
                    dev_log(
                        f"[world map] {target.name} centered_confirmation_retry "
                        f"check={check_count}/{max_checks} "
                        f"drag={drag_count}/{drag_budget}")
                    self.ctx.sleep(0.16)
                    continue
            elif x0 <= point[0] <= x1 and y0 <= point[1] <= y1:
                return self.atlas.observe(frame, target, location)

            if drag_count >= drag_budget:
                break
            delta = np.array(self.CENTER, np.float64) - np.array(point, np.float64)
            reason = "high_confidence_centering"
            if movement_only:
                delta *= self.LOW_CONFIDENCE_DRAG_FACTOR
                reason = "movement_only_centering"
            if perform_drag(delta, reason=reason, source_point=point):
                continue
            if input_blocked:
                return None
            self.ctx.sleep(0.16)
        self.ctx.log(
            f"{self.name}:拖图 {drag_count}/{drag_budget} 次、定位复查 "
            f"{check_count}/{max_checks} 次仍未确认 {target.name}")
        return None

    @staticmethod
    def _clean_ocr_text(text: str) -> str:
        return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", str(text)))

    @staticmethod
    def _one_edit_match(left: str, right: str) -> bool:
        '名称至少三字时只容忍一次 OCR 增、删或替换。'
        if left == right:
            return True
        if min(len(left), len(right)) < 3 or abs(len(left) - len(right)) > 1:
            return False
        if len(left) == len(right):
            return sum(a != b for a, b in zip(left, right)) == 1
        if len(left) > len(right):
            left, right = right, left
        i = j = 0
        skipped = False
        while i < len(left) and j < len(right):
            if left[i] == right[j]:
                i += 1
                j += 1
            elif skipped:
                return False
            else:
                skipped = True
                j += 1
        return True

    @classmethod
    def _line_matches_target(cls, text: str, target: MapTarget) -> bool:
        if target.category == "青色传送地点列表":
            
            
            return text == target.name
        return (text == target.name or target.name in text or
                cls._one_edit_match(text, target.name))

    def _target_ui_state(self, frame: np.ndarray, target: MapTarget) -> dict:
        lines = rec.ocr_lines(frame, self.TARGET_UI_ROI, upscale=1.35)
        cleaned = [
            (self._clean_ocr_text(text), float(cx), float(cy))
            for text, cx, cy in lines
        ]
        matches = [line for line in cleaned
                   if self._line_matches_target(line[0], target)]
        joined = "".join(text for text, _, _ in cleaned)
        action_line = next(
            ((word, cx, cy)
             for text, cx, cy in cleaned
             for word in self.TARGET_ACTIONS
             if (word in text and
                 self.TARGET_ACTION_ROI[0] <= cx <= self.TARGET_ACTION_ROI[2] and
                 self.TARGET_ACTION_ROI[1] <= cy <= self.TARGET_ACTION_ROI[3])),
            None,
        )
        detail_panel = (
            ("渡石" in joined and "盛产风物" in joined) or
            ("当前难度" in joined and "关卡奖励" in joined)
        )
        return {
            "action": action_line[0] if action_line else "",
            "action_point": ((action_line[1], action_line[2])
                             if action_line else None),
            "detail_panel": detail_panel,
            "matches": matches,
            "text": joined,
        }

    def _finish_target_selection(self, target: MapTarget,
                                 timeout: float = 2.8) -> bool:
        '确认详情；若同一地图坐标叠放多个入口，只选目标列表项。'
        deadline = self.ctx.logical_time() + max(0.0, timeout)
        list_candidate = None
        candidate_hits = 0
        while self.ctx.logical_time() < deadline and not self.ctx.should_stop():
            frame = self.ctx.grab()
            if frame is None:
                self.ctx.sleep(0.18)
                continue
            state = self._target_ui_state(frame, target)
            if state["matches"] and (state["action"] or state["detail_panel"]):
                self.ctx.log(
                    f"{self.name}:已确认 {target.name}，"
                    f"证据={state['action'] or '渡石详情'}，"
                    "未执行最终动作")
                return True
            if (state["matches"] and not state["action"] and
                    not state["detail_panel"]):
                
                
                candidate = max(state["matches"], key=lambda line: line[1])
                point = (candidate[1], candidate[2])
                if (list_candidate is not None and
                        np.hypot(point[0] - list_candidate[0],
                                 point[1] - list_candidate[1]) <= 0.02):
                    candidate_hits += 1
                else:
                    list_candidate = point
                    candidate_hits = 1
                if candidate_hits >= 2:
                    if not self.ctx.click(list_candidate):
                        return False
                    self.ctx.log(f"{self.name}:重叠点列表选择 {target.name}")
                    self.ctx.sleep(0.28)
                    
                    break
            self.ctx.sleep(0.18)

        if list_candidate is None or candidate_hits < 2:
            self.ctx.log(f"{self.name}:点击后未确认 {target.name} 详情或同名列表项")
            return False

        deadline = self.ctx.logical_time() + max(1.0, timeout)
        while self.ctx.logical_time() < deadline and not self.ctx.should_stop():
            frame = self.ctx.grab()
            if frame is not None:
                state = self._target_ui_state(frame, target)
                if state["matches"] and (state["action"] or state["detail_panel"]):
                    self.ctx.log(
                        f"{self.name}:列表项 {target.name} 已确认，"
                        f"证据={state['action'] or '渡石详情'}，未执行最终动作")
                    return True
            self.ctx.sleep(0.18)
        self.ctx.log(f"{self.name}:选择列表项后未出现 {target.name} 详情")
        return False

    def _select_target(self, name: str,
                       clickable: set[MapTargetStatus]) -> MapTarget | None:
        '打开并确认目标详情；clickable 决定允许点击哪些图标状态。'
        try:
            target = self.atlas.resolve(name)
        except (KeyError, FileNotFoundError) as exc:
            self.ctx.log(f"{self.name}:{exc}")
            return None
        if not self._open_and_prepare_target(target):
            return None
        observation = self.center_target(target, layer_ready=True)
        if observation is None:
            return None
        dev_log(
            f"[world map] target_observation target={name} "
            f"status={observation.status.value} point={observation.point}")
        if observation.target.kind is MapTargetKind.DESTINATION:
            self.ctx.log(f"{self.name}:{name} 是导航目标，不是可点击传送点")
            return None
        if observation.status not in clickable:
            self.ctx.log(
                f"{self.name}:{name} 当前状态={observation.status.value}，不点击")
            return None
        if not self.ctx.click(observation.point):
            return None
        self.ctx.log(
            f"{self.name}:已点击 {name} 点位(status={observation.status.value})")
        if not self._finish_target_selection(observation.target):
            return None
        return observation.target

    def click_target(self, name: str) -> bool:
        '点击并确认可用或未解锁点；不点“传送/前往追踪”。'
        target = self._select_target(
            name, {
                MapTargetStatus.AVAILABLE,
                MapTargetStatus.COORDINATE_VERIFIED,
                MapTargetStatus.LOCKED,
            })
        return target is not None

    def focus_source_coordinate(
            self, name: str, source: tuple[float, float], *, region: str = "",
            max_drags: int = 24) -> TargetObservation | None:
        '打开地图并把任意地上目的地移到中央，不点击目标。'
        try:
            target = self.atlas.destination(name, source, region)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            self.ctx.log(f"{self.name}:无法创建坐标目标 {name}: {exc}")
            return None
        if not self._open_and_prepare_target(target):
            return None
        observation = self.center_target(
            target, max_drags=max_drags, layer_ready=True)
        if (observation is None or
                observation.status is not MapTargetStatus.COORDINATE_VERIFIED):
            self.ctx.log(f"{self.name}:未能稳定居中坐标目标 {name}")
            return None
        self.ctx.log(
            f"{self.name}:已居中坐标目标 {name} source="
            f"({target.source[0]:.1f},{target.source[1]:.1f})，未点击")
        return observation

    def _wait_teleport_action(self, target: MapTarget,
                              timeout: float = 4.0) -> tuple[float, float] | None:
        '等待同名详情中的可传送动作，拒绝把“前往追踪”当成传送。'
        deadline = self.ctx.logical_time() + max(0.0, timeout)
        while self.ctx.logical_time() < deadline and not self.ctx.should_stop():
            frame = self.ctx.grab()
            if frame is not None:
                state = self._target_ui_state(frame, target)
                if state["matches"] and state["action"] == "前往追踪":
                    self.ctx.log(f"{self.name}:{target.name} 尚未解锁，只能前往追踪")
                    return None
                if (state["matches"] and
                        state["action"] in self.TELEPORT_ACTIONS and
                        state["action_point"] is not None):
                    return state["action_point"]
            self.ctx.sleep(0.18)
        self.ctx.log(f"{self.name}:{target.name} 详情中未找到可传送按钮")
        return None

    def teleport_to(self, name: str, *, timeout: float = 25.0,
                    arrival_predicate=None) -> bool:
        '传送到具名点，并等待角色 HUD 或调用方给定的到达判据。'
        target = self._select_target(name, {
            MapTargetStatus.AVAILABLE,
            MapTargetStatus.COORDINATE_VERIFIED,
            MapTargetStatus.LOCKED,
        })
        if target is None:
            return False
        action_point = self._wait_teleport_action(target)
        if action_point is None or not self.ctx.click(action_point):
            return False
        self.ctx.log(f"{self.name}:执行传送 → {target.name}")
        self._layer = None

        deadline = self.ctx.logical_time() + max(1.0, float(timeout))
        left_map = self.ctx.wait_until(
            lambda frame: not rec.in_world_map(frame),
            timeout=min(8.0, max(1.0, float(timeout))), interval=0.20,
            desc=f"离开地图({target.name})",
        )
        if not left_map:
            return False
        predicate = arrival_predicate or rec.in_world_hud
        remaining = max(0.5, deadline - self.ctx.logical_time())
        arrived = self.ctx.wait_until(
            predicate, timeout=remaining, interval=0.35,
            desc=f"传送到达({target.name})",
        )
        if not arrived:
            return False
        self.ctx.log(f"{self.name}:已到达 {target.name}")
        return True


def teleport_to(ctx, name: str, *, timeout: float = 25.0,
                arrival_predicate=None, atlas: WorldMapAtlas | None = None) -> bool:
    '一行调用世界地图传送：teleportto(ctx, "云根镇云舟栈")。'
    return WorldMapNavigator(ctx, atlas=atlas).teleport_to(
        name, timeout=timeout, arrival_predicate=arrival_predicate)


def focus_source_coordinate(
        ctx, name: str, source: tuple[float, float], *, region: str = "",
        max_drags: int = 24,
        atlas: WorldMapAtlas | None = None) -> TargetObservation | None:
    '通过一行调用把钓鱼、训练或寻路坐标移到全地图中央。'
    return WorldMapNavigator(ctx, atlas=atlas).focus_source_coordinate(
        name, source, region=region, max_drags=max_drags)
