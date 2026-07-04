"""okww 式模板识别小框架(逐特征预处理 + 命名匹配)。

设计目标:以后所有功能(钓鱼/剧情跳过/日常/红光闪避)都走同一套识别——
  每个模板 = 名字 + 图 + 可选预处理(原图灰度/二值/白掩膜) + ROI + 阈值 + 尺度集(+可选掩膜)。
关键点(参考 ok-wuthering-waves):**同一种预处理同时作用于「加载时的模板」与「匹配时的实时裁切」**,
再做多尺度 TM_CCOEFF_NORMED。这样:
  · 白色文字/图标(上钩啦、按钮文字、F 放入背包)用二值/白掩膜,抗半透明背景与水色噪声;
  · 新增模板只需 register(...) 一行,不必再到处写 _match_scales/_match_masked;
  · 省内存:模板加载即预处理只存一份,绝不堆「所有情况」的图。

okww 三种预处理对应:
  binarize_for_matching → 'binary'(gray→threshold 244→白)   :亮白文字/图标
  convert_bw            → 'white' (inRange 244..255 白)        :纯白 UI
  convert_dialog_icon   → 'dim'   (inRange 210..244 略暗白)     :略暗图标
  原图灰度              → 'gray'                                 :彩色/渐变图标
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

# 限 OpenCV 并行线程:默认=逻辑核全开(16),对每秒几百次 ~1ms 的小 ROI matchTemplate/resize
# 纯是线程池唤醒/自旋开销,还与游戏渲染线程争核 → 帧数/低帧抖动(实测 2 线程下单次耗时几乎不变:
# 整帧 resize 0.7→1.2ms、matchTemplate ~1ms 持平)。BetterGI/okww 同为"延迟模式、把核让给游戏"。
cv2.setNumThreads(2)

NORM_W = 1920  # 模板在 1920 宽下裁切;实时帧先归一化到此宽再匹配

# 容忍 4K 模板 vs 实测 HUD 缩放差异的默认尺度集
DEFAULT_SCALES = (1.0, 0.85, 0.72, 1.18, 1.4, 0.6)
# 热路径(按钮/横幅)用的精简尺度集,兼顾速度与分辨率容差
FAST_SCALES = (1.0, 0.85, 1.18)

# ---- okww 预处理参数 ----
_LOWER_WHITE = np.array([244, 244, 244], dtype=np.uint8)
_UPPER_WHITE = np.array([255, 255, 255], dtype=np.uint8)
_LOWER_DIM = np.array([210, 210, 210], dtype=np.uint8)
_UPPER_DIM = np.array([244, 244, 244], dtype=np.uint8)


def _to_gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def pp_gray(img: np.ndarray) -> np.ndarray:
    """原图灰度(默认):彩色/渐变图标。"""
    return _to_gray(img)


def pp_binary(img: np.ndarray) -> np.ndarray:
    """okww binarize_for_matching:gray→threshold(244)→白。亮白文字/图标。"""
    _, b = cv2.threshold(_to_gray(img), 244, 255, cv2.THRESH_BINARY)
    return b


def pp_white(img: np.ndarray) -> np.ndarray:
    """okww convert_bw:inRange(244..255) 白掩膜。纯白 UI。"""
    if img.ndim == 2:
        return img
    return cv2.inRange(img, _LOWER_WHITE, _UPPER_WHITE)


def pp_dim(img: np.ndarray) -> np.ndarray:
    """okww convert_dialog_icon:inRange(210..244) 略暗白图标。"""
    if img.ndim == 2:
        return img
    return cv2.inRange(img, _LOWER_DIM, _UPPER_DIM)


PREPROCESS = {"gray": pp_gray, "binary": pp_binary, "white": pp_white, "dim": pp_dim}


def normalize(frame: np.ndarray) -> np.ndarray:
    """归一化到 1920 宽(模板基准)。已是该宽则原样返回。"""
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
    """多尺度 TM_CCOEFF_NORMED 最佳分。sub/tpl 均为单通道(已预处理)。
    mask 不为空时做带掩膜匹配(只比对掩膜内像素,忽略半透明背景)。"""
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
    roi: tuple                       # 归一化 (x0,y0,x1,y1)
    thresh: float
    pre: str = "gray"                # 预处理:gray/binary/white/dim
    scales: tuple = DEFAULT_SCALES
    tpl: np.ndarray = field(default=None, repr=False)   # 已预处理的模板(单通道)
    mask: np.ndarray = field(default=None, repr=False)  # 可选灰度掩膜


class TemplateBank:
    """命名模板库。register() 登记,score()/hit() 匹配。

    用法:
        bank = TemplateBank(templates_dir)
        bank.register("ready", "ready_button.png", ROI_BUTTON, 0.45, pre="gray", scales=FAST_SCALES)
        f = bank.norm(frame)                 # 整帧只归一化一次
        if bank.hit("ready", f, normalized=True): ...
    """

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
        """命名匹配:裁 ROI → 同款预处理 → 多尺度匹配,返回最佳分。"""
        t = self._t[name]
        f = frame if normalized else normalize(frame)
        sub = PREPROCESS[t.pre](crop(f, t.roi))
        return match_scales(sub, t.tpl, t.scales, t.mask)

    def hit(self, name: str, frame: np.ndarray, normalized: bool = False) -> bool:
        return self.score(name, frame, normalized) >= self._t[name].thresh

    def best_of(self, names, frame: np.ndarray, normalized: bool = False) -> tuple[str, float]:
        """在共享/各自 ROI 的若干命名模板中取最高分者。返回 (name, score)。"""
        f = frame if normalized else normalize(frame)
        best_n, best_s = "", 0.0
        for n in names:
            s = self.score(n, f, normalized=True)
            if s > best_s:
                best_n, best_s = n, s
        return best_n, best_s
