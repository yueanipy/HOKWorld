"""HOKWord 剧情 / 实时画面识别(OCR + 右下 HUD 判定)。"""
from __future__ import annotations

import cv2
import numpy as np

from fishing.matcher import _get_ocr
from fishing.template_bank import normalize

# 归一化坐标 (x0,y0,x1,y1)
ROI_TR = (0.76, 0.0, 1.0, 0.075)            # 右上控制条:抓拍/自动/跳过
ROI_DLG_TITLE = (0.26, 0.34, 0.74, 0.52)    # 确认框标题
ROI_DLG_BTN = (0.30, 0.58, 0.70, 0.82)      # 确认框按钮区(取消/跳过)
ROI_HUD = (0.80, 0.78, 1.0, 1.0)            # 右下技能 HUD
ROI_CENTER = (0.20, 0.28, 0.80, 0.86)       # 居中交互框(宝箱等)
ROI_CONTINUE = (0.26, 0.60, 0.74, 0.98)     # 点击继续提示


def _crop_off(frame: np.ndarray, roi):
    """按 ROI 裁切,返回 (子图, 左偏移x, 上偏移y)。"""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = roi
    ox, oy = int(x0 * w), int(y0 * h)
    return frame[oy:int(y1 * h), ox:int(x1 * w)], ox, oy


class StoryRecognizer:
    MIN_CONF = 0.45      # OCR 置信度过滤
    HUD_EDGE_TH = 0.015  # 右下 HUD 边缘密度阈值,超过即游戏态
    BLACK_LO = 0.50      # 黑屏过场近黑比例下限
    BLACK_HI = 0.99      # 上限,再高就是纯黑加载,不点

    def _ocr(self, sub: np.ndarray):
        if sub.size == 0:
            return []
        try:
            res, _ = _get_ocr()(sub)
        except Exception:
            return []
        out = []
        for item in (res or []):
            box, txt, score = item[0], item[1], item[2]
            try:
                if float(score) >= self.MIN_CONF:
                    out.append((box, txt))
            except (TypeError, ValueError):
                out.append((box, txt))
        return out

    def _texts(self, frame: np.ndarray, roi):
        sub, _, _ = _crop_off(normalize(frame), roi)
        return [t for _, t in self._ocr(sub)]

    def bar_state(self, frame: np.ndarray) -> str | None:
        """右上控制条状态:'skip' / 'auto' / None。"""
        tr = "".join(self._texts(frame, ROI_TR))
        has_cap = ("抓拍" in tr) or ("自动" in tr)
        if "不可跳过" in tr:
            return None
        if "跳过" in tr and (has_cap or "esc" in tr.lower()):
            return "skip"
        if has_cap:
            return "auto"
        return None

    def in_gameplay(self, frame: np.ndarray) -> bool:
        """右下技能 HUD 是否存在(在 = 普通游戏态,过场/剧情时隐藏)。"""
        f = normalize(frame)
        sub, _, _ = _crop_off(f, ROI_HUD)
        if sub.size == 0:
            return False
        sub = cv2.resize(sub, (240, 120))
        g = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
        return float(cv2.Canny(g, 80, 200).mean() / 255.0) > self.HUD_EDGE_TH

    def is_interact_prompt(self, frame: np.ndarray) -> bool:
        """居中交互框(宝箱·点击开启 等),不是剧情。"""
        t = "".join(self._texts(frame, ROI_CENTER))
        return ("开启" in t) or ("宝箱" in t)

    def skip_dialog_point(self, frame: np.ndarray):
        """跳过确认框命中则返回「跳过」按钮中心坐标,否则 None。"""
        f = normalize(frame)
        h, w = f.shape[:2]
        title = "".join(self._texts(frame, ROI_DLG_TITLE))
        if "是否跳过" not in title and "跳过本段" not in title:
            return None
        bsub, ox, oy = _crop_off(f, ROI_DLG_BTN)
        for box, txt in self._ocr(bsub):
            t = txt.strip()
            if "跳过" in t and "取消" not in t:        # 只点跳过,不点取消
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                cx = (ox + sum(xs) / len(xs)) / w
                cy = (oy + sum(ys) / len(ys)) / h
                return (float(cx), float(cy))
        return None

    def is_continue_prompt(self, frame: np.ndarray) -> bool:
        """点击空白处继续 / 轻触屏幕 类提示。"""
        t = "".join(self._texts(frame, ROI_CONTINUE))
        if "空白处" in t:
            return True
        return ("继续" in t) and ("点击" in t or "轻触" in t or "屏幕" in t)

    def black_ratio(self, frame: np.ndarray) -> float:
        """中间三分之一近黑像素比例。"""
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h = g.shape[0]
        mid = g[h // 3:2 * h // 3, :]
        return float((mid < 16).mean())

    def classify(self, frame: np.ndarray):
        """归类当前帧,返回 (状态, 跳过按钮坐标或 None)。优先级从高到低。"""
        if self.in_gameplay(frame):
            return "play", None
        pt = self.skip_dialog_point(frame)
        if pt:
            return "confirm", pt
        if self.is_interact_prompt(frame):
            return "interact", None
        bar = self.bar_state(frame)
        if bar == "skip":
            return "skip", None
        if bar == "auto":
            return "dialogue", None
        if self.is_continue_prompt(frame):
            return "continue", None
        if self.BLACK_LO <= self.black_ratio(frame) < self.BLACK_HI:
            return "black", None
        return "immersive", None
