"""模板识别小框架(逐特征预处理 + 命名匹配)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

NORM_W = 1920  # 模板在 1920 宽下裁切;实时帧先归一化到此宽再匹配

DEFAULT_SCALES = (1.0, 0.85, 0.72, 1.18, 1.4, 0.6)   # 容忍分辨率/缩放差异
FAST_SCALES = (1.0, 0.85, 1.18)                      # 热路径精简尺度集

_LOWER_WHITE = np.array([244, 244, 244], dtype=np.uint8)
_UPPER_WHITE = np.array([255, 255, 255], dtype=np.uint8)
_LOWER_DIM = np.array([210, 210, 210], dtype=np.uint8)
_UPPER_DIM = np.array([244, 244, 244], dtype=np.uint8)


def _to_gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def pp_gray(img: np.ndarray) -> np.ndarray:
    """原图灰度:彩色/渐变图标。"""
    return _to_gray(img)


def pp_binary(img: np.ndarray) -> np.ndarray:
    """二值化 gray→threshold(244)→白:亮白文字/图标。"""
    _, b = cv2.threshold(_to_gray(img), 244, 255, cv2.THRESH_BINARY)
    return b


def pp_white(img: np.ndarray) -> np.ndarray:
    """白掩膜 inRange(244..255):纯白 UI。"""
    if img.ndim == 2:
        return img
    return cv2.inRange(img, _LOWER_WHITE, _UPPER_WHITE)


def pp_dim(img: np.ndarray) -> np.ndarray:
    """略暗白图标 inRange(210..244)。"""
    if img.ndim == 2:
        return img
    return cv2.inRange(img, _LOWER_DIM, _UPPER_DIM)


PREPROCESS = {"gray": pp_gray, "binary": pp_binary, "white": pp_white, "dim": pp_dim}


def normalize(frame: np.ndarray) -> np.ndarray:
    """归一化到 1920 宽,已是该宽则原样返回。"""
    h, w = frame.shape[:2]
    if w == NORM_W:
        return frame
    return cv2.resize(frame, (NORM_W, int(h * NORM_W / w)), interpolation=cv2.INTER_AREA)


def crop(frame: np.ndarray, roi) -> np.ndarray:
    """按归一化坐标 (x0,y0,x1,y1) 裁切。"""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = roi
    return frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def match_scales(sub: np.ndarray, tpl: np.ndarray, scales, mask: np.ndarray | None = None) -> float:
    """多尺度 TM_CCOEFF_NORMED 最佳分(sub/tpl 单通道)。mask 不为空则带掩膜匹配。"""
    if sub is None or tpl is None or sub.size == 0 or tpl.size == 0:
        return 0.0
    if sub.ndim == 3:
        sub = _to_gray(sub)
    if tpl.ndim == 3:
        tpl = _to_gray(tpl)
    sh, sw = sub.shape[:2]
    best = 0.0
    for s in scales:
        th, tw = int(tpl.shape[0] * s), int(tpl.shape[1] * s)
        if th < 8 or tw < 8 or th > sh or tw > sw:
            continue
        t = cv2.resize(tpl, (tw, th))
        if mask is not None:
            m = cv2.resize(mask, (tw, th))
            r = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED, mask=m)
            r = np.where(np.isfinite(r), r, 0.0)
        else:
            r = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED)
        best = max(best, float(r.max()))
    return best


@dataclass
class Template:
    name: str
    roi: tuple
    thresh: float
    pre: str = "gray"
    scales: tuple = DEFAULT_SCALES
    tpl: np.ndarray = field(default=None, repr=False)
    mask: np.ndarray = field(default=None, repr=False)


class TemplateBank:
    """命名模板库:register() 登记,score()/hit() 匹配。"""

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
        t.tpl = PREPROCESS[pre](img)   # 加载即预处理,只存一份
        if mask:
            m = cv2.imread(str(self.dir / mask), cv2.IMREAD_GRAYSCALE)
            if m is None:
                raise FileNotFoundError(f"掩膜缺失: {self.dir / mask}")
            t.mask = m
        self._t[name] = t

    def has(self, name: str) -> bool:
        return name in self._t

    def score(self, name: str, frame: np.ndarray, normalized: bool = False) -> float:
        """裁 ROI → 预处理 → 多尺度匹配,返回最佳分。"""
        t = self._t[name]
        f = frame if normalized else normalize(frame)
        sub = PREPROCESS[t.pre](crop(f, t.roi))
        return match_scales(sub, t.tpl, t.scales, t.mask)

    def hit(self, name: str, frame: np.ndarray, normalized: bool = False) -> bool:
        return self.score(name, frame, normalized) >= self._t[name].thresh

    def best_of(self, names, frame: np.ndarray, normalized: bool = False) -> tuple[str, float]:
        """若干命名模板中取最高分者,返回 (name, score)。"""
        f = frame if normalized else normalize(frame)
        best_n, best_s = "", 0.0
        for n in names:
            s = self.score(n, f, normalized=True)
            if s > best_s:
                best_n, best_s = n, s
        return best_n, best_s
