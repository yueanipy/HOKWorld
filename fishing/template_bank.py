'同类脚本 式模板识别小框架(逐特征预处理 + 命名匹配)。'
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np




cv2.setNumThreads(1)

NORM_W = 1920  


DEFAULT_SCALES = (1.0, 0.85, 0.72, 1.18, 1.4, 0.6)

FAST_SCALES = (1.0, 0.85, 1.18)


_LOWER_WHITE = np.array([244, 244, 244], dtype=np.uint8)
_UPPER_WHITE = np.array([255, 255, 255], dtype=np.uint8)
_LOWER_DIM = np.array([210, 210, 210], dtype=np.uint8)
_UPPER_DIM = np.array([244, 244, 244], dtype=np.uint8)


def _to_gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def pp_gray(img: np.ndarray) -> np.ndarray:
    '原图灰度(默认):彩色/渐变图标。'
    return _to_gray(img)


def pp_binary(img: np.ndarray) -> np.ndarray:
    '同类脚本 binarizeformatching:gray→threshold(244)→白。'
    _, b = cv2.threshold(_to_gray(img), 244, 255, cv2.THRESH_BINARY)
    return b


def pp_white(img: np.ndarray) -> np.ndarray:
    '同类脚本 convertbw:inRange(244..255) 白掩膜。'
    if img.ndim == 2:
        return img
    return cv2.inRange(img, _LOWER_WHITE, _UPPER_WHITE)


def pp_dim(img: np.ndarray) -> np.ndarray:
    '同类脚本 convertdialogicon:inRange(210..244) 略暗白图标。'
    if img.ndim == 2:
        return img
    return cv2.inRange(img, _LOWER_DIM, _UPPER_DIM)


PREPROCESS = {"gray": pp_gray, "binary": pp_binary, "white": pp_white, "dim": pp_dim}


def normalize(frame):
    '归一化到 1920 宽(模板基准)。'
    h, w = frame.shape[:2]
    if w == NORM_W:
        return frame
    return cv2.resize(frame, (NORM_W, int(h * NORM_W / w)), interpolation=cv2.INTER_AREA)


def crop(frame, roi) -> np.ndarray:
    '按归一化坐标 (x0,y0,x1,y1) 裁切。'
    crop_roi = getattr(frame, "crop_roi", None)
    if crop_roi is not None:
        return crop_roi(roi)
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = roi
    return frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def preprocess_crop(frame: np.ndarray, roi, pre: str) -> np.ndarray:
    '裁切并预处理 ROI。'
    roi_key = tuple(float(v) for v in roi)
    return PREPROCESS[pre](crop(frame, roi_key))


def preprocess_box(frame, box, pre: str) -> np.ndarray:
    '裁切并预处理像素框；用于随提示位置变化的动态区域。'
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in box)
    clipped = (max(0, x0), max(0, y0), min(w, x1), min(h, y1))
    crop_box = getattr(frame, "crop_box", None)
    sub = (crop_box(clipped) if crop_box is not None
           else frame[clipped[1]:clipped[3], clipped[0]:clipped[2]])
    return PREPROCESS[pre](sub)


def prepare_scales(tpl: np.ndarray, scales, mask: np.ndarray | None = None) -> tuple:
    '按原有取整和插值方式预生成多尺度模板及掩膜。'
    if tpl is None or tpl.size == 0:
        return ()
    if tpl.ndim == 3:
        tpl = _to_gray(tpl)
    out = []
    for s in scales:
        th, tw = int(tpl.shape[0] * s), int(tpl.shape[1] * s)
        if th < 8 or tw < 8:
            continue
        scaled_tpl = cv2.resize(tpl, (tw, th))
        scaled_mask = cv2.resize(mask, (tw, th)) if mask is not None else None
        out.append((scaled_tpl, scaled_mask))
    return tuple(out)


def match_prepared_scales(sub: np.ndarray, prepared) -> float:
    '使用预生成的多尺度模板计算最佳匹配分。'
    if sub is None or sub.size == 0:
        return 0.0
    if sub.ndim == 3:
        sub = _to_gray(sub)
    sh, sw = sub.shape[:2]
    best = 0.0
    for t, m in prepared:
        th, tw = t.shape[:2]
        if th > sh or tw > sw:
            continue
        if m is not None:
            r = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED, mask=m)
            r = np.where(np.isfinite(r), r, 0.0)
        else:
            r = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED)
        best = max(best, float(r.max()))
    return best


def match_scales(sub: np.ndarray, tpl: np.ndarray, scales, mask: np.ndarray | None = None) -> float:
    '多尺度 TMCCOEFFNORMED 最佳分，保留兼容旧调用。'
    if sub is None or tpl is None or sub.size == 0 or tpl.size == 0:
        return 0.0
    return match_prepared_scales(sub, prepare_scales(tpl, scales, mask))


@dataclass
class Template:
    name: str
    roi: tuple                       
    thresh: float
    pre: str = "gray"                
    scales: tuple = DEFAULT_SCALES
    tpl: np.ndarray = field(default=None, repr=False)   
    mask: np.ndarray = field(default=None, repr=False)  
    prepared: tuple = field(default_factory=tuple, repr=False)  


class TemplateBank:
    '命名模板库。'

    def __init__(self, tpl_dir) -> None:
        self.dir = Path(tpl_dir)
        self._t: dict[str, Template] = {}

    def norm(self, frame: np.ndarray) -> np.ndarray:
        return normalize(frame)

    def register(self, name: str, file: str, roi, thresh: float,
                 pre: str = "gray", scales=DEFAULT_SCALES, mask: str | None = None) -> None:
        if pre not in PREPROCESS:
            raise ValueError(f"未知预处理 {pre!r},可选 {list(PREPROCESS)}")
        img = cv2.imread(str(self.dir / file))
        if img is None:
            raise FileNotFoundError(f"模板缺失: {self.dir / file}")
        t = Template(name=name, roi=tuple(roi), thresh=float(thresh), pre=pre, scales=tuple(scales))
        t.tpl = PREPROCESS[pre](img)   
        if mask:
            m = cv2.imread(str(self.dir / mask), cv2.IMREAD_GRAYSCALE)
            if m is None:
                raise FileNotFoundError(f"掩膜缺失: {self.dir / mask}")
            t.mask = m
        t.prepared = prepare_scales(t.tpl, t.scales, t.mask)
        self._t[name] = t

    def has(self, name: str) -> bool:
        return name in self._t

    def score(self, name: str, frame: np.ndarray, normalized: bool = False) -> float:
        '命名匹配:裁 ROI → 同款预处理 → 多尺度匹配,返回最佳分。'
        t = self._t[name]
        f = frame if normalized else normalize(frame)
        sub = preprocess_crop(f, t.roi, t.pre)
        return match_prepared_scales(sub, t.prepared)

    def hit(self, name: str, frame: np.ndarray, normalized: bool = False) -> bool:
        return self.score(name, frame, normalized) >= self._t[name].thresh

    def best_of(self, names, frame: np.ndarray, normalized: bool = False) -> tuple[str, float]:
        '在共享/各自 ROI 的若干命名模板中取最高分者。'
        f = frame if normalized else normalize(frame)
        best_n, best_s = "", 0.0
        for n in names:
            s = self.score(n, f, normalized=True)
            if s > best_s:
                best_n, best_s = n, s
        return best_n, best_s
