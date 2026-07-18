'HOKWord 剧情识别 v2 —— 成熟自动化方案 AutoSkip 式「正向门」(看真实剧情视频重写)。'
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from fishing.matcher import _get_ocr
from fishing.template_bank import PREPROCESS, TemplateBank, crop

HERE = Path(__file__).resolve().parent


def _res(*parts) -> Path:
    '资源路径:发布版 paths.resourcepath(冻结后指向解包目录);开发版退回源码相对。'
    try:
        from paths import resource_path
        return resource_path("story", *parts)
    except Exception:
        return HERE.joinpath(*parts)


TPL = _res("templates", "raw")


ROI_TR = (0.78, 0.0, 1.0, 0.085)        
ROI_CONFIRM = (0.40, 0.58, 0.80, 0.84)  
ROI_DLG_TITLE = (0.30, 0.26, 0.70, 0.46)  
ROI_OPT_TEXT = (0.63, 0.34, 0.98, 0.80) 
                                        





REGION_TR = (0.77, 0.0, 1.0, 0.10)            
REGION_CONFIRM = (0.39, 0.57, 0.81, 0.85)     
REGION_DLG_TITLE = (0.29, 0.25, 0.71, 0.47)   
REGION_OPT = (0.62, 0.33, 0.99, 0.81)         




SCALES = (0.95, 1.0, 1.05)


GATE_LO = 0.60      
                    
                    
TH_ESC = 0.90       
                    
                    
TH_CONFIRM = 0.75   
MIN_CONF = 0.45     
MIN_OPT_LEN = 2     

EXIT_WORDS = ("再见", "退出", "离开", "结束对话")  


class StoryRecognizer:
    '剧情识别:F9 正向门 + 模板细分 + (门内)OCR 选项。'

    def __init__(self) -> None:
        self.bank = TemplateBank(TPL)
        reg = {
            "kc_f9": ("kc_f9.png", ROI_TR, GATE_LO),   
            "kc_esc": ("kc_esc.png", ROI_TR, TH_ESC),
            "confirm_skip": ("confirm_skip.png", ROI_CONFIRM, TH_CONFIRM),
        }
        self.ready = all((TPL / f).exists() for f, *_ in reg.values())
        if self.ready:
            for n, (f, roi, th) in reg.items():
                self.bank.register(n, f, roi, th, pre="gray", scales=SCALES)

    
    def _score(self, name: str, f_norm: np.ndarray) -> float:
        return self.bank.score(name, f_norm, normalized=True)

    def _locate(self, name: str, f_norm: np.ndarray):
        '返回 (bestscore, cxnorm, cynorm):模板 ROI 内多尺度匹配,给出最佳点中心。'
        t = self.bank._t[name]
        h, w = f_norm.shape[:2]
        x0, y0, _, _ = t.roi
        ox, oy = int(x0 * w), int(y0 * h)
        sub = PREPROCESS[t.pre](crop(f_norm, t.roi))
        tpl = t.tpl
        if sub is None or sub.size == 0 or tpl.size == 0:
            return (0.0, None, None)
        sh, sw = sub.shape[:2]
        best = (0.0, None, None)
        for s in t.scales:
            th, tw = int(tpl.shape[0] * s), int(tpl.shape[1] * s)
            if th < 8 or tw < 8 or th > sh or tw > sw:
                continue
            r = cv2.matchTemplate(sub, cv2.resize(tpl, (tw, th)), cv2.TM_CCOEFF_NORMED)
            _, mx, _, mxloc = cv2.minMaxLoc(r)
            if mx > best[0]:
                cx = (ox + mxloc[0] + tw / 2) / w
                cy = (oy + mxloc[1] + th / 2) / h
                best = (float(mx), float(cx), float(cy))
        return best

    
    def _right_lines(self, f_norm: np.ndarray):
        '[(text, cxnorm, cynorm), ...] 按 y 从上到下。'
        h, w = f_norm.shape[:2]
        x0, y0, x1, y1 = ROI_OPT_TEXT
        ox, oy = int(x0 * w), int(y0 * h)
        sub = crop(f_norm, ROI_OPT_TEXT)
        if sub.size == 0:
            return []
        try:
            res, _ = _get_ocr()(sub)
        except Exception:
            return []
        out = []
        for it in (res or []):
            box, txt, score = it[0], str(it[1]).strip(), it[2]
            try:
                if float(score) < MIN_CONF:
                    continue
            except (TypeError, ValueError):
                pass
            if len(txt) < MIN_OPT_LEN:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = (ox + sum(xs) / len(xs)) / w
            cy = (oy + sum(ys) / len(ys)) / h
            out.append((txt, float(cx), float(cy)))
        out.sort(key=lambda e: e[2])
        return out

    def classify(self, frame: np.ndarray):
        '纯模板、快(无 OCR ~15-35ms),把当前帧归到一个粗状态 + 点击点。'
        if not self.ready:
            return ("idle", None)
        f = self.bank.norm(frame)
        cs, cx, cy = self._locate("confirm_skip", f)
        if cs >= TH_CONFIRM:
            return ("confirm", (cx, cy))
        
        
        
        
        
        if self._score("kc_f9", f) >= GATE_LO or self._score("kc_esc", f) >= GATE_LO:
            return ("gate", None)
        return ("idle", None)

    def read_bar(self, frame: np.ndarray) -> str:
        'OCR 右上控制条(背景无关),剧情精判主力(同类脚本/同类脚本 文字识别那一套),由 skipper 限频调用:。'
        f = self.bank.norm(frame)
        sub = crop(f, ROI_TR)
        if sub.size == 0:
            return "none"
        try:
            res, _ = _get_ocr()(sub)
        except Exception:
            return "none"
        parts = []
        for it in (res or []):
            try:
                if float(it[2]) >= MIN_CONF:
                    parts.append(str(it[1]))
            except (TypeError, ValueError):
                parts.append(str(it[1]))
        txt = "".join(parts)
        if "跳过" in txt and "不可" not in txt:
            return "skip"
        
        
        
        if ("抓拍" in txt) or ("不可" in txt and "跳过" in txt):
            return "story"
        return "none"

    def is_skip_dialog(self, frame: np.ndarray) -> bool:
        'OCR 复核确认框标题含「本段」(是否跳过本段剧情)。'
        f = self.bank.norm(frame)
        sub = crop(f, ROI_DLG_TITLE)
        if sub.size == 0:
            return False
        try:
            res, _ = _get_ocr()(sub)
        except Exception:
            return False
        parts = []
        for it in (res or []):
            try:
                if float(it[2]) >= MIN_CONF:
                    parts.append(str(it[1]))
            except (TypeError, ValueError):
                parts.append(str(it[1]))
        txt = "".join(parts)
        return ("本段" in txt) or ("段剧情" in txt)

    def read_options(self, frame: np.ndarray):
        '慢(OCR ~150ms),由 skipper 限频调用:门内非可跳过时读右侧可点文字决定怎么推进。'
        f = self.bank.norm(frame)
        lines = self._right_lines(f)
        if not lines:
            return ("none", None)
        if any(any(x in t for x in EXIT_WORDS) for t, _, _ in lines):
            return ("hold", None)
        _, lx, ly = lines[0]
        return ("choice", (lx, ly))

    
    def scores(self, frame: np.ndarray) -> dict:
        if not self.ready:
            return {}
        f = self.bank.norm(frame)
        return {
            "f9": round(self._score("kc_f9", f), 3),
            "esc": round(self._score("kc_esc", f), 3),
            "confirm": round(self._locate("confirm_skip", f)[0], 3),
        }
