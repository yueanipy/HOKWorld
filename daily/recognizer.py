'每日任务一条龙 —— 识别特征(纯识别,绝不发键鼠 —— 红线:识别/动作分离)。'
from __future__ import annotations

from pathlib import Path
import re

import cv2
import numpy as np

from fishing.matcher import _get_ocr
from fishing.template_bank import crop, normalize
from daily import regions as R

MIN_CONF = 0.5   





def ocr_boxes(frame: np.ndarray, roi, *, upscale: float = 1.0) \
        -> list[tuple[str, float, float, float, float]]:
    'OCR 该 ROI → [(text, x0, y0, x1, y1), ...]，坐标为整帧归一化文字框。'
    f = normalize(frame)
    sub = crop(f, roi)
    if sub is None or sub.size == 0:
        return []
    scale = max(1.0, float(upscale))
    ocr_sub = sub
    if scale > 1.0:
        ocr_sub = cv2.resize(sub, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    try:
        res, _ = _get_ocr()(ocr_sub)
    except Exception:
        return []
    H, W = f.shape[:2]
    ox, oy = roi[0] * W, roi[1] * H
    out: list[tuple[str, float, float, float, float]] = []
    for it in (res or []):
        try:
            box, txt, score = it[0], str(it[1]).strip(), float(it[2])
        except (IndexError, ValueError, TypeError):
            continue
        if not txt or score < MIN_CONF:
            continue
        xs = [float(p[0]) / scale for p in box]
        ys = [float(p[1]) / scale for p in box]
        if not xs or not ys:
            continue
        out.append((txt, (ox + min(xs)) / W, (oy + min(ys)) / H,
                    (ox + max(xs)) / W, (oy + max(ys)) / H))
    return out


def ocr_lines(frame: np.ndarray, roi, *, upscale: float = 1.0) \
        -> list[tuple[str, float, float]]:
    'OCR 该 ROI → [(text, cxnorm, cynorm), ...];cx/cy 为文字框中心(整帧 0~1)。'
    return [(txt, (x0 + x1) * 0.5, (y0 + y1) * 0.5)
            for txt, x0, y0, x1, y1 in ocr_boxes(frame, roi, upscale=upscale)]


def ocr_text(frame: np.ndarray, roi) -> str:
    'OCR 该 ROI 的拼接文字(不含坐标)。'
    return "".join(t for t, _, _ in ocr_lines(frame, roi))


def find_word(frame: np.ndarray, roi, word: str):
    '在 ROI 内找含 word 的文字框中心 (cx, cy);没有则 None。'
    for t, cx, cy in ocr_lines(frame, roi):
        if word in t:
            return (cx, cy)
    return None


def interest_badge_popup(frame: np.ndarray) -> bool:
    '识别兴趣圈内遮挡点赞按钮的徽章升级奖励层。'
    text = ocr_text(frame, (0.24, 0.06, 0.76, 0.94))
    title = "解锁徽章" in text
    body_hits = sum(1 for word in ("等级提升", "获得奖励", "按下任意键关闭")
                    if word in text)
    return title and body_hits >= 1


def _gold_ratio(frame: np.ndarray, roi) -> float:
    'ROI 内金黄色像素占比(0~1)。'
    f = normalize(frame)
    sub = crop(f, roi)
    if sub is None or sub.size == 0:
        return 0.0
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, np.array([18, 120, 140], np.uint8), np.array([42, 255, 255], np.uint8))
    return float(cv2.countNonZero(m)) / float(m.size)





_ESC_MENU_WORDS = ("居所", "玩法", "背包", "任务", "竞技", "成就", "赛季", "百家",
                   "地图", "英雄录", "逍遥游", "山河鉴", "共鸣", "唤灵")


def in_esc_menu(frame: np.ndarray) -> bool:
    '是否在 ESC 系统菜单。'
    t = ocr_text(frame, R.ROI_ESC_GRID)
    return sum(1 for k in _ESC_MENU_WORDS if k in t) >= 2


def find_tile(frame: np.ndarray, word: str):
    'ESC 菜单磁贴:返回含 word 的磁贴文字中心;取不到给 None(相机图标无字→用 PT 定值)。'
    return find_word(frame, R.ROI_ESC_GRID, word)


def world_map_text(frame: np.ndarray) -> str:
    '读取世界地图底栏文字，供地图状态门和失败诊断共用。'
    return ocr_text(frame, (0.30, 0.82, 1.00, 1.00))


def in_world_map(frame: np.ndarray) -> bool:
    '是否在可缩放的世界地图。'
    
    text = world_map_text(frame)
    return (("切换地图" in text or "缩放地图" in text) and
            ("返回" in text or "返回所在处" in text))


WORLD_HUD_KEYS = ("抓拍", "好友", "切换", "输入", "高处")


def world_hud_hits(frame: np.ndarray) -> tuple[str, ...]:
    '返回普通角色 HUD 的稳定边缘关键词；地图/加载/公告不会具备该组合。'
    text = (ocr_text(frame, (0.70, 0.02, 1.00, 0.96))
            + ocr_text(frame, (0.00, 0.82, 0.30, 1.00)))
    return tuple(key for key in WORLD_HUD_KEYS if key in text)


def in_world_hud(frame: np.ndarray) -> bool:
    '至少两个稳定词命中才确认已经回到可操作角色 HUD。'
    return len(world_hud_hits(frame)) >= 2


def climb_key_visible(frame: np.ndarray) -> bool:
    '右下角攀爬状态的 C 键帽是否出现。'
    f = normalize(frame)
    if f is None or f.size == 0:
        return False
    h, w = f.shape[:2]
    x0, y0 = int(0.875 * w), int(0.920 * h)
    x1, y1 = int(0.930 * w), int(0.970 * h)
    sub = f[y0:y1, x0:x1]
    if sub.size == 0:
        return False

    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    bright = cv2.inRange(
        hsv, np.array([0, 0, 175], np.uint8), np.array([180, 75, 255], np.uint8))
    count, _, stats, centroids = cv2.connectedComponentsWithStats(bright)
    min_side = max(7, int(round(w * 0.004)))
    max_side = max(min_side + 1, int(round(w * 0.017)))
    for index in range(1, count):
        _, _, cw, ch, area = (int(v) for v in stats[index])
        if not (min_side <= cw <= max_side and min_side <= ch <= max_side):
            continue
        aspect = cw / max(1.0, float(ch))
        fill = area / max(1.0, float(cw * ch))
        cx = (x0 + float(centroids[index][0])) / w
        cy = (y0 + float(centroids[index][1])) / h
        if (0.887 <= cx <= 0.918 and 0.928 <= cy <= 0.960
                and 0.68 <= aspect <= 1.42 and 0.45 <= fill <= 0.97):
            return True
    return False


def center_interaction_text(frame: np.ndarray) -> str:
    '读取人物前方中央交互提示，仅覆盖烹饪/制药/使用望远镜等 F 提示区域。'
    return ocr_text(frame, R.ROI_CENTER_INTERACT)


def telescope_interaction_text(frame: np.ndarray) -> str:
    '读取持续行进时的窄幅 F 交互文字，降低望远镜出现后的 OCR 扫描范围。'
    return ocr_text(frame, R.ROI_TELESCOPE_INTERACT)


def alchemy_interaction_text(frame: np.ndarray) -> str:
    '读取制药路线交互候选区；是否为可按 F 的按钮另由键帽门确认。'
    return ocr_text(frame, R.ROI_ALCHEMY_INTERACT)


def alchemy_interaction_state(frame: np.ndarray, target_word: str = "制药",
                              *, wide: bool = False) -> dict:
    '识别真正与中央 F 键帽同一行的制药按钮。'
    roi = R.ROI_CENTER_INTERACT if wide else R.ROI_ALCHEMY_INTERACT
    boxes = ocr_boxes(frame, roi)
    candidates = []
    for text, x0, y0, x1, y1 in boxes:
        cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        if target_word in text and 0.59 <= cx <= 0.75 and 0.52 <= cy <= 0.61:
            candidates.append((text, cx, cy))
    return {
        "found": bool(candidates and interaction_f_key_visible(frame)),
        "text": "".join(text for text, *_ in boxes),
        "targets": candidates,
    }


def interaction_f_key_visible(frame: np.ndarray) -> bool:
    '中央交互项左侧白色 F 键帽是否出现。'
    f = normalize(frame)
    sub = crop(f, R.ROI_INTERACT_F_KEY)
    if sub is None or sub.size == 0:
        return False
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    bright_ratio = float(np.mean(gray >= 190))
    dark_ratio = float(np.mean(gray <= 90))
    return bright_ratio >= 0.16 and dark_ratio >= 0.25


def crafting_interaction_state(frame: np.ndarray, target_word: str) -> dict:
    '识别制作台交互项及其上方是否叠着人物对话。'
    lines = ocr_lines(frame, R.ROI_COOKING_INTERACT)
    targets = [(text, cx, cy) for text, cx, cy in lines if target_word in text]
    if not targets:
        return {"found": False, "stacked_above": False,
                "target_pt": None, "text": "".join(t for t, _, _ in lines)}

    
    _, target_x, target_y = max(targets, key=lambda item: item[2])
    stacked_above = False
    for text, cx, cy in lines:
        if target_word in text:
            continue
        chinese = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
        if (chinese >= 2 and abs(cx - target_x) <= 0.12
                and 0.02 <= target_y - cy <= 0.16):
            stacked_above = True
            break
    return {
        "found": True,
        "stacked_above": stacked_above,
        "target_pt": (target_x, target_y),
        "text": "".join(t for t, _, _ in lines),
    }


def cooking_interaction_state(frame: np.ndarray) -> dict:
    '兼容旧调用的烹饪交互状态。'
    state = crafting_interaction_state(frame, "烹饪")
    state["cooking_pt"] = state["target_pt"]
    return state






_ALCHEMY_RECIPE_SKIP = (
    "药方", "菜谱", "食谱", "烹饪", "素材不足", "恢复生命", "提升震流", "提升疾流", "治疗队友",
    "获得护盾", "世界探索", "商店", "逸事", "全部类型", "制作数量",
)
_ALCHEMY_UNLOCK_WORDS = ("世界探索", "商店", "逸事", "解锁")
_ALCHEMY_QTY_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "assets" / "daily"
_alchemy_qty_templates: dict[int, np.ndarray] | None = None


def in_crafting_page(frame: np.ndarray, title: str) -> bool:
    '制药或烹饪主界面。'
    header = ocr_text(frame, R.ROI_ALCHEMY_HEADER)
    if title and title in header:
        return True
    
    
    controls = ocr_text(frame, R.ROI_ALCHEMY_CONTROLS)
    return (("制作数量" in controls and "制作" in controls)
            or ("烹饪数量" in controls and "烹饪" in controls))


def in_alchemy_page(frame: np.ndarray) -> bool:
    '制药制作页面。'
    return in_crafting_page(frame, "制药")


def in_cooking_page(frame: np.ndarray) -> bool:
    '烹饪制作页面。'
    return in_crafting_page(frame, "烹饪")


def alchemy_selected_name(frame: np.ndarray) -> str:
    '返回右侧当前选中药品名称；去掉 OCR 偶发粘连的前导数字。'
    candidates: list[str] = []
    for text, _, cy in ocr_lines(frame, R.ROI_ALCHEMY_SELECTED):
        if cy > 0.19 or any(word in text for word in ("药品", "菜品", "料理", "已有")):
            continue
        candidates.extend(re.findall(r"[\u4e00-\u9fff]{2,}", text))
    return max(candidates, key=len) if candidates else ""


def _alchemy_row_has_lock(normalized: np.ndarray, name_y: float) -> bool:
    '药名左侧锁图标：低饱和亮白小锁；开放药方相同位置基本无亮白像素。'
    h, w = normalized.shape[:2]
    x0, x1 = int(0.058 * w), int(0.068 * w)
    y0, y1 = int((name_y - 0.018) * h), int((name_y + 0.006) * h)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return False
    hsv = cv2.cvtColor(normalized[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    bright_neutral = (hsv[:, :, 2] >= 170) & (hsv[:, :, 1] <= 85)
    return float(bright_neutral.mean()) >= 0.18


def alchemy_recipe_rows(frame: np.ndarray) -> list[dict]:
    '解析当前可见药方行。'
    lines = ocr_lines(frame, R.ROI_ALCHEMY_LIST)
    names: list[tuple[str, float, float]] = []
    for text, cx, cy in lines:
        clean = "".join(re.findall(r"[\u4e00-\u9fff]", text))
        if not (2 <= len(clean) <= 10 and 0.065 <= cx <= 0.18):
            continue
        if any(word in clean for word in _ALCHEMY_RECIPE_SKIP):
            continue
        names.append((clean, cx, cy))

    normalized = normalize(frame)
    rows: list[dict] = []
    for name, cx, cy in sorted(names, key=lambda item: item[2]):
        nearby = [text for text, _, y in lines if cy + 0.002 <= y <= cy + 0.060]
        status = "".join(nearby)
        lock_icon = _alchemy_row_has_lock(normalized, cy)
        locked = lock_icon or any(word in status for word in _ALCHEMY_UNLOCK_WORDS)
        insufficient = "素材不足" in status
        rows.append({
            "name": name,
            "point": (float(np.clip(cx, 0.08, 0.20)), float(np.clip(cy + 0.018, 0.15, 0.84))),
            "locked": locked,
            "insufficient": insufficient,
            "available": not locked and not insufficient,
            "status": status,
        })
    return rows


def alchemy_material_ratios(frame: np.ndarray) -> list[tuple[int, int]]:
    '读取右侧消耗素材的 拥有/本次数量 数字对。'
    ratios: list[tuple[int, int]] = []
    for text, _, _ in ocr_lines(frame, R.ROI_ALCHEMY_CONTROLS):
        ratios.extend((int(have), int(need))
                      for have, need in re.findall(r"(\d+)\s*/\s*(\d+)", text))
    return ratios


def alchemy_can_craft(frame: np.ndarray) -> bool:
    '右侧至少有一组素材，且每组拥有量均不少于本次消耗量。'
    ratios = alchemy_material_ratios(frame)
    return bool(ratios) and all(need > 0 and have >= need for have, need in ratios)


def alchemy_craft_button(frame: np.ndarray):
    '只返回底部单独“制作/烹饪”按钮，排除上方数量标题。'
    for text, cx, cy in ocr_lines(frame, R.ROI_ALCHEMY_CONTROLS):
        if (cy >= 0.84 and "数量" not in text
                and ("制作" in text or "烹饪" in text)):
            return (cx, cy)
    return None


def _load_alchemy_qty_templates() -> dict[int, np.ndarray]:
    global _alchemy_qty_templates
    if _alchemy_qty_templates is None:
        loaded: dict[int, np.ndarray] = {}
        for digit in range(1, 5):
            image = cv2.imread(str(_ALCHEMY_QTY_TEMPLATE_DIR / f"alchemy_qty_{digit}.png"),
                               cv2.IMREAD_GRAYSCALE)
            if image is not None:
                loaded[digit] = image
        _alchemy_qty_templates = loaded
    return _alchemy_qty_templates


def _alchemy_quantity_glyph(frame: np.ndarray) -> np.ndarray | None:
    f = normalize(frame)
    sub = crop(f, R.ROI_ALCHEMY_QUANTITY)
    if sub is None or sub.size == 0:
        return None
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 165, 255, cv2.THRESH_BINARY)
    count, _, stats, _ = cv2.connectedComponentsWithStats(binary)
    components = [i for i in range(1, count)
                  if 20 <= stats[i, cv2.CC_STAT_AREA] <= 220
                  and stats[i, cv2.CC_STAT_HEIGHT] >= 10]
    if not components:
        return None
    index = max(components, key=lambda i: stats[i, cv2.CC_STAT_AREA])
    x, y, w, h, _ = (int(v) for v in stats[index])
    raw = binary[y:y + h, x:x + w]
    canvas = np.zeros((36, 24), np.uint8)
    scale = min(30.0 / max(1, h), 18.0 / max(1, w))
    resized = cv2.resize(raw, (max(1, round(w * scale)), max(1, round(h * scale))),
                         interpolation=cv2.INTER_NEAREST)
    oy = (canvas.shape[0] - resized.shape[0]) // 2
    ox = (canvas.shape[1] - resized.shape[1]) // 2
    canvas[oy:oy + resized.shape[0], ox:ox + resized.shape[1]] = resized
    return canvas


def alchemy_quantity(frame: np.ndarray) -> int | None:
    '识别制作数量黑框内的 1~4；模板来自录像真实 UI，归一化后跨分辨率匹配。'
    glyph = _alchemy_quantity_glyph(frame)
    templates = _load_alchemy_qty_templates()
    if glyph is None or not templates:
        return None
    scores = {digit: float((glyph != template).mean())
              for digit, template in templates.items() if template.shape == glyph.shape}
    if not scores:
        return None
    digit = min(scores, key=scores.get)
    return digit if scores[digit] <= 0.12 else None


def alchemy_complete_overlay(frame: np.ndarray) -> bool:
    '制作结果浮层；底部“点击空白处关闭”比金色标题更稳定。'
    text = ocr_text(frame, R.ROI_ALCHEMY_COMPLETE)
    return ("点击空白处关闭" in text
            or ("制作完成" in text and "返回" in text)
            or ("烹饪完成" in text and "返回" in text))


def scene_motion_score(previous: np.ndarray, current: np.ndarray) -> float:
    '两帧场景光流强度，仅用于判断短步是否产生了画面位移。'
    if previous is None or current is None:
        return 0.0
    a = normalize(previous)
    b = normalize(current)
    if a.shape[:2] != b.shape[:2]:
        return 0.0
    h, w = a.shape[:2]
    x0, x1 = int(0.18 * w), int(0.82 * w)
    y0, y1 = int(0.20 * h), int(0.78 * h)
    ga = cv2.cvtColor(a[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(b[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    if ga.size == 0 or gb.size == 0:
        return 0.0
    ga = cv2.resize(ga, (320, 180), interpolation=cv2.INTER_AREA)
    gb = cv2.resize(gb, (320, 180), interpolation=cv2.INTER_AREA)
    flow = cv2.calcOpticalFlowFarneback(ga, gb, None, 0.5, 3, 15, 3, 5, 1.2, 0)
    magnitude = np.hypot(flow[..., 0], flow[..., 1])
    return float(np.median(magnitude))


def find_map_teleport_icons(frame: np.ndarray, anchor,
                            max_distance: float = 0.42,
                            min_distance: float = 0.015,
                            min_width: int = 8,
                            min_height: int = 6,
                            min_original_pixels: int = 35) -> list[tuple[float, float]]:
    '动态查找地图上的青色传送图标，返回距 anchor 由近到远的归一化点击点。'
    if frame is None or anchor is None:
        return []
    f = normalize(frame)
    hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
    h, w = f.shape[:2]
    mask = cv2.inRange(hsv, np.array([82, 100, 150], np.uint8),
                       np.array([100, 255, 255], np.uint8))
    
    mask[:int(0.10 * h), :] = 0
    mask[int(0.88 * h):, :] = 0
    mask[:, :int(0.04 * w)] = 0
    mask[:, int(0.96 * w):] = 0
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 9))
    merged = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    count, _, stats, centers = cv2.connectedComponentsWithStats(merged)
    ax, ay = float(anchor[0]), float(anchor[1])
    found: list[tuple[float, float, float]] = []
    for i in range(1, count):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        
        if not (max(1, int(min_width)) <= cw <= 65 and
                max(1, int(min_height)) <= ch <= 55 and area >= 45):
            continue
        original_pixels = cv2.countNonZero(mask[y:y + ch, x:x + cw])
        if original_pixels < max(1, int(min_original_pixels)):
            continue
        cx = float(centers[i][0] / w)
        
        cy = float(np.clip((y + ch + 4) / h, 0.05, 0.95))
        distance = float(np.hypot(cx - ax, cy - ay))
        if max(0.0, float(min_distance)) <= distance <= max_distance:
            found.append((distance, cx, cy))
    found.sort(key=lambda item: item[0])
    return [(cx, cy) for _, cx, cy in found]


def teleport_dialog(frame: np.ndarray):
    '传送/确认「提示」弹窗 → (indialog, bodytext, confirmpt)。'
    title = ocr_text(frame, R.ROI_DIALOG_TITLE)
    body = ocr_text(frame, R.ROI_DIALOG_BODY)
    in_dialog = ("提示" in title) or ("是否" in body) or ("传送" in body)
    if not in_dialog:
        return (False, "", None)
    cp = find_word(frame, R.ROI_DIALOG_BTNS, "确定") or R.PT_DIALOG_CONFIRM
    return (True, body, cp)


def read_toast(frame: np.ndarray) -> str:
    '顶部 Toast 归一化文字(动作完成信号)。'
    return ocr_text(frame, R.ROI_TOAST)





def in_residence(frame: np.ndarray) -> bool:
    '是否在「我的居所」页(左上标题)。'
    return "居所" in ocr_text(frame, R.ROI_RESIDENCE_HEADER)


def homeland_loaded(frame: np.ndarray) -> bool:
    '游戏内家园已加载:左上小地图旁出现「××的居所」标题(20260712 用户拍板的传送完成判据。'
    return "居所" in ocr_text(frame, R.ROI_HOMELAND_HEADER)


def residence_active_tab(frame: np.ndarray) -> str | None:
    '当前高亮页签(总览/管理/留言/访客)——OCR 无法直接读高亮,返回读到的页签集合首个匹配。'
    t = ocr_text(frame, R.ROI_RESIDENCE_TABS)
    for tab in ("管理", "总览", "留言", "访客"):
        if tab in t:
            return tab
    return None


_MANAGE_NODES = ("农贸作物", "培养箱", "派遣小屋", "牧场", "蔬菜摊")


def in_manage_map(frame: np.ndarray) -> bool:
    '是否在管理页(插画地图)。'
    t = ocr_text(frame, R.ROI_MANAGE_MAP)
    if ("已种植" in t) or ("区域" in t):
        return True
    return sum(1 for k in _MANAGE_NODES if k in t) >= 3


def read_manage_nodes(frame: np.ndarray) -> dict:
    '管理地图各节点状态(先看地图就能决定是否进场,省得空跑):。'
    def _planted(roi):
        t = ocr_text(frame, roi)
        import re
        m = re.search(r"已种植[:：]?\s*(\d+)\s*/\s*(\d+)", t)
        return (int(m.group(1)), int(m.group(2))) if m else None

    dispatch_txt = ocr_text(frame, R.ROI_NODE_DISPATCH)
    import re
    dm = re.search(r"(\d+)", dispatch_txt)
    return {
        "farm": {"planted": _planted(R.ROI_NODE_FARM), "pt": R.PT_NODE_FARM},
        "incubator": {"planted": _planted(R.ROI_NODE_INCUBATOR), "pt": R.PT_NODE_INCUBATOR},
        "dispatch": {
            "has_done": bool(find_word(frame, R.ROI_DISPATCH_DONE_BUBBLE, "完成")
                             or find_word(frame, R.ROI_DISPATCH_DONE_BUBBLE, "派遣")),
            "count": int(dm.group(1)) if dm else None,
            "pt": R.PT_NODE_DISPATCH,
        },
    }





_FIELD_WORDS = ("可收获", "收获翻倍", "小时")
_TIMER_RE = None


def field_targets(frame: np.ndarray) -> list[tuple[float, float]]:
    '屏幕上田地浮标的位置列表 [(cx,cy)](同类脚本 walkto 式视觉伺服的目标):。'
    global _TIMER_RE
    if _TIMER_RE is None:
        import re
        _TIMER_RE = re.compile(r"\d{1,2}:\d{2}(:\d{2})?|\d+\s*小时")
    out = []
    for t, cx, cy in ocr_lines(frame, (0.05, 0.12, 0.95, 0.80)):
        if any(k in t for k in _FIELD_WORDS) or _TIMER_RE.search(t):
            out.append((cx, cy))
    return out


def field_harvestable(frame: np.ndarray) -> bool:
    '田地里是否有「可收获」浮标(有=还能收割)。'
    return "可收获" in ocr_text(frame, R.ROI_FIELD)


def field_growing(frame: np.ndarray) -> bool:
    '是否有生长倒计时浮标(如 16小时 / 15:59:56)——已种植、未成熟。'
    t = ocr_text(frame, R.ROI_FIELD)
    import re
    return bool(re.search(r"\d+\s*小时", t) or re.search(r"\d{1,2}:\d{2}", t))


def bottom_actions_text(frame: np.ndarray) -> str:
    '右下四图标行的文字标签(求助浇水/1行/更换种子)。'
    return ocr_text(frame, R.ROI_BOTTOM_ACTIONS)


def plant_action_text(frame: np.ndarray) -> str:
    '右下第 3 格标签；种植态为「更换种子」，种完进入浇水态后变为「铲除」。'
    return ocr_text(frame, R.ROI_PLANT_LABEL)


def plant_action_available(frame: np.ndarray) -> bool:
    '是否处于可种植动作组；只要求命中「更换」，兼容 OCR 将「种子」误读成其它字。'
    return "更换" in plant_action_text(frame)


def field_waterable(frame: np.ndarray):
    '田块上方是否有 ⛲ 浇水壶气泡(缺水可浇)。'
    return None   



_ERODE_K = np.ones((9, 9), np.uint8)   

_BLUE_LO = np.array([88, 90, 150], np.uint8)
_BLUE_HI = np.array([112, 255, 255], np.uint8)


def _thin(mask: np.ndarray) -> np.ndarray:
    '细线掩膜:掩膜 − (腐蚀→膨胀外扩的大色块)。'
    blob = cv2.erode(mask, _ERODE_K)                       
    blob = cv2.dilate(blob, _ERODE_K, iterations=2)        
    return cv2.bitwise_and(mask, cv2.bitwise_not(blob))


def _thin_ratio(mask: np.ndarray) -> float:
    '掩膜中细线像素占比(与角色站位无关)。'
    if mask is None or mask.size == 0:
        return 0.0
    return float(cv2.countNonZero(_thin(mask))) / float(mask.size)





_MIN_FRAME_LINE = 150



_MIN_FRAME_LINE_BLUE = 100



_MIN_FRAME_LINE_HINT = 80


def _has_frame_line(thin: np.ndarray, min_len: int = _MIN_FRAME_LINE) -> bool:
    '细线掩膜里是否存在足够长的直线段(霓虹框的本质特征;散点噪声过不了)。'
    lines = cv2.HoughLinesP(thin, 1, np.pi / 180, threshold=40,
                            minLineLength=min_len, maxLineGap=6)
    return lines is not None


def _frame_thin_masks(hsv: np.ndarray):
    '脚下 ROI 的 HSV → (蓝/红/白)三色细线掩膜。'
    t_blue = _thin(cv2.inRange(hsv, _BLUE_LO, _BLUE_HI))
    t_red = _thin(cv2.bitwise_or(
        cv2.inRange(hsv, np.array([0, 110, 120], np.uint8), np.array([8, 255, 255], np.uint8)),
        cv2.inRange(hsv, np.array([172, 110, 120], np.uint8), np.array([180, 255, 255], np.uint8))))
    t_white = _thin(cv2.inRange(hsv, np.array([0, 0, 225], np.uint8), np.array([180, 45, 255], np.uint8)))
    return t_blue, t_red, t_white


def _ratio(t: np.ndarray) -> float:
    return float(cv2.countNonZero(t)) / float(t.size) if t is not None and t.size else 0.0


_WATER_TPL = None            
_WATER_TPL_SCALES = (1.0, 0.9, 1.1, 0.8, 1.2)
WATER_ACTION_TH = 0.70       
WATER_FORBIDDEN_TH = 0.75    
ACTION_ICON_CHANGE_TH = 0.17 


_GOLD_LO = np.array([18, 120, 150], np.uint8)   
_GOLD_HI = np.array([38, 255, 255], np.uint8)
ACTION_GOLD_MIN = 600        
                             


def action_gold_px(frame: np.ndarray) -> int:
    '右下按钮 ROI 内金色发光像素数(激活=有金环,灰暗=无)。'
    f = normalize(frame)
    sub = crop(f, R.ROI_ACTION_GOLD)
    if sub is None or sub.size == 0:
        return 0
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    return int(cv2.countNonZero(cv2.inRange(hsv, _GOLD_LO, _GOLD_HI)))




_RING_C = (0.935, 0.915)      
_RING_R = (25, 60)            
ACTION_RING_MIN = 500         
_RING_MASK = {}               


def action_ring_gold_px(frame: np.ndarray) -> int:
    '环形区域内的金色像素数(只认按钮金环那一圈,场景透光/图标本体金色都不算)。'
    f = normalize(frame)
    H, W = f.shape[:2]
    cx, cy, r_out = int(_RING_C[0] * W), int(_RING_C[1] * H), _RING_R[1]
    x0, y0 = max(0, cx - r_out), max(0, cy - r_out)
    x1, y1 = min(W, cx + r_out), min(H, cy + r_out)
    sub = f[y0:y1, x0:x1]                    
    if sub.size == 0:
        return 0
    key = (sub.shape[0], sub.shape[1], cx - x0, cy - y0)
    mask = _RING_MASK.get(key)
    if mask is None:
        yy, xx = np.mgrid[0:sub.shape[0], 0:sub.shape[1]]
        r2 = (xx - (cx - x0)) ** 2 + (yy - (cy - y0)) ** 2
        mask = ((r2 >= _RING_R[0] ** 2) & (r2 <= _RING_R[1] ** 2)).astype(np.uint8) * 255
        _RING_MASK[key] = mask
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, _GOLD_LO, _GOLD_HI)
    return int(cv2.countNonZero(cv2.bitwise_and(gold, mask)))


def action_roi_gray(frame: np.ndarray):
    '右下动作按钮 ROI 的灰度图(供调用方跨帧算时间方差判"闪烁=可操作 vs 灰暗=不可操作")。'
    f = normalize(frame)
    sub = crop(f, R.ROI_ACTION_GOLD)
    if sub is None or sub.size == 0:
        return None
    return cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)


def action_icon_signature(frame: np.ndarray):
    '动作图标主体的白色掩膜；ROI 避开闪烁外圈，不受金环明暗相位影响。'
    f = normalize(frame)
    sub = crop(f, R.ROI_ACTION_SYMBOL)
    if sub is None or sub.size == 0:
        return None
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    return cv2.inRange(
        hsv,
        np.array([0, 0, 175], np.uint8),
        np.array([180, 105, 255], np.uint8),
    )


def action_icon_change_score(before_signature, after_frame: np.ndarray) -> float:
    '点击前后动作图标主体的变化率；只比较白色形状，不比较闪烁金环。'
    if before_signature is None or after_frame is None:
        return 0.0
    after_signature = action_icon_signature(after_frame)
    if after_signature is None:
        return 0.0
    if before_signature.shape != after_signature.shape:
        before_signature = cv2.resize(
            before_signature,
            (after_signature.shape[1], after_signature.shape[0]),
            interpolation=cv2.INTER_NEAREST,
        )
    changed = cv2.bitwise_xor(before_signature, after_signature)
    return float(cv2.countNonZero(changed)) / float(changed.size)


_HARVEST_TPL = None          
_WATER_FORBIDDEN_TPL = None  
HARVEST_ACTION_TH = 0.62     
                             
                             
_ACTION_SCALES = (1.0, 0.9, 1.1, 0.8, 1.2, 0.7, 1.3, 1.4)   


def _load_action_tpl_bgr(fname: str):
    '加载动作按钮模板(BGR;读不到返回 False,只报一次)。'
    from pathlib import Path
    img = cv2.imread(str(Path(__file__).resolve().parent / "templates" / fname))
    return img if img is not None else False


def _white_mask(bgr: np.ndarray) -> np.ndarray:
    'UI 纯白图标掩膜(HSV 低饱和高亮度)。'
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, np.array([0, 0, 190], np.uint8), np.array([180, 80, 255], np.uint8))


def _tpl_score_multi(sub_bgr: np.ndarray, tpl_bgr) -> float:
    '按钮 ROI 内多尺度模板匹配,双特征取最高:①灰度 ②白色图标掩膜(形状匹配,环境无关)。'
    if tpl_bgr is False or tpl_bgr is None or sub_bgr is None or sub_bgr.size == 0:
        return 0.0
    pairs = (
        (cv2.cvtColor(sub_bgr, cv2.COLOR_BGR2GRAY), cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)),
        (_white_mask(sub_bgr), _white_mask(tpl_bgr)),
    )
    best = 0.0
    for sub, tpl in pairs:
        for s in _ACTION_SCALES:
            th, tw = int(tpl.shape[0] * s), int(tpl.shape[1] * s)
            if th < 10 or tw < 10 or th > sub.shape[0] or tw > sub.shape[1]:
                continue
            r = cv2.matchTemplate(sub, cv2.resize(tpl, (tw, th)), cv2.TM_CCOEFF_NORMED)
            best = max(best, float(r.max()))
    return best


def _action_roi_bgr(frame: np.ndarray):
    f = normalize(frame)
    return crop(f, R.ROI_ACTION_GOLD)


def water_action_score(frame: np.ndarray) -> float:
    '浇水壶匹配分(双特征取高,只扫按钮小 ROI)。'
    global _WATER_TPL
    if _WATER_TPL is None:
        _WATER_TPL = _load_action_tpl_bgr("water_action.png")
    return _tpl_score_multi(_action_roi_bgr(frame), _WATER_TPL)


def water_forbidden_action_score(frame: np.ndarray) -> float:
    '右下角“禁止浇水”圆圈斜杠分数；Canny 形状匹配抗场景明暗和背景变化。'
    global _WATER_FORBIDDEN_TPL
    if _WATER_FORBIDDEN_TPL is None:
        _WATER_FORBIDDEN_TPL = _load_action_tpl_bgr("water_forbidden_action.png")
    sub = _action_roi_bgr(frame)
    tpl = _WATER_FORBIDDEN_TPL
    if tpl is False or tpl is None or sub is None or sub.size == 0:
        return 0.0
    a = cv2.Canny(cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY), 80, 180)
    b = cv2.Canny(cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY), 80, 180)
    best = 0.0
    for s in _ACTION_SCALES:
        th, tw = int(b.shape[0] * s), int(b.shape[1] * s)
        if th < 10 or tw < 10 or th > a.shape[0] or tw > a.shape[1]:
            continue
        result = cv2.matchTemplate(a, cv2.resize(b, (tw, th)), cv2.TM_CCOEFF_NORMED)
        best = max(best, float(result.max()))
    return best


def water_forbidden_action(frame: np.ndarray) -> bool:
    return water_forbidden_action_score(frame) >= WATER_FORBIDDEN_TH


def harvest_action_score(frame: np.ndarray) -> float:
    '镰刀匹配分(双特征取高,只扫按钮小 ROI)。'
    global _HARVEST_TPL
    if _HARVEST_TPL is None:
        _HARVEST_TPL = _load_action_tpl_bgr("harvest_action.png")
    return _tpl_score_multi(_action_roi_bgr(frame), _HARVEST_TPL)


def water_action_available(frame: np.ndarray) -> bool:
    '右下按钮是否浇水壶(=需浇)。'
    return water_action_score(frame) >= WATER_ACTION_TH


def harvest_action_available(frame: np.ndarray) -> bool:
    '右下按钮是否镰刀(=可收割)。'
    return harvest_action_score(frame) >= HARVEST_ACTION_TH


def plot_frame_present(frame: np.ndarray) -> bool:
    '脚下是否有(哪怕不完整的)霓虹方框——碎步走位专用的敏感判据,只答"有/无"不分色。'
    f = normalize(frame)
    sub = crop(f, R.ROI_PLOT_FRAME)
    if sub is None or sub.size == 0:
        return False
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    t_blue, t_red, t_white = _frame_thin_masks(hsv)
    H = _MIN_FRAME_LINE_HINT
    return ((_ratio(t_blue) > 0.005 and _has_frame_line(t_blue, H))
            or (_ratio(t_red) > 0.0025 and _has_frame_line(t_red, H))
            or (_ratio(t_white) > 0.008 and _has_frame_line(t_white, H)))


def plot_frame_state(frame: np.ndarray, roi=None):
    "脚下/身前田块霓虹方框颜色 → 'blue'(可交互:可浇/可种)/'white'(已浇)/'red'(不可)/None(无框)。"
    f = normalize(frame)
    sub = crop(f, roi or R.ROI_PLOT_FRAME)
    if sub is None or sub.size == 0:
        return None
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    t_blue, t_red, t_white = _frame_thin_masks(hsv)   
    
    
    
    
    if _ratio(t_blue) > 0.008 and _has_frame_line(t_blue, _MIN_FRAME_LINE_BLUE):
        return "blue"
    if _ratio(t_red) > 0.004 and _has_frame_line(t_red):
        return "red"
    if _ratio(t_white) > 0.012 and _has_frame_line(t_white):
        return "white"
    return None


def plot_frame_point(frame: np.ndarray):
    '可浇/可种蓝框田块的点击点(归一化全帧坐标)。'
    f = normalize(frame)
    sub = crop(f, R.ROI_PLOT_FRAME)
    if sub is None or sub.size == 0:
        return None
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    thin = _thin(cv2.inRange(hsv, _BLUE_LO, _BLUE_HI))
    if float(cv2.countNonZero(thin)) / float(thin.size) <= 0.008:   
        return None
    if not _has_frame_line(thin):
        return None                          
    link = cv2.dilate(thin, np.ones((15, 15), np.uint8))      
    n, _labels, stats, _cents = cv2.connectedComponentsWithStats(link)
    if n < 2:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    bx, by, bw, bh = (int(stats[k, c]) for c in
                      (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
    dist = cv2.distanceTransform(cv2.bitwise_not(link), cv2.DIST_L2, 3)
    box = dist[by:by + bh, bx:bx + bw]
    iy, ix = np.unravel_index(int(np.argmax(box)), box.shape)
    if box[iy, ix] <= 20:                     
        return None
    
    
    
    px, py = bx + ix, by + iy
    if px in (0, sub.shape[1] - 1) or py in (0, sub.shape[0] - 1):
        return None                           
                                              
    hits = sum((int(link[:py, px].any()), int(link[py + 1:, px].any()),
                int(link[py, :px].any()), int(link[py, px + 1:].any())))
    if hits < 2:
        return None
    x0, y0, x1, y1 = R.ROI_PLOT_FRAME
    return (x0 + (float(px) / sub.shape[1]) * (x1 - x0),
            y0 + (float(py) / sub.shape[0]) * (y1 - y0))


def water_bubbles(frame: np.ndarray):
    '植株上方的水壶气泡(需浇水标记,billboard 圆图标)→ [(nx,ny), ...]。'
    f = normalize(frame)
    H, W = f.shape[:2]
    y0 = int(0.12 * H)
    sub = f[y0:int(0.72 * H)]
    g = cv2.medianBlur(cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY), 3)
    cir = cv2.HoughCircles(g, cv2.HOUGH_GRADIENT, dp=1.2, minDist=30,
                           param1=90, param2=28, minRadius=15, maxRadius=27)
    hits = []
    if cir is None:
        return hits
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    v, s = hsv[..., 2], hsv[..., 1]
    for x, y, r in cir[0]:
        x, y, r = int(x), int(y), int(r)
        if not (r < x < sub.shape[1] - r and r < y < sub.shape[0] - r):
            continue
        m = np.zeros(g.shape, np.uint8)
        cv2.circle(m, (x, y), int(r * 0.72), 255, -1)
        n = float(cv2.countNonZero(m))
        dark = cv2.countNonZero(cv2.bitwise_and((v < 95).astype(np.uint8) * 255, m)) / n
        glyph = cv2.countNonZero(cv2.bitwise_and(((v > 140) & (s < 110)).astype(np.uint8) * 255, m)) / n
        if dark > 0.45 and 0.06 < glyph < 0.45:
            hits.append((x / W, (y + y0) / H))
    return hits


def ahead_is_soil(frame: np.ndarray):
    '⚠ 已证伪,勿作行尾判定(留作观察):作物是绿的(泥土行被判"草地")、木板是棕的(行尾被判。'
    f = normalize(frame)
    sub = crop(f, R.ROI_AHEAD_GROUND)
    if sub is None or sub.size == 0:
        return None
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    n = float(sub.shape[0] * sub.shape[1])
    soil = cv2.countNonZero(cv2.inRange(hsv, np.array([5, 40, 30], np.uint8), np.array([28, 200, 190], np.uint8))) / n
    grass = cv2.countNonZero(cv2.inRange(hsv, np.array([35, 60, 40], np.uint8), np.array([85, 255, 220], np.uint8))) / n
    if soil < 0.10 and grass < 0.10:
        return None
    return soil >= grass


_BOARD_TPL = None            
_BOARD_SCALES = (1.2, 1.0, 0.85, 0.72)
BOARD_TH = 0.80              
ROI_BOARD = (0.28, 0.22, 0.72, 0.62)   


def board_ahead(frame: np.ndarray) -> bool:
    '前方是否出现公告牌(近大,第一列对齐时)= 最后一行信号(f001079)。'
    global _BOARD_TPL
    if _BOARD_TPL is None:
        from pathlib import Path
        p = Path(__file__).resolve().parent / "templates" / "board.png"
        img = cv2.imread(str(p))
        _BOARD_TPL = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img is not None else False
    if _BOARD_TPL is False or _BOARD_TPL is None:
        return False
    f = normalize(frame)
    sub = crop(f, ROI_BOARD)
    if sub is None or sub.size == 0:
        return False
    g = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    best = 0.0
    for s in _BOARD_SCALES:
        th, tw = int(_BOARD_TPL.shape[0] * s), int(_BOARD_TPL.shape[1] * s)
        if th < 16 or tw < 16 or th > g.shape[0] or tw > g.shape[1]:
            continue
        r = cv2.matchTemplate(g, cv2.resize(_BOARD_TPL, (tw, th)), cv2.TM_CCOEFF_NORMED)
        best = max(best, float(r.max()))
    return best >= BOARD_TH






_FLOW_L = (0.10, 0.72, 0.34, 0.96)     
_FLOW_R = (0.66, 0.72, 0.90, 0.96)     


def ground_shift_dy(prev_bgr: np.ndarray, cur_bgr: np.ndarray) -> float:
    '两帧间地面竖直位移(1920 归一化像素,正=角色前进/地面下滚)。'
    a = normalize(prev_bgr)
    b = normalize(cur_bgr)
    H, W = a.shape[:2]
    dvs = []
    for (x0, y0, x1, y1) in (_FLOW_L, _FLOW_R):
        pa = cv2.cvtColor(a[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)], cv2.COLOR_BGR2GRAY)
        pb = cv2.cvtColor(b[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)], cv2.COLOR_BGR2GRAY)
        if pa.shape != pb.shape or pa.size == 0:
            continue
        flow = cv2.calcOpticalFlowFarneback(pa, pb, None, 0.5, 3, 25, 3, 5, 1.2, 0)
        dvs.append(float(np.median(flow[..., 1])))
    if not dvs:
        return 0.0
    return float(np.median(dvs))


def action_mode_text(frame: np.ndarray) -> str:
    '右下操作范围标签文字(「1格」/「4格」/「1行」,G 键循环切换)。'
    return ocr_text(frame, R.ROI_MODE_LABEL)


def proficiency_overlay(frame: np.ndarray) -> bool:
    '屏幕中央是否弹出「熟练度提升」全屏浮层(收割/种植后随机出现,「点击空白处关闭」)。'
    t = ocr_text(frame, R.ROI_PROF_TEXT)
    return "熟练度" in t or "售价提升" in t


def seed_panel_open(frame: np.ndarray) -> bool:
    '种子选择面板是否打开(右侧面板,底部有「选择」按钮)。'
    return "选择" in ocr_text(frame, R.ROI_SEED_SELECT_BTN)


def seed_names(frame: np.ndarray) -> list[tuple[str, float, float]]:
    '当前类别页的种子名列表 [(名, cx, cy)](名称在图标下方;文本识别选种,用户明确不要模板)。'
    import re
    out = []
    for t, cx, cy in ocr_lines(frame, R.ROI_SEED_GRID):
        if len(t) < 2 or "限时" in t:
            continue
        if re.fullmatch(r"[\d:\.]+", t) or re.search(r"\d+\s*(小时|分钟|时|分)", t):
            continue
        out.append((t, cx, cy))
    return out


def find_seed(frame: np.ndarray, name: str):
    '在当前类别页找名字含 name 的种子中心;没有 None(任务模块可换 tab 再找)。'
    for t, cx, cy in seed_names(frame):
        if name in t:
            return (cx, cy)
    return None


def seedling_insufficient(frame: np.ndarray) -> bool:
    '「幼苗不足,请获取幼苗后再种植」提示弹窗(f000675)。'
    return "幼苗不足" in ocr_text(frame, R.ROI_DIALOG_BODY)


def farm_high_value_warning(frame: np.ndarray):
    '农贸作物高售价二次确认 → (命中, 不再提示圆圈, 确定按钮)。'
    lines = ocr_lines(frame, R.ROI_DIALOG_BODY)
    body = "".join(text for text, _, _ in lines)
    high_value = any(word in body for word in (
        "高售价作物", "无法获得经验", "每周限次数", "种植数量",
    ))
    if "今日不再提示" not in body or not high_value:
        return False, None, None

    no_remind_pt = R.PT_FARM_DONT_REMIND
    for text, cx, cy in lines:
        if "不再提示" in text and len(text) <= 8:
            
            
            no_remind_pt = (max(0.0, cx - 0.047), cy)
            break
    confirm_pt = find_word(frame, R.ROI_DIALOG_BTNS, "确定") or R.PT_DIALOG_CONFIRM
    return True, no_remind_pt, confirm_pt





def in_dispatch_page(frame: np.ndarray) -> bool:
    '收紧:要求标题完整「探险派遣」(仅"派遣"会被世界里"派遣小屋"提示误判)。'
    return "探险派遣" in ocr_text(frame, R.ROI_DISPATCH_TITLE)


def read_dispatch_slots(frame: np.ndarray) -> list[str]:
    "三槽位状态列表(自上而下),每项 ∈ {'done'(完成派遣,可领)/'idle'(空闲中,可派)/'busy'(倒计时)/'none'}。"
    out = []
    for roi in R.ROI_SLOT:
        t = ocr_text(frame, roi)
        if "完成" in t:
            out.append("done")
        elif "空闲" in t:
            out.append("idle")
        elif any(c.isdigit() for c in t) and ":" in t:
            out.append("busy")
        else:
            out.append("none")
    return out


def read_dispatch_button(frame: np.ndarray):
    '右下三态大按钮 → (state, pt)。'
    lines = ocr_lines(frame, R.ROI_DISPATCH_BTN)
    t = "".join(x[0] for x in lines)
    def _pt(kw):
        return next(((cx, cy) for txt, cx, cy in lines if kw in txt), R.PT_DISPATCH_BTN)
    if "领取" in t:
        return ("claim", _pt("领取"))
    if "选择成员" in t or "成员" in t:
        return ("member", _pt("成员"))
    if "派遣出发" in t or "出发" in t:
        return ("go", _pt("出发"))
    if "召回" in t:
        return ("recall", _pt("召回"))
    return ("none", R.PT_DISPATCH_BTN)


def member_drawer_open(frame: np.ndarray) -> bool:
    '选择成员抽屉是否打开(标题「选择成员」)。'
    return "选择成员" in ocr_text(frame, R.ROI_MEMBER_TITLE) or "成员" in ocr_text(frame, R.ROI_MEMBER_TITLE)



_PET_RING_P80_MIN = 110       
_PET_CHECK_CENTER_MIN = 0.12  


def pet_row_circle(frame: np.ndarray, row_idx: int) -> str:
    "选择成员抽屉第 rowidx 行右端圆圈状态 ∈ {'empty'(空心=可选)/'checked'(打钩=已选)/'none'(无圈)}。"
    if not (0 <= row_idx < len(R.PET_ROW_Y)):
        return "none"
    f = normalize(frame)
    H, W = f.shape[:2]
    cx, cy = int(R.PT_PET_CIRCLE_X * W), int(R.PET_ROW_Y[row_idx] * H)
    r_out = int(round(32.0 / 1920 * W))
    x0, y0 = max(0, cx - r_out), max(0, cy - r_out)
    x1, y1 = min(W, cx + r_out), min(H, cy + r_out)
    box = f[y0:y1, x0:x1]
    if box.size == 0:
        return "none"
    hsv = cv2.cvtColor(box, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2].astype(np.int32)
    s = hsv[:, :, 1].astype(np.int32)
    yy, xx = np.mgrid[0:box.shape[0], 0:box.shape[1]]
    d = np.sqrt((yy - (cy - y0)) ** 2 + (xx - (cx - x0)) ** 2) / W * 1920.0  
    ring = (d >= 17) & (d <= 25)
    center = d <= 12
    if not ring.any() or not center.any():
        return "none"
    if float(np.percentile(v[ring], 80)) < _PET_RING_P80_MIN:
        return "none"
    center_bright = float(((v >= 170) & (s <= 90))[center].mean())
    return "checked" if center_bright >= _PET_CHECK_CENTER_MIN else "empty"


def reward_overlay(frame: np.ndarray) -> bool:
    '领取奖励后的全屏「获得道具」浮层(f001192):底部「点击空白处继续」或大字「获得道具/领取奖励」。'
    hint = ocr_text(frame, R.ROI_REWARD_HINT)
    if "空白" in hint or "继续" in hint:
        return True
    title = ocr_text(frame, R.ROI_REWARD_TITLE)
    return ("获得道具" in title) or ("领取奖励" in title) or ("获得奖励" in title)


def dispatch_confirm_dialog(frame: np.ndarray):
    '1~2 只宠物派遣时弹「提示…当前队伍中尚有空位,是否确认派遣呢?」(f001464)→ (indialog, confirmpt)。'
    title = ocr_text(frame, R.ROI_DIALOG_TITLE)
    body = ocr_text(frame, R.ROI_DIALOG_BODY)
    if ("提示" not in title) and ("确认派遣" not in body) and ("是否" not in body):
        return (False, None)
    pt = find_word(frame, R.ROI_DIALOG_BTNS, "确定") or R.PT_DIALOG_CONFIRM
    return (True, pt)





def in_camera(frame: np.ndarray) -> bool:
    return "相机" in ocr_text(frame, R.ROI_CAMERA_TITLE)


def camera_f_ready(frame: np.ndarray) -> bool:
    '相机模式右下出现「F」快门提示(可拍)。'
    return "F" in ocr_text(frame, R.ROI_CAMERA_F_HINT)


def in_share_page(frame: np.ndarray) -> bool:
    '快门后自动进入「分享」页(拍照已成)。'
    t = ocr_text(frame, R.ROI_SHARE_TITLE)
    return "分享" in t





def in_playbook_hub(frame: np.ndarray) -> bool:
    '玩法手册六块入口页,不是朝闻道详情页。'
    header = ocr_text(frame, R.ROI_PLAYBOOK_HUB_HEADER)
    card = ocr_text(frame, R.ROI_PLAYBOOK_HUB_DAILY)
    return "玩法手册" in header and "朝闻道" in card and not in_playbook(frame)


def playbook_daily_done(frame: np.ndarray) -> bool:
    '入口页朝闻道卡片显示「今日已完成」时严禁继续点击。'
    return "今日已完成" in ocr_text(frame, R.ROI_PLAYBOOK_HUB_DONE)


def find_playbook_daily_entry(frame: np.ndarray):
    '只在入口页朝闻道卡片 ROI 内找标题,不误点日行修炼或上周宝箱。'
    if not in_playbook_hub(frame):
        return None
    return find_word(frame, R.ROI_PLAYBOOK_HUB_DAILY, "朝闻道") or R.PT_PLAYBOOK_HUB_DAILY


def in_playbook(frame: np.ndarray) -> bool:
    return "朝闻道" in ocr_text(frame, R.ROI_PLAYBOOK_TITLE) or "朝闻道" in ocr_text(frame, R.ROI_PLAYBOOK_TOPTABS)


def find_subtab(frame: np.ndarray, word: str):
    '朝闻道底部子页签(推荐/尘战/消遣/探索/会友)中含 word 的中心。'
    return find_word(frame, R.ROI_PLAYBOOK_SUBTABS, word)


def read_daily_cards(frame: np.ndarray) -> list[dict]:
    "任务卡片行 → [{'text':描述, 'claimable':是否金色「领取」, 'done':是否已完成, 'pt':点击点}]。"
    lines = ocr_lines(frame, R.ROI_TASK_CARDS)
    cards = []
    for txt, cx, cy in lines:
        cards.append({
            "text": txt,
            "claimable": "领取" in txt,
            "done": "已完成" in txt,
            "pt": (cx, cy),
        })
    return cards


def find_claimable_daily_card(frame: np.ndarray):
    '推荐页五张卡中第一个金色「领取」按钮；小块 HSV 扫描比整行 OCR 更快。'
    f = normalize(frame)
    h, w = f.shape[:2]
    for cx in R.TASK_CARD_CLAIM_XS:
        x0, x1 = int((cx - 0.035) * w), int((cx + 0.035) * w)
        y0, y1 = int(0.845 * h), int(0.895 * h)
        sub = f[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
        if sub.size == 0:
            continue
        hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
        gold = cv2.inRange(hsv, np.array([15, 60, 130]), np.array([48, 255, 255]))
        if float(np.count_nonzero(gold)) / gold.size >= 0.03:
            return (cx, R.PT_TASK_CARD_CLAIM_Y)
    return None


def read_daily_activity(frame: np.ndarray) -> int | None:
    '读取每日活跃度当前值,如 120。'
    import re
    for text, _, _ in ocr_lines(frame, R.ROI_ACTIVITY_VALUE):
        m = re.search(r"\d+", text)
        if m:
            try:
                return int(m.group())
            except ValueError:
                pass
    return None


def _activity_reward_gold_score(frame: np.ndarray, cx: float) -> float:
    '奖励格边框金色占比；只取四周,避免未解锁格内金币/经验图标造成假阳。'
    f = normalize(frame)
    h, w = f.shape[:2]
    x0, x1 = int((cx - 0.019) * w), int((cx + 0.019) * w)
    y0, y1 = int(0.552 * h), int(0.636 * h)
    sub = f[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
    if sub.size == 0:
        return 0.0
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([48, 255, 255]))
    border = np.zeros(gold.shape, np.uint8)
    by = max(2, min(12, gold.shape[0] // 4))
    bx = max(2, min(7, gold.shape[1] // 4))
    border[:by, :] = 1
    border[-by:, :] = 1
    border[:, :bx] = 1
    border[:, -bx:] = 1
    n = int(np.count_nonzero(border))
    return float(np.count_nonzero(gold[border > 0])) / n if n else 0.0


def find_claimable_activity_reward(frame: np.ndarray):
    '从 9 个奖励格中返回第一个仍有金色边框的格；灰勾/未解锁均跳过。'
    for cx in R.ACTIVITY_REWARD_XS:
        if _activity_reward_gold_score(frame, cx) >= 0.18:
            return (cx, R.PT_ACTIVITY_REWARD_Y)
    return None


def find_interest_task_card(frame: np.ndarray):
    '只 OCR 会友页的兴趣圈卡片;命中任务描述才返回卡片点击点。'
    text = ocr_text(frame, R.ROI_INTEREST_TASK_CARD)
    if "兴趣圈" in text and ("浏览" in text or "点赞" in text):
        return R.PT_INTEREST_TASK_CARD
    return None


def activity_has_claim(frame: np.ndarray) -> bool:
    '活跃度奖励区是否仍有任一金色可领取格。'
    return find_claimable_activity_reward(frame) is not None





def in_interest_circle(frame: np.ndarray) -> bool:
    '在兴趣圈(顶部 关注/发现/精选/社团,或左缘 发现/聊天/好友)。'
    top = ocr_text(frame, R.ROI_IC_TOPTABS)
    left = ocr_text(frame, R.ROI_IC_LEFTNAV)
    return ("发现" in top) or ("精选" in top) or ("发现" in left and "好友" in left)


_LIKE_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "daily" / "interest_like_thumb.png"
_LIKE_TEMPLATE_EDGES: np.ndarray | None = None


def _like_template_edges() -> np.ndarray | None:
    '缓存点赞轮廓模板;模板只含图标小区域,不会受帖子图片和点赞数影响。'
    global _LIKE_TEMPLATE_EDGES
    if _LIKE_TEMPLATE_EDGES is not None:
        return _LIKE_TEMPLATE_EDGES
    template = cv2.imread(str(_LIKE_TEMPLATE_PATH), cv2.IMREAD_GRAYSCALE)
    if template is None or template.size == 0:
        return None
    _LIKE_TEMPLATE_EDGES = cv2.Canny(template, 50, 140)
    return _LIKE_TEMPLATE_EDGES


def like_is_gold(frame: np.ndarray, pt, half=0.018) -> bool:
    '点赞按钮是否为金色已赞状态。'
    x, y = pt
    roi = (max(0.0, x - half), max(0.0, y - half), min(1.0, x + half), min(1.0, y + half))
    return _gold_ratio(frame, roi) >= 0.05


def find_like_buttons(frame: np.ndarray) -> list[dict]:
    '在扩大后的帖子区域定位所有点赞图标。'
    template = _like_template_edges()
    if template is None:
        return []
    f = normalize(frame)
    sub = crop(f, R.ROI_IC_LIKE_SEARCH)
    if sub is None or sub.size == 0:
        return []
    edges = cv2.Canny(cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY), 50, 140)
    th, tw = template.shape[:2]
    if edges.shape[0] < th or edges.shape[1] < tw:
        return []
    scores = cv2.matchTemplate(edges, template, cv2.TM_CCOEFF_NORMED)
    
    peaks = cv2.dilate(scores, np.ones((25, 25), np.uint8))
    ys, xs = np.where((scores >= peaks - 1e-6) & (scores >= 0.65))
    H, W = f.shape[:2]
    x0 = R.ROI_IC_LIKE_SEARCH[0] * W
    y0 = R.ROI_IC_LIKE_SEARCH[1] * H
    raw = sorted(
        ((float(scores[y, x]), (x0 + x + tw * 0.5) / W, (y0 + y + th * 0.5) / H)
         for y, x in zip(ys, xs)),
        reverse=True,
    )
    hits: list[dict] = []
    for score, cx, cy in raw:
        if any(abs(cx - hit["pt"][0]) < 0.025 and abs(cy - hit["pt"][1]) < 0.025 for hit in hits):
            continue
        pt = (cx, cy)
        hits.append({"pt": pt, "gold": like_is_gold(f, pt), "score": score})
    hits.sort(key=lambda hit: (round(hit["pt"][1], 2), hit["pt"][0]))
    return hits





def friend_panel_open(frame: np.ndarray) -> bool:
    t = ocr_text(frame, R.ROI_FRIEND_TABS)
    return any(k in t for k in ("我的好友", "最近相遇", "附近的人", "聊天频道", "好友"))


def friend_list_empty(frame: np.ndarray) -> bool:
    '好友面板是否明确显示空列表。'
    text = ocr_text(frame, R.ROI_FRIEND_PANEL)
    return any(word in text for word in ("暂无好友", "没有好友", "暂无家园好友", "列表为空"))


def friend_list_change_score(before: np.ndarray, after: np.ndarray) -> float:
    '列表拖动前后的平均灰度差;低分表示已到底/没有更多好友。'
    a = crop(normalize(before), R.ROI_FRIEND_PANEL)
    b = crop(normalize(after), R.ROI_FRIEND_PANEL)
    if a is None or b is None or a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    ga = cv2.GaussianBlur(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), (9, 9), 0)
    gb = cv2.GaussianBlur(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), (9, 9), 0)
    return float(cv2.absdiff(ga, gb).mean())


_FRIEND_WATER_TEMPLATE_PATH = (Path(__file__).resolve().parent.parent / "assets" / "daily"
                               / "friend_water_badge.png")
_FRIEND_WATER_TEMPLATE: np.ndarray | None = None


def _friend_water_template() -> np.ndarray | None:
    global _FRIEND_WATER_TEMPLATE
    if _FRIEND_WATER_TEMPLATE is None:
        _FRIEND_WATER_TEMPLATE = cv2.imread(str(_FRIEND_WATER_TEMPLATE_PATH), cv2.IMREAD_COLOR)
    return _FRIEND_WATER_TEMPLATE


def friend_water_houses(frame: np.ndarray) -> list[dict]:
    '动态定位当前好友列表中带蓝色水壶徽标的房屋按钮。'
    template = _friend_water_template()
    if template is None or template.size == 0:
        return []
    f = normalize(frame)
    sub = crop(f, R.ROI_FRIEND_WATER_BADGES)
    if sub is None or sub.size == 0:
        return []
    th, tw = template.shape[:2]
    if sub.shape[0] < th or sub.shape[1] < tw:
        return []
    scores = cv2.matchTemplate(sub, template, cv2.TM_CCOEFF_NORMED)
    peaks = cv2.dilate(scores, np.ones((25, 25), np.uint8))
    ys, xs = np.where((scores >= peaks - 1e-6) & (scores >= 0.95))
    H, W = f.shape[:2]
    x0 = R.ROI_FRIEND_WATER_BADGES[0] * W
    y0 = R.ROI_FRIEND_WATER_BADGES[1] * H
    hits = []
    for y, x in zip(ys, xs):
        cy = (y0 + y + th * 0.5) / H
        
        name_roi = (0.60, max(0.18, cy - 0.055), 0.84, min(0.98, cy + 0.055))
        lines = ocr_lines(f, name_roi)
        name = lines[0][0] if lines else f"row-{cy:.3f}"
        hits.append({
            "name": name,
            "pt": ((x0 + x + tw * 0.5) / W, min(0.98, cy + 0.018)),
            "badge_pt": ((x0 + x + tw * 0.5) / W, cy),
            "score": float(scores[y, x]),
        })
    hits.sort(key=lambda hit: hit["pt"][1])
    return hits


def friend_home_ready(frame: np.ndarray) -> bool:
    '已进入好友家且右下操作范围明确显示「1行」。'
    return homeland_loaded(frame) and "行" in action_mode_text(frame)


def in_friend_home(frame: np.ndarray) -> bool:
    '好友家右上有“回家”,自家同区域是“家居铺/仓库”。'
    return "回家" in ocr_text(frame, R.ROI_FRIEND_HOME_ACTIONS)


def in_own_home(frame: np.ndarray) -> bool:
    '自家右上有“家居铺/仓库”，并且左上仍有“××的居所”标题。'
    if not homeland_loaded(frame):
        return False
    text = ocr_text(frame, R.ROI_FRIEND_HOME_ACTIONS)
    return "家居" in text or "仓库" in text


def friend_water_success(frame: np.ndarray) -> bool:
    '好友浇水成功提示;该提示位置低于通用 Toast,使用专用局部 OCR。'
    return "浇水成功" in ocr_text(frame, R.ROI_FRIEND_WATER_TOAST)













