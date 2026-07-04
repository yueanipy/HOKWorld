"""HOKWord 钓鱼状态识别。

统一走 okww 式小框架 `TemplateBank`(见 template_bank.py):每个模板 = 名字 + 图 +
可选预处理(原图灰度/二值/白掩膜)+ ROI + 阈值 + 尺度集。以后剧情跳过/日常/红光闪避
只需 register 新模板即可,识别口径一致、省内存。

判别依据(均来自真实客户端帧):
  右下角圆形按钮文字:  抛竿=ready  取消=wait  拉杆=hook
  中央横幅:           "上钩啦" -> 上钩(主信号,时机关键)
  左侧奖励飘字:        含"鱼"或"×1" -> 成功(OCR)
  右下"F 放入背包":   个人新纪录界面(带掩膜匹配)
  方向键帽 A/D:        特殊大鱼收线 QTE(亮键帽定位 + 字形 IoU,见 qte_key)
"""
from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np

from .template_bank import (
    DEFAULT_SCALES, FAST_SCALES, TemplateBank, crop as _crop, match_scales,
    normalize, pp_gray,
)

# 渔获数量标记:×1 / x1 / *1 等
_QTY_RE = re.compile(r"[x×X*]\s*[0-9]")
_OCR = None


def _limit_ocr_threads() -> None:
    """把 RapidOCR 三个 onnxruntime 会话的 intra 线程压到 2(默认=物理核数;OCR 每秒才几次、
    延迟不敏感,却和游戏渲染争核 → 掉帧)。本机 rapidocr 老版不暴露线程参数 → 在**构造前**把
    其模块里的 SessionOptions 换成"预设线程上限"的工厂(不动 site-packages);结构不认识/出错
    则原样回退——识别行为完全不变,只是少了线程上限。"""
    try:
        import rapidocr_onnxruntime.utils as _ru
        from onnxruntime import SessionOptions as _SO
        if getattr(_ru, "SessionOptions", None) is not _SO:
            return                       # 已打过补丁 / 未知版本结构 → 不动
        def _capped(*a, **k):
            so = _SO(*a, **k)
            so.intra_op_num_threads = 2
            so.inter_op_num_threads = 1
            return so
        _ru.SessionOptions = _capped     # OrtInferSession 按模块名晚绑定取用 → 生效
    except Exception:
        pass


def _get_ocr():
    """惰性加载中文 OCR(首次约 1-2s)。"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _limit_ocr_threads()             # 须在 RapidOCR() 建会话之前
        _OCR = RapidOCR()
    return _OCR


HERE = Path(__file__).resolve().parent
try:                                       # 发布版:模板在 PyInstaller 解包目录(sys._MEIPASS)
    from paths import resource_path
    TPL = resource_path("fishing", "templates", "raw")
except Exception:                          # 开发版:源码相对
    TPL = HERE / "templates" / "raw"

# 归一化坐标 (x0, y0, x1, y1)
ROI_BUTTON = (0.90, 0.85, 0.978, 0.97)   # 右下角按钮簇
ROI_BANNER = (0.43, 0.27, 0.57, 0.38)    # 中央"上钩啦"横幅(留余量供滑动)
ROI_SUCCESS = (0.0, 0.45, 0.30, 0.75)    # 左侧渔获奖励飘字
ROI_CAST_MSG = (0.26, 0.15, 0.74, 0.28)  # 落杆错误提示横幅(顶部中央白字)
ROI_LEVELCAP = (0.28, 0.36, 0.72, 0.60)  # "等级上限"弹窗正文(屏幕中央)
ROI_FBAG = (0.70, 0.83, 0.99, 0.96)      # 右下"F 放入背包"按钮
ROI_QTE = (0.20, 0.06, 0.84, 0.50)       # 快速连点小键帽:随鱼移动,放宽搜索区
ROI_DISC = (0.14, 0.10, 0.90, 0.82)      # 离散 QTE 大按钮(圆形按钮+亮白字母):位置随鱼大范围移动
_QTE_LS = (40, 48)                       # 字形归一化大小(IoU 比较;连点小键帽 & 离散字母共用)
# 自动化点击点(抛竿/拉杆同一处),来自录制输入的归一化坐标
CLICK_POINT = (0.568, 0.574)


# ---- QTE 方向键帽:亮键帽定位 + 字形 IoU(模板/ORB 对 ~40px 小字母都失效,故专用)----
def _locate_cap(sub: np.ndarray):
    """在区域内定位 QTE 方向键帽(亮白的近方形填充块)。返回 (left,top,w,h) 或 None。"""
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
    """键帽灰度图 → 字形二值掩膜(暗字=255),归一化大小。"""
    _, m = cv2.threshold(cap_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return cv2.resize(m, _QTE_LS)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = float(np.logical_and(a > 0, b > 0).sum())
    union = float(np.logical_or(a > 0, b > 0).sum())
    return inter / union if union > 0 else 0.0


class FishingRecognizer:
    BLACK_MEAN = 12.0     # 近黑帧阈值
    BTN_MIN = 0.45        # 按钮最低可信分
    BANNER_HOOK = 0.70    # 横幅判 HOOK 的分(预备/水波噪声~0.53,真上钩~0.9)
    SUCCESS_TH = 0.72     # (保留)渔获奖励模板分阈值
    RECORD_TH = 0.75      # "个人记录"界面 F放入背包(真记录界面~0.87,QTE/普通帧≤0.70)
    QTE_IOU_TH = 0.55     # 连点小键帽字形 IoU 判定(正确键 A≥0.84/D≥0.65,错键≤0.48)
    QTE_IOU_MARGIN = 0.10 # 须明显胜过另一键(否则 None,避免按错方向)
    RAPID_TH = 0.50       # "快速连点"字样判定(键帽右侧同高带;辅助信号,核心靠小键帽)
    DISC_IOU_TH = 0.55    # 离散大按钮圆心字母 IoU 判定(A/S/W/D);实测正确字母≥0.76
    DISC_IOU_MARGIN = 0.06 # 须胜过次高(含鱼形拒识参考)

    # 钓鱼必备关键模板(缺任一 → ready=False,fisher 提示「缺关键模板」并停手、不发键鼠)。
    # 含:右下按钮(抛竿/取消/拉杆)、上钩横幅、F放入背包、以及 AD-QTE(方向键帽 A/D + 连点字样)。
    KEY_TPLS = ("ready_button.png", "wait_button.png", "hook_button.png", "hook_banner.png",
                "f_putbag.png", "f_putbag_mask.png",
                "qte_A.png", "qte_A_letmask.png", "qte_D.png", "qte_D_letmask.png", "qte_text.png")

    def __init__(self) -> None:
        # okww/MaaNTE 式命名模板库:每个模板配 ROI + 阈值 + 预处理 + 尺度集 + 可选掩膜
        self.bank = TemplateBank(TPL)
        self._letmask: dict[str, np.ndarray] = {}       # QTE 方向键帽字形掩膜(A/D 必备,W/S 选配)
        self._rapid_tpl = None                          # "快速连点"字样模板
        self._disc_mask: dict[str, np.ndarray] = {}     # 离散 QTE 大按钮字母掩膜
        self._disc_fish: list[np.ndarray] = []          # 挣扎圈鱼形拒识参考
        # **与 gather/story 一致地暴露 ready**(此前漏设 → getattr(rec,'ready',False) 恒 False、钓鱼总报缺模板)。
        self.ready = all((TPL / f).exists() for f in self.KEY_TPLS)
        if not self.ready:
            return                                      # 缺关键模板:不注册(免 register 抛错),交 fisher 提示停手

        self.bank.register("ready",  "ready_button.png", ROI_BUTTON, self.BTN_MIN, pre="gray", scales=FAST_SCALES)
        self.bank.register("wait",   "wait_button.png",  ROI_BUTTON, self.BTN_MIN, pre="gray", scales=FAST_SCALES)
        self.bank.register("hook",   "hook_button.png",  ROI_BUTTON, self.BTN_MIN, pre="gray", scales=FAST_SCALES)
        self.bank.register("banner", "hook_banner.png",  ROI_BANNER, self.BANNER_HOOK, pre="gray", scales=FAST_SCALES)
        # F 放入背包:白文字按钮,带掩膜匹配(全尺度,较慢,非热路径)
        self.bank.register("fbag",   "f_putbag.png",     ROI_FBAG,   self.RECORD_TH, pre="gray",
                           scales=DEFAULT_SCALES, mask="f_putbag_mask.png")

        # 特殊收线方向键帽:A/D 必备,W/S 有则加载(后续补录)。参考掩膜内存极小。
        for k in ("A", "D", "W", "S"):
            mp = TPL / f"qte_{k}_letmask.png"
            if mp.exists():
                m = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
                if m is not None:
                    self._letmask[k] = m
        # "快速连点"字样模板(键帽右侧):用于区分连点态 vs 离散 QTE/等待态
        rp = TPL / "qte_text.png"
        self._rapid_tpl = pp_gray(cv2.imread(str(rp))) if rp.exists() else None
        # 离散 QTE 大按钮字母参考掩膜(A/S/W/D,从真实失败/超时帧裁切)
        for k in ("A", "D", "W", "S"):
            mp = TPL / f"qte_disc_{k}_mask.png"
            if mp.exists():
                m = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
                if m is not None:
                    self._disc_mask[k] = m
        # "挣扎圈鱼形图标"拒识参考(F0..):字母帽 IoU 会与之撞形,加入比较,鱼形胜出即丢弃
        for k in ("F0", "F1", "F2", "F3"):
            mp = TPL / f"qte_disc_{k}_mask.png"
            if mp.exists():
                m = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
                if m is not None:
                    self._disc_fish.append(m)

    # ---- 快速专用检测(热路径)----
    def is_hook(self, frame: np.ndarray) -> bool:
        """快速判上钩:中央"上钩啦"横幅 或 右下按钮变"拉杆"任一命中。
        窗口短(~0.3-0.5s),不拉即回常态,故双信号 + 低延迟。"""
        f = self.bank.norm(frame)
        if self.bank.score("banner", f, normalized=True) >= self.BANNER_HOOK:
            return True
        s_hook = self.bank.score("hook", f, normalized=True)
        s_wait = self.bank.score("wait", f, normalized=True)
        s_ready = self.bank.score("ready", f, normalized=True)
        # "拉杆"按钮须明显胜过取消/抛竿(图标相近,需大间隔防误触)
        return s_hook >= 0.6 and s_hook >= max(s_wait, s_ready) + 0.10

    def is_success(self, frame: np.ndarray) -> bool:
        """成功=奖励区(左侧中偏下)出现渔获:含『鱼』或『×1/x1/*1』。OCR,仅结算窗口内调用。"""
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
        """判是否进入等待咬钩(取消按钮),用于确认抛竿成功。"""
        return self.bank.hit("wait", frame)

    @staticmethod
    def _ocr_text(sub: np.ndarray, min_conf: float = 0.5) -> str:
        """OCR 该区域文字拼接(过滤低置信结果,降低水面纹理/反光的误识)。"""
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
        """落杆错误提示(顶部中央白字横幅)→ OCR 关键字:
          'too_close'(落点位置过近)/'too_far'(超出落杆范围)/
          'not_water'(落点不在水面)/'shallow'(当前水域深度不足)/None。"""
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
        """"已达到等级上限"弹窗(屏幕中央)。命中=按计划停机(脚本不处理升级)。"""
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = ROI_LEVELCAP
        t = self._ocr_text(frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)])
        return ("等级上限" in t) if t else False

    def is_record_screen(self, frame: np.ndarray) -> bool:
        """判"个人记录/渔获详情"界面:以右下"F 放入背包"按钮为准(普通钓鱼不出现)。"""
        return self.bank.hit("fbag", frame)

    def qte_prompt(self, frame: np.ndarray) -> tuple[str | None, bool]:
        """特殊收线提示识别,返回 (key, rapid):
          key   = 当前方向键 'A'/'D'(W/S 暂无字形→None);无键帽则 None。
          rapid = 键帽右侧是否有"快速连点"字样。
        rapid=True → 连续点按态(对该方向高频连点);rapid=False 但有 key → 离散 QTE
        (每个按钮只点一次、等下一个出现);二者都无 → 等待/无提示。
        先定位亮白键帽方块,字形 IoU 取键(A 尖顶 / D 右弧),再查键帽右侧同高带的连点字样。"""
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
        """键帽同高带(左右两侧)是否有"快速连点"字样。
        布局随方向镜像:A 为『[A] 快速连点』(字在键帽右)、D 为『快速连点 [D]』(字在键帽左),
        故横向取键帽两侧约 7×键帽高的带宽一起搜。文字高 ≈ 2.1×键帽高。"""
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
        base = 2.1 * hh / self._rapid_tpl.shape[0]      # 文字高 ≈ 2.1×键帽高
        return match_scales(region, self._rapid_tpl, (base * 0.8, base, base * 1.2)) >= self.RAPID_TH

    def qte_key(self, frame: np.ndarray) -> str | None:
        """仅返回连点小键帽方向键(兼容旧调用)。"""
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
        """离散 QTE 大按钮(圆形按钮 + 亮白字母 A/S/W/D,激活态亮环/失败超时态深灰,字母不变)。
        返回应按的键,无则 None。识别 = HoughCircles 定位圆形按钮(锚点,排除散点反光/连点小键帽)
        + 圆心亮字母字形 IoU 命中某字母(并须胜过"挣扎圈鱼形"拒识参考,排除水中央鱼形图标)。
        失败/超时按钮与正常按钮同形,均可识别。"""
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
        """快速判右下按钮:'ready'/'wait'/'hook'/'none'。供状态机热路径。"""
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
