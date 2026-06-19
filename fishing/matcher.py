"""HOKWord 钓鱼状态识别(模板匹配 + 字形 IoU + OCR)。"""
from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np

from .template_bank import (
    DEFAULT_SCALES, FAST_SCALES, TemplateBank, crop as _crop, match_scales,
    normalize, pp_gray,
)

# 渔获数量:×1 / x1 / *1 等
_QTY_RE = re.compile(r"[x×X*]\s*[0-9]")
_OCR = None


def _get_ocr():
    """惰性加载中文 OCR。"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


HERE = Path(__file__).resolve().parent
TPL = HERE / "templates" / "raw"

# 归一化坐标 (x0, y0, x1, y1)
ROI_BUTTON = (0.90, 0.85, 0.978, 0.97)   # 右下角按钮簇
ROI_BANNER = (0.43, 0.27, 0.57, 0.38)    # 中央"上钩啦"横幅
ROI_SUCCESS = (0.0, 0.45, 0.30, 0.75)    # 左侧渔获奖励飘字
ROI_CAST_MSG = (0.26, 0.15, 0.74, 0.28)  # 落杆错误提示横幅
ROI_LEVELCAP = (0.28, 0.36, 0.72, 0.60)  # 等级上限弹窗正文
ROI_FBAG = (0.70, 0.83, 0.99, 0.96)      # 右下 F 放入背包按钮
ROI_QTE = (0.20, 0.06, 0.84, 0.50)       # 快速连点小键帽搜索区
ROI_DISC = (0.14, 0.10, 0.90, 0.82)      # 离散 QTE 大按钮搜索区
_QTE_LS = (40, 48)                       # 字形归一化大小(IoU 比较用)
CLICK_POINT = (0.568, 0.574)             # 抛竿/拉杆点(来自录制坐标)


def _locate_cap(sub: np.ndarray):
    """定位 QTE 方向键帽(亮白近方形块),返回 (left,top,w,h) 或 None。"""
    g = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(g, 165, 255, cv2.THRESH_BINARY)
    nl, _lab, st, _ce = cv2.connectedComponentsWithStats(bw, 8)
    best = None
    for i in range(1, nl):
        a = st[i, cv2.CC_STAT_AREA]
        ww = st[i, cv2.CC_STAT_WIDTH]
        hh = st[i, cv2.CC_STAT_HEIGHT]
        fill = a / (ww * hh + 1)
        ar = ww / max(hh, 1)
        if 150 < a < 2600 and 0.6 < ar < 1.7 and fill > 0.55 and 14 < hh < 60:
            if best is None or a > best[0]:
                best = (a, st[i, cv2.CC_STAT_LEFT], st[i, cv2.CC_STAT_TOP], ww, hh)
    return None if best is None else best[1:]


def _letter_mask(cap_gray: np.ndarray) -> np.ndarray:
    """键帽灰度 → 字形二值掩膜(暗字=255),归一化大小。"""
    _, m = cv2.threshold(cap_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return cv2.resize(m, _QTE_LS)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = float(np.logical_and(a > 0, b > 0).sum())
    union = float(np.logical_or(a > 0, b > 0).sum())
    return inter / union if union > 0 else 0.0


class FishingRecognizer:
    BLACK_MEAN = 12.0     # 近黑帧阈值
    BTN_MIN = 0.45        # 按钮最低可信分
    BANNER_HOOK = 0.70    # 横幅判上钩的分
    SUCCESS_TH = 0.72     # 渔获奖励模板分阈值(保留)
    RECORD_TH = 0.75      # 个人记录界面 F放入背包阈值
    QTE_IOU_TH = 0.55     # 连点小键帽字形 IoU 阈值
    QTE_IOU_MARGIN = 0.10 # 须明显胜过另一键,否则判 None
    RAPID_TH = 0.50       # 快速连点字样阈值
    DISC_IOU_TH = 0.55    # 离散大按钮圆心字母 IoU 阈值
    DISC_IOU_MARGIN = 0.06 # 须胜过次高(含鱼形拒识参考)

    def __init__(self) -> None:
        self.bank = TemplateBank(TPL)
        self.bank.register("ready",  "ready_button.png", ROI_BUTTON, self.BTN_MIN, pre="gray", scales=FAST_SCALES)
        self.bank.register("wait",   "wait_button.png",  ROI_BUTTON, self.BTN_MIN, pre="gray", scales=FAST_SCALES)
        self.bank.register("hook",   "hook_button.png",  ROI_BUTTON, self.BTN_MIN, pre="gray", scales=FAST_SCALES)
        self.bank.register("banner", "hook_banner.png",  ROI_BANNER, self.BANNER_HOOK, pre="gray", scales=FAST_SCALES)
        self.bank.register("fbag",   "f_putbag.png",     ROI_FBAG,   self.RECORD_TH, pre="gray",
                           scales=DEFAULT_SCALES, mask="f_putbag_mask.png")

        # 连点方向键帽参考掩膜:A/D 必备,W/S 有则加载
        self._letmask: dict[str, np.ndarray] = {}
        for k in ("A", "D", "W", "S"):
            mp = TPL / f"qte_{k}_letmask.png"
            if mp.exists():
                m = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
                if m is not None:
                    self._letmask[k] = m
        # 快速连点字样模板
        rp = TPL / "qte_text.png"
        self._rapid_tpl = pp_gray(cv2.imread(str(rp))) if rp.exists() else None
        # 离散 QTE 大按钮字母参考掩膜(A/S/W/D)
        self._disc_mask: dict[str, np.ndarray] = {}
        for k in ("A", "D", "W", "S"):
            mp = TPL / f"qte_disc_{k}_mask.png"
            if mp.exists():
                m = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
                if m is not None:
                    self._disc_mask[k] = m
        # 挣扎圈鱼形图标拒识参考(会与字母帽撞形,鱼形胜出即丢弃)
        self._disc_fish: list[np.ndarray] = []
        for k in ("F0", "F1", "F2", "F3"):
            mp = TPL / f"qte_disc_{k}_mask.png"
            if mp.exists():
                m = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
                if m is not None:
                    self._disc_fish.append(m)

    def is_hook(self, frame: np.ndarray) -> bool:
        """判上钩:中央上钩啦横幅 或 右下按钮变拉杆。"""
        f = self.bank.norm(frame)
        if self.bank.score("banner", f, normalized=True) >= self.BANNER_HOOK:
            return True
        s_hook = self.bank.score("hook", f, normalized=True)
        s_wait = self.bank.score("wait", f, normalized=True)
        s_ready = self.bank.score("ready", f, normalized=True)
        # 拉杆按钮须明显胜过取消/抛竿(图标相近)
        return s_hook >= 0.6 and s_hook >= max(s_wait, s_ready) + 0.10

    def is_success(self, frame: np.ndarray) -> bool:
        """成功 = 左侧奖励区出现『鱼』或『×1』。仅结算窗口内调用。"""
        h, w = frame.shape[:2]
        sub = frame[int(0.45 * h):int(0.75 * h), 0:int(0.32 * w)]
        try:
            res, _ = _get_ocr()(sub)
        except Exception:
            return False
        if not res:
            return False
        txt = "".join(t[1] for t in res)
        return ("鱼" in txt) or bool(_QTY_RE.search(txt))

    def is_waiting(self, frame: np.ndarray) -> bool:
        """是否进入等待咬钩(取消按钮),用于确认抛竿成功。"""
        return self.bank.hit("wait", frame)

    @staticmethod
    def _ocr_text(sub: np.ndarray, min_conf: float = 0.5) -> str:
        """OCR 该区域文字拼接,过滤低置信结果。"""
        if sub.size == 0:
            return ""
        try:
            res, _ = _get_ocr()(sub)
        except Exception:
            return ""
        if not res:
            return ""
        out = []
        for t in res:
            try:
                if float(t[2]) >= min_conf:
                    out.append(t[1])
            except (IndexError, ValueError, TypeError):
                out.append(t[1])
        return "".join(out)

    def cast_error(self, frame: np.ndarray) -> str | None:
        """落杆错误提示 → 'too_close' / 'too_far' / 'not_water' / 'shallow' / None。"""
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = ROI_CAST_MSG
        t = self._ocr_text(frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)])
        if not t:
            return None
        if "过近" in t:
            return "too_close"
        if "超出" in t or "落杆范围" in t:
            return "too_far"
        if "不在水面" in t or ("水面" in t and "不" in t):
            return "not_water"
        if "深度不足" in t or "过浅" in t:
            return "shallow"
        return None

    def is_level_cap(self, frame: np.ndarray) -> bool:
        """等级上限弹窗(屏幕中央)。"""
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = ROI_LEVELCAP
        t = self._ocr_text(frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)])
        return ("等级上限" in t) if t else False

    def is_record_screen(self, frame: np.ndarray) -> bool:
        """个人记录/渔获详情界面(以右下 F 放入背包按钮为准)。"""
        return self.bank.hit("fbag", frame)

    def qte_prompt(self, frame: np.ndarray) -> tuple[str | None, bool]:
        """返回 (key, rapid):key=方向键 'A'/'D'(无则 None);rapid=键帽旁是否有快速连点字样。"""
        if not self._letmask:
            return (None, False)
        f = self.bank.norm(frame)
        sub = _crop(f, ROI_QTE)
        cap = _locate_cap(sub)
        if cap is None:
            return (None, False)
        l, t, ww, hh = cap
        m = _letter_mask(cv2.cvtColor(sub[t:t + hh, l:l + ww], cv2.COLOR_BGR2GRAY))
        scored = sorted(((_iou(m, mask), k) for k, mask in self._letmask.items()), reverse=True)
        best_iou, best_k = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        key = best_k if (best_iou >= self.QTE_IOU_TH and best_iou - second >= self.QTE_IOU_MARGIN) else None
        return (key, self._rapid_near_cap(sub, l, t, ww, hh))

    def _rapid_near_cap(self, sub: np.ndarray, l: int, t: int, ww: int, hh: int) -> bool:
        """键帽两侧是否有快速连点字样(布局随方向镜像,故左右一起搜)。"""
        if self._rapid_tpl is None:
            return False
        h, w = sub.shape[:2]
        x0 = max(0, l - int(7 * hh))
        x1 = min(w, l + ww + int(7 * hh))
        y0 = max(0, t - int(0.6 * hh))
        y1 = min(h, t + int(2.4 * hh))
        if x1 - x0 < 12 or y1 - y0 < 8:
            return False
        region = cv2.cvtColor(sub[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        base = 2.1 * hh / self._rapid_tpl.shape[0]      # 文字高约 2.1×键帽高
        return match_scales(region, self._rapid_tpl, (base * 0.8, base, base * 1.2)) >= self.RAPID_TH

    def qte_key(self, frame: np.ndarray) -> str | None:
        """仅返回连点小键帽方向键。"""
        return self.qte_prompt(frame)[0]

    def _disc_center_letter(self, g: np.ndarray, cx: int, cy: int) -> np.ndarray | None:
        """取圆心 ±22px 内最大的亮字母连通块 → 归一化字形掩膜。"""
        r = 22
        h, w = g.shape[:2]
        sub = g[max(0, cy - r):min(h, cy + r), max(0, cx - r):min(w, cx + r)]
        if sub.size == 0:
            return None
        _, bw = cv2.threshold(sub, 165, 255, cv2.THRESH_BINARY)
        nl, _lab, st, _ce = cv2.connectedComponentsWithStats(bw, 8)
        best = None
        for i in range(1, nl):
            a = st[i, cv2.CC_STAT_AREA]
            ww = st[i, cv2.CC_STAT_WIDTH]; hh = st[i, cv2.CC_STAT_HEIGHT]
            if not (80 < a < 1000 and 16 < hh < 44 and 0.4 < ww / max(hh, 1) < 1.5):
                continue
            if best is None or a > best[0]:
                best = (a, st[i, cv2.CC_STAT_LEFT], st[i, cv2.CC_STAT_TOP], ww, hh)
        if best is None:
            return None
        _, l, t, ww, hh = best
        return cv2.resize(bw[t:t + hh, l:l + ww], _QTE_LS)

    def qte_disc(self, frame: np.ndarray) -> str | None:
        """离散 QTE 大按钮:HoughCircles 定位圆形按钮 + 圆心字母 IoU,返回应按的键或 None。"""
        if not self._disc_mask:
            return None
        f = self.bank.norm(frame)
        g = cv2.cvtColor(_crop(f, ROI_DISC), cv2.COLOR_BGR2GRAY)
        small = cv2.medianBlur(cv2.resize(g, (g.shape[1] // 2, g.shape[0] // 2)), 3)
        cc = cv2.HoughCircles(small, cv2.HOUGH_GRADIENT, 1, 40,
                              param1=120, param2=22, minRadius=18, maxRadius=40)
        if cc is None:
            return None
        best_key, best_v = None, 0.0
        for c in cc[0]:
            cx, cy = int(c[0]) * 2, int(c[1]) * 2          # 半尺度 → 原尺度
            m = self._disc_center_letter(g, cx, cy)
            if m is None:
                continue
            scored = [(_iou(m, rm), k) for k, rm in self._disc_mask.items()]
            scored += [(_iou(m, fm), "_FISH") for fm in self._disc_fish]
            scored.sort(reverse=True)
            v, k = scored[0]
            sec = scored[1][0] if len(scored) > 1 else 0.0
            if k in self._disc_mask and v >= self.DISC_IOU_TH and (v - sec) >= self.DISC_IOU_MARGIN:
                if v > best_v:
                    best_v, best_key = v, k
        return best_key

    def button_state(self, frame: np.ndarray) -> tuple[str, float]:
        """右下按钮:'ready' / 'wait' / 'hook' / 'none'。"""
        f = self.bank.norm(frame)
        s_ready = self.bank.score("ready", f, normalized=True)
        s_wait = self.bank.score("wait", f, normalized=True)
        s_hook = self.bank.score("hook", f, normalized=True)
        best = max(s_ready, s_wait, s_hook)
        if best < self.BTN_MIN:
            return "none", best
        if s_ready >= s_wait and s_ready >= s_hook:
            return "ready", s_ready
        if s_hook >= s_wait:
            return "hook", s_hook
        return "wait", s_wait

    def scores(self, frame: np.ndarray) -> dict:
        f = self.bank.norm(frame)
        small = cv2.cvtColor(cv2.resize(f, (96, 54)), cv2.COLOR_BGR2GRAY)
        return {
            "black_mean": round(float(small.mean()), 1),
            "ready": round(self.bank.score("ready", f, normalized=True), 3),
            "wait": round(self.bank.score("wait", f, normalized=True), 3),
            "hook_btn": round(self.bank.score("hook", f, normalized=True), 3),
            "banner": round(self.bank.score("banner", f, normalized=True), 3),
        }

    def classify(self, frame: np.ndarray) -> tuple[str, dict]:
        s = self.scores(frame)
        if s["black_mean"] < self.BLACK_MEAN:
            return "RESULT_OR_TRANSITION", s
        if s["banner"] >= self.BANNER_HOOK or (
            s["hook_btn"] >= self.BTN_MIN and s["hook_btn"] >= max(s["ready"], s["wait"]) + 0.03
        ):
            return "HOOK_PROMPT", s
        cand = max(("FISHING_READY", s["ready"]), ("WAITING_FOR_BITE", s["wait"]), key=lambda x: x[1])
        if cand[1] >= self.BTN_MIN:
            return cand[0], s
        return "UNKNOWN", s
