'自动战斗 HUD 识别。'
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

import cv2
import numpy as np

from fishing.matcher import _get_ocr
from fishing.template_bank import crop, normalize


ULTIMATE_ROI = (0.890, 0.820, 0.975, 0.990)
COMBAT_CENTER_ROI = (0.250, 0.180, 0.750, 0.860)
BATTLE_RESULT_ROI = (0.420, 0.290, 0.580, 0.430)
MONSTER_HEALTH_ROI = (0.280, 0.025, 0.720, 0.110)
PLAYER_HEALTH_ROI = (0.390, 0.885, 0.530, 0.955)
COUNTER_PROMPT_ROI = COMBAT_CENTER_ROI
X_PROMPT_ROI = COUNTER_PROMPT_ROI
COMBAT_ROIS = (ULTIMATE_ROI, COMBAT_CENTER_ROI, MONSTER_HEALTH_ROI)
STUN_COMBAT_ROIS = COMBAT_ROIS
HEALTH_COMBAT_ROIS = (*COMBAT_ROIS, PLAYER_HEALTH_ROI)
MIN_COUNTER_SCORE = 0.50
MIN_COUNTER_GLYPH_SCORE = 0.70
MIN_COUNTER_GLYPH_MARGIN = 0.06
MIN_HEALTH_OCR_CONFIDENCE = 0.50
MIN_BATTLE_RESULT_OCR_CONFIDENCE = 0.50
_PLAYER_HEALTH_PATTERN = re.compile(
    r"(?<!\d)(\d{2,7})\s*[/／|\\]\s*(\d{3,7})(?!\d)"
)
_STUN_X_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "combat" / "stun_x.png"
)
_STUN_X_TEMPLATE = cv2.imread(str(_STUN_X_TEMPLATE_PATH), cv2.IMREAD_GRAYSCALE)


def _glyph_pattern(*rows: str) -> np.ndarray:
    return np.asarray(
        [[value == "1" for value in row] for row in rows],
        dtype=np.uint8,
    )


_COUNTER_GLYPH_TEMPLATES = {
    "z": _glyph_pattern(
        "1111111111111111111111111",
        "1111111111111111111111111",
        "0001111110001111111111100",
        "0001111110001111111111100",
        "0000000000000000011111100",
        "0000000000000000011111100",
        "0000000000000011111100000",
        "0000000000000011111100000",
        "0000000000001111111100000",
        "0000000000001111111100000",
        "0000000000001111100000000",
        "0000000000001111100000000",
        "0000000001111100000000000",
        "0000000001111100000000000",
        "0000001111111100000000000",
        "0000001111111100000000000",
        "0000001111110000000000000",
        "0000001111110000000000000",
        "0001111110000000000000000",
        "0001111110000000000000000",
        "1111111110000000000000000",
        "1111111110000000000000000",
        "1111111111111111111111111",
        "1111111111111111111111111",
        "1111111111111111111111111",
    ),
    "x": _glyph_pattern(
        "0001100000000000000011100",
        "0001100000000000000011100",
        "0001111100000000001111100",
        "0001111100000000001111100",
        "0001111111000000001111100",
        "0001111111000000001111100",
        "0000011111000001111100000",
        "0000011111000001111100000",
        "0000011111111001111100000",
        "0000000011111111110000000",
        "0000000011111111110000000",
        "0000000011111111110000000",
        "0000000011111111110000000",
        "0000000011111111110000000",
        "0000000011111111110000000",
        "0000000011111111110000000",
        "0000000011111111110000000",
        "0000011111000001111100000",
        "0000011111000001111100000",
        "0000011111000001111100000",
        "0001111100000000001111100",
        "0001111100000000001111100",
        "1111111100000000001111111",
        "1111111100000000001111111",
        "1111100000000000000011111",
    ),
}


@dataclass(frozen=True)
class UltimateState:
    available: bool
    color_score: float
    color_signature: tuple[float, ...]
    ring_score: float = 0.0
    icon_image: np.ndarray | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    ready_evidence: bool | None = None
    consumed_evidence: bool | None = None


@dataclass(frozen=True)
class RedFlashState:
    triggered: bool
    red_ratio: float
    component_ratio: float
    score: float


@dataclass(frozen=True)
class MonsterStunState:
    stunned: bool
    evidence: str
    x_score: float
    yellow_score: float


@dataclass(frozen=True)
class PlayerHealthState:
    current: int
    maximum: int
    confidence: float
    text: str


@dataclass(frozen=True)
class BattleResultState:
    outcome: str
    text: str
    confidence: float


def _prepare_ultimate_icon_image(sub: np.ndarray) -> np.ndarray | None:
    '把已裁好的大招区域转换成切人对比小图。'
    if sub is None or sub.size == 0:
        return None
    height, width = sub.shape[:2]
    cx, cy = width * 0.51, height * 0.63
    inner_radius = min(width, height) * 0.34 * 0.70
    x0 = max(0, round(cx - inner_radius))
    y0 = max(0, round(cy - inner_radius))
    x1 = min(width, round(cx + inner_radius))
    y1 = min(height, round(cy + inner_radius))
    icon = sub[y0:y1, x0:x1]
    if icon.size == 0 or min(icon.shape[:2]) < 4:
        return None
    gray = cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    return cv2.equalizeHist(gray)


def extract_ultimate_icon_image(frame) -> np.ndarray | None:
    '提取大招按钮内部图案，排除外圈进度后用于切人确认。'
    return _prepare_ultimate_icon_image(crop(frame, ULTIMATE_ROI))


def ultimate_icon_similarity(
    left: np.ndarray | None,
    right: np.ndarray | None,
) -> float:
    '比较两个大招内部图案；越接近一越可能是同一英雄。'
    if left is None or right is None or left.size == 0 or right.size == 0:
        return -1.0
    if left.shape != right.shape:
        right = cv2.resize(
            right,
            (left.shape[1], left.shape[0]),
            interpolation=cv2.INTER_AREA,
        )
    left_std = float(np.std(left))
    right_std = float(np.std(right))
    if left_std < 1e-6 or right_std < 1e-6:
        return 1.0 if np.array_equal(left, right) else 0.0
    return float(
        cv2.matchTemplate(
            left,
            right,
            cv2.TM_CCOEFF_NORMED,
        )[0, 0]
    )


def _has_battle_result_candidate(sub: np.ndarray) -> bool:
    '用中央大字的亮度和几何特征筛掉普通战斗帧。'
    if sub is None or sub.size == 0:
        return False
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    bright = (gray >= 185).astype(np.uint8)
    bright_ratio = float(np.mean(bright))
    if not 0.02 <= bright_ratio <= 0.35:
        return False
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        bright, 8
    )
    height, width = gray.shape[:2]
    min_area = max(20, round(gray.size * 0.003))
    glyphs = 0
    tallest = 0
    for stat in stats[1:count]:
        box_width = int(stat[cv2.CC_STAT_WIDTH])
        box_height = int(stat[cv2.CC_STAT_HEIGHT])
        area = int(stat[cv2.CC_STAT_AREA])
        if (
            area >= min_area
            and box_height >= height * 0.22
            and width * 0.02 <= box_width <= width * 0.45
        ):
            glyphs += 1
            tallest = max(tallest, box_height)
    return glyphs >= 2 and tallest >= height * 0.30


def detect_battle_result(frame) -> BattleResultState | None:
    '识别中央“得胜”或“惜败/失败”，无结果时返回空。'
    if frame is None:
        return None
    source = frame
    if getattr(frame, "crop_roi", None) is None:
        if getattr(frame, "shape", None) is None:
            return None
        source = normalize(frame)
    sub = crop(source, BATTLE_RESULT_ROI)
    if not _has_battle_result_candidate(sub):
        return None
    enlarged = cv2.resize(
        sub,
        None,
        fx=1.5,
        fy=1.5,
        interpolation=cv2.INTER_CUBIC,
    )
    try:
        result, _ = _get_ocr()(enlarged)
    except Exception:
        return None
    best: BattleResultState | None = None
    for item in result or []:
        try:
            text = str(item[1]).strip().replace(" ", "")
            confidence = float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if confidence < MIN_BATTLE_RESULT_OCR_CONFIDENCE:
            continue
        if "得胜" in text:
            state = BattleResultState("victory", text, confidence)
        elif "惜败" in text or "失败" in text:
            state = BattleResultState("defeat", text, confidence)
        else:
            continue
        if best is None or state.confidence > best.confidence:
            best = state
    return best


def parse_player_health_text(
    text: str,
    confidence: float = 1.0,
) -> PlayerHealthState | None:
    '从“当前生命/最大生命”文字中解析角色生命值。'
    compact = str(text or "").replace(",", "").replace("，", "")
    best: PlayerHealthState | None = None
    for match in _PLAYER_HEALTH_PATTERN.finditer(compact):
        current = int(match.group(1))
        maximum = int(match.group(2))
        if maximum < 1000 or current < 0 or current > maximum * 2:
            continue
        state = PlayerHealthState(
            current=current,
            maximum=maximum,
            confidence=float(confidence),
            text=match.group(0),
        )
        if best is None or state.maximum > best.maximum:
            best = state
    return best


def read_player_health(frame) -> PlayerHealthState | None:
    '只在切换角色时OCR底部生命值，并返回最大生命值证据。'
    if frame is None:
        return None
    source = frame
    if getattr(frame, "crop_roi", None) is None:
        source = normalize(frame)
    sub = crop(source, PLAYER_HEALTH_ROI)
    if sub is None or sub.size == 0:
        return None
    enlarged = cv2.resize(
        sub,
        None,
        fx=2.0,
        fy=2.0,
        interpolation=cv2.INTER_CUBIC,
    )
    try:
        result, _ = _get_ocr()(enlarged)
    except Exception:
        return None
    best: PlayerHealthState | None = None
    for item in result or []:
        try:
            text = str(item[1]).strip()
            confidence = float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if confidence < MIN_HEALTH_OCR_CONFIDENCE:
            continue
        state = parse_player_health_text(text, confidence)
        if state is None:
            continue
        if best is None or state.confidence > best.confidence:
            best = state
    return best


def monster_health_red_ratio(frame) -> float:
    '返回顶部怪物血条红色像素比例。'
    health = crop(frame, MONSTER_HEALTH_ROI)
    if health is None or health.size == 0:
        return 0.0
    height, width = health.shape[:2]
    red_band = health[
        round(height * 0.20):round(height * 0.50),
        round(width * 0.05):round(width * 0.95),
    ]
    if red_band.size == 0:
        return 0.0
    red_hsv = cv2.cvtColor(red_band, cv2.COLOR_BGR2HSV)
    red = (
        ((red_hsv[..., 0] < 9) | (red_hsv[..., 0] > 172))
        & (red_hsv[..., 1] > 55)
        & (red_hsv[..., 2] > 70)
    )
    return float(np.mean(red)) if red.size else 0.0


def detect_combat_health_bar(frame) -> bool:
    '通过顶部连续红色长条确认当前处于战斗 HUD。'
    health = crop(frame, MONSTER_HEALTH_ROI)
    if health is None or health.size == 0:
        return False
    height, width = health.shape[:2]
    red_band = health[
        round(height * 0.20):round(height * 0.50),
        round(width * 0.05):round(width * 0.95),
    ]
    if red_band.size == 0:
        return False
    red_hsv = cv2.cvtColor(red_band, cv2.COLOR_BGR2HSV)
    red = (
        ((red_hsv[..., 0] < 9) | (red_hsv[..., 0] > 172))
        & (red_hsv[..., 1] > 55)
        & (red_hsv[..., 2] > 70)
    ).astype(np.uint8)
    kernel_width = max(3, round(red.shape[1] * 0.01))
    connected = cv2.morphologyEx(
        red,
        cv2.MORPH_CLOSE,
        np.ones((1, kernel_width), dtype=np.uint8),
    )
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        connected,
        connectivity=8,
    )
    if count <= 1:
        return False
    component_widths = stats[1:, cv2.CC_STAT_WIDTH]
    component_heights = stats[1:, cv2.CC_STAT_HEIGHT]
    return bool(
        np.any(
            (component_widths >= red.shape[1] * 0.12)
            & (component_heights >= 2)
        )
    )


class MonsterStunDetector:
    '通过血条X图标或血条下方黄色条确认怪物昏迷。'

    X_THRESHOLD = 0.78
    X_CONFIRM_FRAMES = 1
    YELLOW_MIN_RATIO = 0.02
    YELLOW_MIN_DELTA = 0.02
    YELLOW_CONFIRM_FRAMES = 1
    MIN_HEALTH_RED_RATIO = 0.02
    TEMPLATE_SCALES = (0.75, 0.90, 1.0, 1.10, 1.25, 1.40)

    def __init__(self) -> None:
        self._x_frames = 0
        self._yellow_frames = 0

    @staticmethod
    def _band_ratio(hsv: np.ndarray, y0: float, y1: float) -> float:
        height = hsv.shape[0]
        sub = hsv[
            max(0, round(y0 * height)):min(height, round(y1 * height)),
            :,
        ]
        if sub.size == 0:
            return 0.0
        yellow = (
            (sub[..., 0] >= 12)
            & (sub[..., 0] <= 42)
            & (sub[..., 1] >= 70)
            & (sub[..., 2] >= 125)
        )
        return float(np.mean(yellow))

    @staticmethod
    def _template_score(gray: np.ndarray) -> float:
        template = _STUN_X_TEMPLATE
        if template is None or gray.size == 0:
            return 0.0
        best = 0.0
        for scale in MonsterStunDetector.TEMPLATE_SCALES:
            width = max(1, round(template.shape[1] * scale))
            height = max(1, round(template.shape[0] * scale))
            if width > gray.shape[1] or height > gray.shape[0]:
                continue
            resized = (
                template
                if scale == 1.0
                else cv2.resize(
                    template,
                    (width, height),
                    interpolation=cv2.INTER_AREA,
                )
            )
            score = float(
                cv2.matchTemplate(
                    gray,
                    resized,
                    cv2.TM_CCOEFF_NORMED,
                ).max()
            )
            best = max(best, score)
        return best

    def update(self, frame) -> MonsterStunState:
        health = crop(frame, MONSTER_HEALTH_ROI)
        if health is None or health.size == 0:
            self._x_frames = 0
            self._yellow_frames = 0
            return MonsterStunState(False, "", 0.0, 0.0)
        _frame_height, frame_width = frame.shape[:2]
        if frame_width != 1920:
            scale = 1920.0 / max(1, frame_width)
            health = cv2.resize(
                health,
                (
                    max(1, round(health.shape[1] * scale)),
                    max(1, round(health.shape[0] * scale)),
                ),
                interpolation=(
                    cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                ),
            )
        height, width = health.shape[:2]
        gray = cv2.cvtColor(health, cv2.COLOR_BGR2GRAY)
        x_score = self._template_score(gray)

        health_visible = bool(
            monster_health_red_ratio(frame) >= self.MIN_HEALTH_RED_RATIO
        )

        gauge = health[
            :,
            round(width * 0.04):round(width * 0.96),
        ]
        gauge_hsv = cv2.cvtColor(gauge, cv2.COLOR_BGR2HSV)
        yellow_center = self._band_ratio(gauge_hsv, 0.48, 0.64)
        yellow_context = max(
            self._band_ratio(gauge_hsv, 0.34, 0.45),
            self._band_ratio(gauge_hsv, 0.67, 0.80),
        )
        yellow_score = yellow_center - yellow_context

        x_present = x_score >= self.X_THRESHOLD
        yellow_present = bool(
            health_visible
            and yellow_center >= self.YELLOW_MIN_RATIO
            and yellow_score >= self.YELLOW_MIN_DELTA
        )
        self._x_frames = self._x_frames + 1 if x_present else 0
        self._yellow_frames = self._yellow_frames + 1 if yellow_present else 0

        if self._x_frames >= self.X_CONFIRM_FRAMES:
            return MonsterStunState(True, "血条X", x_score, yellow_score)
        if self._yellow_frames >= self.YELLOW_CONFIRM_FRAMES:
            return MonsterStunState(True, "黄色昏迷条", x_score, yellow_score)
        return MonsterStunState(False, "", x_score, yellow_score)


class UltimateAvailabilityDetector:
    '同时检查大招图标着色和外圈完整度，避免蓄力将满时提前触发。'

    READY_ON = 0.42
    READY_OFF = 0.30
    RING_READY_ON = 0.60
    RING_READY_OFF = 0.45
    RING_SECTORS = 36

    def __init__(self) -> None:
        self._available = False

    def update(self, frame) -> UltimateState:
        sub = crop(frame, ULTIMATE_ROI)
        if sub is None or sub.size == 0:
            self._available = False
            return UltimateState(
                False,
                0.0,
                (),
                ready_evidence=False,
                consumed_evidence=True,
            )
        icon_image = _prepare_ultimate_icon_image(sub)
        hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
        height, width = hsv.shape[:2]
        yy, xx = np.ogrid[:height, :width]
        cx, cy = width * 0.51, height * 0.63
        radius = min(width, height) * 0.34
        distance_squared = (xx - cx) ** 2 + (yy - cy) ** 2
        circle = distance_squared <= radius ** 2
        saturation = hsv[..., 1][circle]
        value = hsv[..., 2][circle]
        if saturation.size == 0:
            self._available = False
            return UltimateState(
                False,
                0.0,
                (),
                icon_image=icon_image,
                ready_evidence=False,
                consumed_evidence=True,
            )

        visible = value > 55
        colored = (saturation > 55) & visible
        colored_ratio = float(np.mean(colored))
        saturation_strength = float(np.mean(saturation[visible]) / 255.0) if np.any(visible) else 0.0
        score = 0.78 * colored_ratio + 0.22 * saturation_strength
        ring_score = self._ring_coverage(
            hsv,
            xx,
            yy,
            cx,
            cy,
            radius,
            distance_squared,
        )
        visible_ratio = float(np.mean(visible))
        ready_evidence = bool(
            score >= self.READY_ON
            and ring_score >= self.RING_READY_ON
            and visible_ratio >= 0.10
        )
        consumed_evidence = bool(
            score < self.READY_OFF
            or ring_score < self.RING_READY_OFF
            or visible_ratio < 0.10
        )
        self._available = (
            not consumed_evidence
            if self._available
            else ready_evidence
        )

        weighted = circle & (hsv[..., 1] > 55) & (hsv[..., 2] > 55)
        hist = cv2.calcHist([hsv], [0], weighted.astype(np.uint8), [18], [0, 180])
        total = float(hist.sum())
        signature = tuple(float(v) for v in (hist[:, 0] / total)) if total > 0 else ()
        return UltimateState(
            self._available,
            score,
            signature,
            ring_score,
            icon_image,
            ready_evidence,
            consumed_evidence,
        )

    @classmethod
    def _ring_coverage(
        cls,
        hsv: np.ndarray,
        xx: np.ndarray,
        yy: np.ndarray,
        cx: float,
        cy: float,
        radius: float,
        distance_squared: np.ndarray,
    ) -> float:
        '计算外圈各方向被彩色光环覆盖的比例。'
        ring = (
            (distance_squared >= (radius * 0.82) ** 2)
            & (distance_squared <= (radius * 1.08) ** 2)
        )
        if not np.any(ring):
            return 0.0
        colored = (
            ring
            & (hsv[..., 1] > 55)
            & (hsv[..., 2] > 80)
        )
        angles = (
            np.arctan2(yy - cy, xx - cx) + 2.0 * np.pi
        ) % (2.0 * np.pi)
        sector_ids = np.minimum(
            cls.RING_SECTORS - 1,
            (angles * cls.RING_SECTORS / (2.0 * np.pi)).astype(np.int32),
        )
        ring_counts = np.bincount(
            sector_ids[ring],
            minlength=cls.RING_SECTORS,
        )
        colored_counts = np.bincount(
            sector_ids[colored],
            minlength=cls.RING_SECTORS,
        )
        sector_coverage = np.divide(
            colored_counts,
            ring_counts,
            out=np.zeros(cls.RING_SECTORS, dtype=np.float32),
            where=ring_counts > 0,
        )
        return float(np.mean(sector_coverage >= 0.15))


class RedFlashDetector:
    '通过中央区域突增的横向红色警示线识别闪避时机。'

    MAX_PROCESSING_WIDTH = 640
    
    MIN_RED_RATIO = 0.0020
    MIN_COMPONENT_RATIO = 0.00025
    MIN_HORIZONTAL_WIDTH_RATIO = 0.22
    BASELINE_ALPHA = 0.08
    REARM_FRAMES = 4

    def __init__(self) -> None:
        self._baseline: float | None = None
        self._armed = True
        self._absent_frames = 0

    def update(self, frame) -> RedFlashState:
        sub = crop(frame, COMBAT_CENTER_ROI)
        if sub is None or sub.size == 0:
            return RedFlashState(False, 0.0, 0.0, 0.0)
        if sub.shape[1] > self.MAX_PROCESSING_WIDTH:
            scale = self.MAX_PROCESSING_WIDTH / sub.shape[1]
            sub = cv2.resize(
                sub,
                (
                    self.MAX_PROCESSING_WIDTH,
                    max(1, round(sub.shape[0] * scale)),
                ),
                interpolation=cv2.INTER_AREA,
            )
        hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
        blue, green, red = cv2.split(sub)
        hue = hsv[..., 0]
        saturation = hsv[..., 1]
        value = hsv[..., 2]
        red_light = (
            ((hue < 8) | (hue > 173))
            & (saturation > 145)
            & (value > 155)
            & (red.astype(np.float32) > green.astype(np.float32) * 1.25 + 12)
            & (red.astype(np.float32) > blue.astype(np.float32) * 1.25 + 12)
        ).astype(np.uint8)
        red_ratio = float(np.mean(red_light))
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            red_light, 8
        )
        largest = (
            int(stats[1:, cv2.CC_STAT_AREA].max())
            if count > 1
            else 0
        )
        component_ratio = largest / max(1, red_light.size)
        horizontal_width = 0
        for stat in stats[1:]:
            width = int(stat[cv2.CC_STAT_WIDTH])
            height = int(stat[cv2.CC_STAT_HEIGHT])
            area = int(stat[cv2.CC_STAT_AREA])
            if (
                width >= sub.shape[1] * 0.04
                and height <= sub.shape[0] * 0.05
                and width / max(1, height) >= 4.0
                and area / max(1, width * height) >= 0.15
            ):
                horizontal_width += width
        horizontal_width_ratio = horizontal_width / max(1, sub.shape[1])
        if self._baseline is None:
            self._baseline = min(red_ratio, self.MIN_RED_RATIO * 1.5)
            return RedFlashState(False, red_ratio, component_ratio, 0.0)

        threshold = max(
            self.MIN_RED_RATIO,
            self._baseline * 1.35,
            self._baseline + 0.0015,
        )
        present = bool(
            red_ratio >= threshold
            and component_ratio >= self.MIN_COMPONENT_RATIO
            and horizontal_width_ratio >= self.MIN_HORIZONTAL_WIDTH_RATIO
        )
        triggered = bool(present and self._armed)
        if present:
            self._armed = False
            self._absent_frames = 0
        else:
            self._absent_frames += 1
            if self._absent_frames >= self.REARM_FRAMES:
                self._armed = True
        score = min(
            1.0,
            0.35 * red_ratio / max(threshold, 1e-6)
            + 0.25 * component_ratio / self.MIN_COMPONENT_RATIO
            + 0.40 * horizontal_width_ratio / self.MIN_HORIZONTAL_WIDTH_RATIO,
        )
        alpha = 0.01 if present else self.BASELINE_ALPHA
        baseline_sample = min(red_ratio, self.MIN_RED_RATIO * 1.5)
        self._baseline = (
            (1.0 - alpha) * self._baseline
            + alpha * baseline_sample
        )
        return RedFlashState(triggered, red_ratio, component_ratio, score)


def signature_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    '返回两个大招主色分布的差异，供切换英雄后重新触发大招判断。'
    if not left or not right or len(left) != len(right):
        return 1.0
    return 0.5 * float(np.abs(np.asarray(left) - np.asarray(right)).sum())


def detect_counter_key(frame) -> tuple[str | None, float]:
    '在中央战斗区搜索带圆环的 X/Z 键帽并区分实际按键。'
    normalized = normalize(frame)
    sub = crop(normalized, COUNTER_PROMPT_ROI)
    if sub is None or sub.size == 0:
        return None, 0.0
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    _, white = cv2.threshold(gray, 190, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(
        white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    frame_height, frame_width = normalized.shape[:2]
    offset_x = int(COUNTER_PROMPT_ROI[0] * frame_width)
    offset_y = int(COUNTER_PROMPT_ROI[1] * frame_height)
    best_key = None
    best_score = 0.0
    for contour in contours:
        x, y, box_w, box_h = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if not (
            15 <= box_w <= 40
            and 16 <= box_h <= 42
            and 0.62 <= box_w / max(1, box_h) <= 1.15
            and area >= 180
            and area / max(1, box_w * box_h) >= 0.62
        ):
            continue
        patch = gray[y:y + box_h, x:x + box_w]
        key, glyph_score = _classify_counter_glyph(patch)
        if key is None:
            continue
        center_x = offset_x + x + box_w / 2
        key_center_y = offset_y + y + box_h / 2
        ring_score = _counter_ring_score(
            normalized, center_x, key_center_y)
        score = min(glyph_score, ring_score)
        if score > best_score:
            best_key = key
            best_score = score
    if best_score < MIN_COUNTER_SCORE:
        return None, best_score
    return best_key, best_score


def _classify_counter_glyph(patch) -> tuple[str | None, float]:
    glyph = _extract_counter_glyph(patch)
    if glyph is None:
        return None, 0.0
    scores = {
        key: _counter_glyph_similarity(glyph, template)
        for key, template in _COUNTER_GLYPH_TEMPLATES.items()
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    key, best_score = ranked[0]
    second_score = ranked[1][1]
    if (
        best_score < MIN_COUNTER_GLYPH_SCORE
        or best_score - second_score < MIN_COUNTER_GLYPH_MARGIN
    ):
        return None, best_score
    return key, best_score


def _extract_counter_glyph(patch) -> np.ndarray | None:
    '提取键帽内的暗色字形并统一到实机标定尺寸。'
    height, width = patch.shape[:2]
    y0, y1 = max(1, int(height * 0.12)), max(2, int(height * 0.88))
    x0, x1 = max(1, int(width * 0.12)), max(2, int(width * 0.88))
    inner = patch[y0:y1, x0:x1]
    dark = inner < 150
    ys, xs = np.where(dark)
    if len(xs) < 8:
        return None
    glyph = dark[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    return cv2.resize(
        glyph.astype(np.uint8), (25, 25), interpolation=cv2.INTER_NEAREST)


def _counter_glyph_similarity(
    glyph: np.ndarray,
    template: np.ndarray,
) -> float:
    '用 Dice 相似度区分真实 X/Z 与粒子、锁定菱形。'
    overlap = int(np.count_nonzero(glyph & template))
    total = int(np.count_nonzero(glyph) + np.count_nonzero(template))
    return 2.0 * overlap / max(1, total)


def _counter_ring_score(frame, center_x: float, key_center_y: float) -> float:
    height, width = frame.shape[:2]
    best_ratio = 0.0
    
    for offset_ratio in (0.043, 0.047, 0.051, 0.055):
        center_y = key_center_y - offset_ratio * height
        y0 = max(0, int(center_y - 0.048 * height))
        y1 = min(height, int(center_y + 0.048 * height))
        x0 = max(0, int(center_x - 0.048 * height))
        x1 = min(width, int(center_x + 0.048 * height))
        if x1 <= x0 or y1 <= y0:
            continue
        crop_box = getattr(frame, "crop_box", None)
        patch = (
            crop_box((x0, y0, x1, y1))
            if crop_box is not None
            else frame[y0:y1, x0:x1]
        )
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        yy, xx = np.ogrid[:patch.shape[0], :patch.shape[1]]
        local_cx, local_cy = center_x - x0, center_y - y0
        distance = np.sqrt((xx - local_cx) ** 2 + (yy - local_cy) ** 2)
        ring = (distance >= 0.026 * height) & (distance <= 0.044 * height)
        if not np.any(ring):
            continue
        saturation = hsv[..., 1]
        value = hsv[..., 2]
        visible = (gray >= 120) | ((saturation >= 45) & (value >= 80))
        best_ratio = max(best_ratio, float(np.mean(visible[ring])))
    return min(1.0, best_ratio / 0.45)


def detect_counter_prompt(frame) -> tuple[bool, float]:
    '兼容布尔接口，X 或 Z 任一有效提示均返回命中。'
    key, score = detect_counter_key(frame)
    return key is not None, score


def detect_x_prompt(frame) -> tuple[bool, float]:
    '兼容旧接口，仅在实际识别到 X 键帽时返回命中。'
    key, score = detect_counter_key(frame)
    return key == "x", score if key == "x" else 0.0
