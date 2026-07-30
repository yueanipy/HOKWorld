'牧场界面、动物窝和操作按钮识别。'
from __future__ import annotations

import re
from dataclasses import dataclass
from math import atan2, degrees, hypot
from pathlib import Path

import cv2
import numpy as np

from daily import recognizer as rec


ROI_RANCH_NODE = (0.32, 0.40, 0.50, 0.62)
ROI_WORLD_TEXT = (0.20, 0.45, 1.00, 0.99)
ROI_INTIMACY = (0.25, 0.15, 0.75, 0.72)
ROI_FEED_TITLE = (0.20, 0.18, 0.80, 0.35)
ROI_FEED_ADD = (0.505, 0.67, 0.69, 0.735)
ROI_FEED_CONTROL = (0.88, 0.83, 0.985, 0.995)
ROI_FEED_COST = (0.90, 0.91, 0.985, 0.995)
ROI_GOLD_ACTION = (0.87, 0.82, 0.98, 0.98)
ROI_ACTION_TEXT = (0.78, 0.88, 1.00, 1.00)
ROI_STALL_GROUND = (0.12, 0.70, 0.78, 0.99)
PT_STALL_CONTROL_CENTER = (0.932, 0.928)

MARKER_X_MIN = 0.25
MARKER_X_MAX = 0.75
MARKER_Y_MIN = 0.62
MARKER_Y_MAX = 0.84

PT_RANCH_NODE = (0.424, 0.613)
PT_FEED_ADD = (0.596, 0.711)
PT_FEED_CLOSE = (0.755, 0.272)

FEED_INACTIVE_TEMPLATE_MIN = 0.78
FEED_ACTIVE_TEMPLATE_MIN = 0.64
HAND_ACTION_TEMPLATE_MIN = 0.64
STALL_GROUND_MIN_PIXELS = 650
STALL_GROUND_LONG_EDGE_PX = 90
STALL_GROUND_SHORT_EDGE_PX = 60
STALL_GROUND_STRONG_EDGE_PX = 180
STALL_GROUND_BOTTOM_RATIO = 0.62
STALL_GROUND_PAIR_SEPARATION_RATIO = 0.18
STALL_GROUND_JOIN_DISTANCE_RATIO = 0.12

_ACTION_TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent / "assets" / "daily"
)
_ACTION_TEMPLATE_PATHS = {
    "inactive": _ACTION_TEMPLATE_DIR / "ranch_feed_inactive.png",
    "feed_active": _ACTION_TEMPLATE_DIR / "ranch_feed_active.png",
    "hand": _ACTION_TEMPLATE_DIR / "ranch_hand_action.png",
}
_ACTION_TEMPLATE_EDGES: dict[str, np.ndarray] = {}

_TIMER_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
_FEED_DIGIT_TRANSLATION = str.maketrans({
    "０": "0",
    "１": "1",
    "２": "2",
    "３": "3",
    "４": "4",
    "５": "5",
    "６": "6",
    "７": "7",
    "８": "8",
    "９": "9",
    "I": "1",
    "l": "1",
    "|": "1",
    "O": "0",
    "o": "0",
})
_FEED_COST_ROW_TOLERANCE = 0.012


@dataclass(frozen=True)
class RanchObservation:
    '保存当前动物窝的文字与操作状态。'

    marker_text: str
    marker_key: str
    action_text: str
    feed_cost: int | None
    feed_control: bool
    feed_active: bool
    breed_action: bool
    hand_action: bool
    gold_action: bool
    locked: bool
    outside: bool
    action: str
    summon_available: bool
    summon_countdown: bool


def ranch_node_point(frame) -> tuple[float, float] | None:
    '返回管理地图中“牧场”下方的传送热区。'
    for text, cx, cy in rec.ocr_lines(frame, ROI_RANCH_NODE):
        if "牧场" in text:
            return (cx, min(0.75, cy + 0.11))
    return None


def intimacy_overlay(frame) -> bool:
    '识别召集动物后可能出现的亲密度浮层。'
    return "亲密度" in rec.ocr_text(frame, ROI_INTIMACY)


def feed_dialog_open(frame) -> bool:
    '识别区域饲料添加界面。'
    text = rec.ocr_text(frame, ROI_FEED_TITLE)
    return "添加饲料" in text or ("饲料" in text and "区域" in text)


def feed_add_active(frame) -> bool:
    '用按钮金色底判断“添加”是否可点击。'
    image = rec.normalize(frame)
    height, width = image.shape[:2]
    x0, y0, x1, y1 = ROI_FEED_ADD
    crop = image[
        int(y0 * height):int(y1 * height),
        int(x0 * width):int(x1 * width),
    ]
    if crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(
        hsv,
        np.array([10, 30, 130], np.uint8),
        np.array([45, 255, 255], np.uint8),
    )
    return cv2.countNonZero(gold) >= 1000


def feed_add_point(frame) -> tuple[float, float]:
    '返回“添加”文字中心，识别失败时使用录制坐标。'
    return rec.find_word(frame, ROI_FEED_ADD, "添加") or PT_FEED_ADD


def _gold_action_visible(image) -> bool:
    height, width = image.shape[:2]
    x0, y0, x1, y1 = ROI_GOLD_ACTION
    crop = image[
        int(y0 * height):int(y1 * height),
        int(x0 * width):int(x1 * width),
    ]
    if crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(
        hsv,
        np.array([12, 80, 140], np.uint8),
        np.array([42, 255, 255], np.uint8),
    )
    return cv2.countNonZero(gold) >= 80


def feed_control_visible(frame) -> bool:
    '识别最右下角灰暗或发光的喂食按钮。'
    scores = action_template_scores(frame)
    return bool(
        scores.get("inactive", 0.0) >= FEED_INACTIVE_TEMPLATE_MIN
        or scores.get("feed_active", 0.0) >= FEED_ACTIVE_TEMPLATE_MIN
    )


def _action_template_edges(name: str) -> np.ndarray | None:
    '读取并缓存牧场右下角动作图标模板。'
    cached = _ACTION_TEMPLATE_EDGES.get(name)
    if cached is not None:
        return cached
    path = _ACTION_TEMPLATE_PATHS.get(name)
    if path is None:
        return None
    template = cv2.imread(
        str(path),
        cv2.IMREAD_GRAYSCALE,
    )
    if template is None or template.size == 0:
        return None
    edges = cv2.Canny(template, 40, 120)
    _ACTION_TEMPLATE_EDGES[name] = edges
    return edges


def action_template_scores(frame) -> dict[str, float]:
    '一次裁图返回全部牧场动作图标模板分数。'
    image = rec.normalize(frame)
    height, width = image.shape[:2]
    x0, y0, x1, y1 = ROI_FEED_CONTROL
    control = image[
        int(y0 * height):int(y1 * height),
        int(x0 * width):int(x1 * width),
    ]
    if control.size == 0:
        return {}
    edges = cv2.Canny(cv2.cvtColor(control, cv2.COLOR_BGR2GRAY), 40, 120)
    result: dict[str, float] = {}
    for name in _ACTION_TEMPLATE_PATHS:
        template = _action_template_edges(name)
        if template is None:
            result[name] = 0.0
            continue
        th, tw = template.shape[:2]
        if edges.shape[0] < th or edges.shape[1] < tw:
            result[name] = 0.0
            continue
        scores = cv2.matchTemplate(edges, template, cv2.TM_CCOEFF_NORMED)
        result[name] = float(cv2.minMaxLoc(scores)[1])
    return result


def action_template_score(frame, name: str) -> float:
    '返回指定牧场动作图标的模板分数。'
    return action_template_scores(frame).get(name, 0.0)


def feed_inactive_visible(frame) -> bool:
    '识别最右下角灰暗禁止喂食图标。'
    return feed_inactive_score(frame) >= FEED_INACTIVE_TEMPLATE_MIN


def feed_inactive_score(frame) -> float:
    '返回灰暗禁止喂食图标模板分数。'
    return action_template_score(frame, "inactive")


def feed_active_visible(frame) -> bool:
    '识别最右下角发光的可喂食图标。'
    return feed_active_score(frame) >= FEED_ACTIVE_TEMPLATE_MIN


def feed_active_score(frame) -> float:
    '返回发光可喂食图标模板分数。'
    return action_template_score(frame, "feed_active")


def breed_action_visible(frame) -> bool:
    '在固定交互区识别发光的“催产后可收获”文字。'
    image = rec.normalize(frame)
    text = rec.ocr_text(image, ROI_ACTION_TEXT)
    return _breed_text_visible(text) and _gold_action_visible(image)


def hand_action_visible(frame) -> bool:
    '识别右下角发光的手型采集图标。'
    return (
        action_template_score(frame, "hand")
        >= HAND_ACTION_TEMPLATE_MIN
    )


def stall_control_signature(frame) -> float:
    '返回右侧圆形交互按钮的径向结构评分。'
    image = rec.normalize(frame)
    height, width = image.shape[:2]
    base_x = int(PT_STALL_CONTROL_CENTER[0] * width)
    base_y = int(PT_STALL_CONTROL_CENTER[1] * height)
    radius = 65
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    yy, xx = np.ogrid[:radius * 2 + 1, :radius * 2 + 1]
    distance = np.sqrt((xx - radius) ** 2 + (yy - radius) ** 2)
    inner_mask = distance < 34
    outer_mask = (distance >= 52) & (distance < 62)
    best = 0.0
    for offset_y in (-18, -9, 0, 9, 18):
        for offset_x in (-18, -9, 0, 9, 18):
            center_x = base_x + offset_x
            center_y = base_y + offset_y
            x0, x1 = center_x - radius, center_x + radius + 1
            y0, y1 = center_y - radius, center_y + radius + 1
            if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
                continue
            control = gray[y0:y1, x0:x1]
            inner = control[inner_mask]
            outer = control[outer_mask]
            icon_contrast = max(
                0.0,
                float(np.mean(inner) - np.mean(outer)),
            )
            best = max(
                best,
                float(np.std(inner) + icon_contrast),
            )
    return best


def stall_control_changed(frame, baseline: float | None) -> bool:
    '兼容旧调用，识别任意动物窝操作状态。'
    del baseline
    return stall_action_state_visible(frame)


def stall_action_state_visible(frame) -> bool:
    '识别喂食、催产或收获中的任意右下角操作状态。'
    image = rec.normalize(frame)
    scores = action_template_scores(image)
    return bool(
        scores.get("inactive", 0.0) >= FEED_INACTIVE_TEMPLATE_MIN
        or scores.get("feed_active", 0.0) >= FEED_ACTIVE_TEMPLATE_MIN
        or scores.get("hand", 0.0) >= HAND_ACTION_TEMPLATE_MIN
        or _gold_action_visible(image)
    )


def stall_ground_evidence(frame) -> tuple[bool, bool]:
    '返回完整窝框和进入画面下部的单条强边证据。'
    image = rec.normalize(frame)
    height, width = image.shape[:2]
    x0, y0, x1, y1 = ROI_STALL_GROUND
    crop = image[
        int(y0 * height):int(y1 * height),
        int(x0 * width):int(x1 * width),
    ]
    if crop.size == 0:
        return False, False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(
        hsv,
        np.array([90, 130, 170], np.uint8),
        np.array([112, 255, 255], np.uint8),
    )
    red_low = cv2.inRange(
        hsv,
        np.array([0, 130, 160], np.uint8),
        np.array([8, 255, 255], np.uint8),
    )
    red_high = cv2.inRange(
        hsv,
        np.array([170, 130, 160], np.uint8),
        np.array([179, 255, 255], np.uint8),
    )
    colored = cv2.bitwise_or(
        blue,
        cv2.bitwise_or(red_low, red_high),
    )
    colored = cv2.morphologyEx(
        colored,
        cv2.MORPH_CLOSE,
        np.ones((3, 3), np.uint8),
    )
    if cv2.countNonZero(colored) < STALL_GROUND_MIN_PIXELS:
        return False, False
    lines = cv2.HoughLinesP(
        colored,
        1,
        np.pi / 180,
        threshold=30,
        minLineLength=55,
        maxLineGap=20,
    )
    if lines is None:
        return False, False
    positive_lines = []
    negative_lines = []
    bottom_limit = crop.shape[0] * STALL_GROUND_BOTTOM_RATIO
    strong_lower_edge = False
    for x0, y0, x1, y1 in lines[:, 0]:
        delta_x = float(x1 - x0)
        delta_y = float(y1 - y0)
        length = hypot(delta_x, delta_y)
        angle = abs(degrees(atan2(delta_y, delta_x)))
        angle = min(angle, 180.0 - angle)
        if angle < 18.0:
            continue
        line = (
            length,
            (float(x0) + float(x1)) * 0.5,
            float(max(y0, y1)),
            (float(x0), float(y0)),
            (float(x1), float(y1)),
        )
        if length >= STALL_GROUND_STRONG_EDGE_PX and line[2] >= bottom_limit:
            strong_lower_edge = True
        slope_sign = delta_x * delta_y
        if slope_sign > 0:
            positive_lines.append(line)
        elif slope_sign < 0:
            negative_lines.append(line)

    crop_width = float(crop.shape[1])
    minimum_separation = crop_width * STALL_GROUND_PAIR_SEPARATION_RATIO
    maximum_join_distance = crop_width * STALL_GROUND_JOIN_DISTANCE_RATIO
    for positive in positive_lines:
        for negative in negative_lines:
            long_edge = max(positive[0], negative[0])
            short_edge = min(positive[0], negative[0])
            if (
                long_edge < STALL_GROUND_LONG_EDGE_PX
                or short_edge < STALL_GROUND_SHORT_EDGE_PX
            ):
                continue
            center_separation = abs(positive[1] - negative[1])
            endpoint_distance = min(
                hypot(px - nx, py - ny)
                for px, py in positive[3:]
                for nx, ny in negative[3:]
            )
            both_reach_lower_area = (
                positive[2] >= bottom_limit
                and negative[2] >= bottom_limit
            )
            joined_long_pair = (
                endpoint_distance <= maximum_join_distance
                and long_edge >= STALL_GROUND_STRONG_EDGE_PX
            )
            if (
                (both_reach_lower_area or joined_long_pair)
                and (
                    center_separation >= minimum_separation
                    or endpoint_distance <= maximum_join_distance
                )
            ):
                return True, strong_lower_edge
    return False, strong_lower_edge


def stall_ground_box_visible(frame) -> bool:
    '识别具有成对透视边的蓝色或红色动物窝框。'
    box_visible, _strong_lower_edge = stall_ground_evidence(frame)
    return box_visible


def _marker_key(text: str) -> str:
    cleaned = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    for state_word in ("催产后可收获", "可收获", "小时"):
        cleaned = cleaned.replace(state_word, "")
    return cleaned


def _feed_cost_from_lines(
        lines: list[tuple[str, float, float]],
        ) -> int | None:
    '合并同一行数字片段并读取喂食消耗绝对值。'
    fragments: list[tuple[float, float, str]] = []
    x0, y0, x1, y1 = ROI_FEED_COST
    for text, cx, cy in lines:
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue
        if not re.search(r"\d", text) and not any(
                sign in text for sign in ("-", "−", "﹣")):
            continue
        normalized = text.translate(_FEED_DIGIT_TRANSLATION)
        if not re.search(r"\d", normalized):
            continue
        digits = "".join(re.findall(r"\d", normalized))
        if digits:
            fragments.append((cx, cy, digits))
    if not fragments:
        return None

    rows: list[list[tuple[float, float, str]]] = []
    for fragment in sorted(fragments, key=lambda item: (item[1], item[0])):
        for row in rows:
            average_y = sum(item[1] for item in row) / len(row)
            if abs(fragment[1] - average_y) <= _FEED_COST_ROW_TOLERANCE:
                row.append(fragment)
                break
        else:
            rows.append([fragment])

    values: list[int] = []
    for row in rows:
        joined = "".join(
            item[2] for item in sorted(row, key=lambda item: item[0])
        )
        if joined:
            values.append(int(joined))
    return max(values) if values else None


def _breed_text_visible(text: str) -> bool:
    '允许 OCR 拆字，但必须同时命中催产和收获语义。'
    return "催产" in text and "收获" in text


def stall_arrival_state_visible(observation: RanchObservation) -> bool:
    '判断最右下角是否已出现动物窝专用状态。'
    return bool(
        getattr(observation, "feed_control", False)
        or getattr(observation, "feed_active", False)
        or getattr(observation, "feed_cost", None) is not None
        or getattr(observation, "breed_action", False)
        or getattr(observation, "hand_action", False)
        or getattr(observation, "action", "") in ("feed", "breed", "hand")
    )


def observe(frame) -> RanchObservation:
    '同帧识别动物浮标、召集倒计时和右下角操作状态。'
    lines = rec.ocr_lines(frame, ROI_WORLD_TEXT)
    marker_parts: list[str] = []
    action_parts: list[str] = []
    summon_number = False
    action_x0, action_y0, action_x1, action_y1 = ROI_ACTION_TEXT
    for text, cx, cy in lines:
        if (
            MARKER_X_MIN <= cx < MARKER_X_MAX
            and MARKER_Y_MIN <= cy <= MARKER_Y_MAX
        ):
            marker_parts.append(text)
        if (
            action_x0 <= cx <= action_x1
            and action_y0 <= cy <= action_y1
        ):
            action_parts.append(text)
            if cx < 0.90 and re.search(r"\d+", text):
                summon_number = True

    marker_text = "".join(marker_parts)
    action_text = "".join(action_parts)
    feed_cost = _feed_cost_from_lines(lines)
    has_marker = bool(_TIMER_RE.search(marker_text) or "可收获" in marker_text)
    marker_key = _marker_key(marker_text) if has_marker else ""
    image = rec.normalize(frame)
    template_scores = action_template_scores(image)
    feed_inactive = (
        template_scores.get("inactive", 0.0)
        >= FEED_INACTIVE_TEMPLATE_MIN
    )
    feed_active = (
        template_scores.get("feed_active", 0.0)
        >= FEED_ACTIVE_TEMPLATE_MIN
    )
    feed_control = feed_inactive or feed_active
    gold_action = _gold_action_visible(image)
    breed_action = _breed_text_visible(action_text) and gold_action
    hand_action = (
        template_scores.get("hand", 0.0)
        >= HAND_ACTION_TEMPLATE_MIN
        or "可收获" in marker_text
    )
    feed_state = feed_control or feed_cost is not None
    has_stall_action_state = feed_state or breed_action or hand_action
    locked_text = "解锁" in "".join(text for text, _, _ in lines)
    outside = bool(
        "更换动物" in action_text
        and not has_marker
        and not has_stall_action_state
    )
    locked = bool(
        locked_text
        and not outside
        and not has_stall_action_state
    )

    if breed_action:
        action = "breed"
    elif hand_action:
        action = "hand"
    elif feed_state:
        action = "feed"
    elif outside:
        action = "inactive"
    elif locked:
        action = "locked"
    elif has_marker:
        action = "waiting"
    else:
        action = "unknown"

    summon_available = (
        "召集动物" in action_text
        or "集动物" in action_text
        or "召集动" in action_text
    )
    return RanchObservation(
        marker_text=marker_text,
        marker_key=marker_key,
        action_text=action_text,
        feed_cost=feed_cost,
        feed_control=feed_control,
        feed_active=feed_active,
        breed_action=breed_action,
        hand_action=hand_action,
        gold_action=gold_action,
        locked=locked,
        outside=outside,
        action=action,
        summon_available=summon_available,
        summon_countdown=(summon_available and summon_number),
    )
