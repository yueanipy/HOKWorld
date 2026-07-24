'HOKWord 实时采集识别(图标分类 + 文字兜底)。'
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from fishing.matcher import _get_ocr
from fishing.template_bank import (
    FAST_SCALES, TemplateBank, crop, match_prepared_scales, preprocess_box,
    preprocess_crop,
)
from runtime_guard import atomic_write_text, dev_log

HERE = Path(__file__).resolve().parent


def _res(*parts) -> Path:
    '资源路径:发布版用 paths.resourcepath(冻结后指向解包目录)。'
    try:
        from paths import resource_path
        return resource_path(*parts)
    except ModuleNotFoundError:
        return HERE.parent.joinpath(*parts)
    except Exception as exc:
        dev_log("paths.resource_path 不可用,退回源码路径", exc)
        return HERE.parent.joinpath(*parts)


TPL = _res("fishing", "templates", "raw")   

ROI_KC = (0.54, 0.46, 0.63, 0.63)     
ROI_ICON = (0.58, 0.46, 0.67, 0.63)   
ROI_TEXT = (0.53, 0.45, 0.82, 0.63)   


GATHER_REGION = (0.47, 0.39, 0.87, 0.71)


GATHER_FAST_REGION = (0.52, 0.43, 0.70, 0.67)
PICK_F_TH = 0.80                       
ICON_TH = 0.72                         
CX_TH = 0.68                           
MIN_CONF = 0.45



_SEED_BLACKLIST = (
    "渡石", "语印", "捕获", "启动滑索", "松开滑索", "启动冲云翼", "铸星",
    "牵引", "释放", "安置", "设置休息时间", "长按修复", "启动", "解锁牧场", "手动重置", "坐下",
    "顶层", "底层", "使用望远镜", "转动",
)
_ICONS = ("pick_f.png", "icon_pick.png", "icon_chongxian.png")


def _user_dir() -> Path:
    '名单/配置所在的用户可写目录(%LOCALAPPDATA%\\<应用>)。'
    try:
        from paths import user_data_dir          
        return user_data_dir()
    except ModuleNotFoundError:
        pass
    except Exception as exc:
        dev_log("paths.user_data_dir 不可用", exc)
    try:
        from config import user_data_dir          
        return user_data_dir()
    except Exception as exc:
        dev_log("config.user_data_dir 不可用,退回 gather 目录", exc)
        return HERE


def _list_file(name: str) -> Path:
    '名单的用户目录路径;首次运行用 gather/ 下的同名模板初始化(模板缺失则建空文件)。'
    user = _user_dir() / name
    if not user.exists():
        try:
            tpl = _res("gather", name)          
            atomic_write_text(user, tpl.read_text(encoding="utf-8") if tpl.exists() else "", encoding="utf-8")
        except Exception as exc:
            dev_log(f"名单初始化失败: {user}", exc)
    return user


def blacklist_file() -> Path:
    '碰撞名单(用户可编辑)文件路径——供「设置」编辑;采集运行时也读它。'
    return _list_file("blacklist.txt")


def whitelist_file() -> Path:
    '白名单(用户可编辑)文件路径。'
    return _list_file("whitelist.txt")


def _load_list(name: str, seed=()) -> list[str]:
    '从用户目录读名单(首次用模板初始化),并入内置 seed。'
    out: list[str] = []
    try:
        for line in _list_file(name).read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    except Exception as exc:
        dev_log(f"名单读取失败: {name}", exc)
    for s in seed:
        if s not in out:
            out.append(s)
    return out


class GatherRecognizer:
    '实时采集识别:F 键帽(有无提示)+ 图标分类(可采/重现/其它)+ 文字兜底(手型碰撞)。'

    def __init__(self) -> None:
        self.bank = TemplateBank(TPL)
        self.blacklist = _load_list("blacklist.txt", _SEED_BLACKLIST)   
        self.whitelist = _load_list("whitelist.txt")                     
        self.ready = all((TPL / f).exists() for f in _ICONS)
        if self.ready:
            self.bank.register("pick_f", "pick_f.png", ROI_KC, PICK_F_TH, pre="gray", scales=FAST_SCALES)
            self.bank.register("icon_pick", "icon_pick.png", ROI_ICON, ICON_TH, pre="gray", scales=FAST_SCALES)
            self.bank.register("icon_chongxian", "icon_chongxian.png", ROI_ICON, CX_TH, pre="gray", scales=FAST_SCALES)

    def scores(self, frame: np.ndarray) -> dict:
        '调试/标定用:三个模板分。'
        if not self.ready:
            return {}
        f = self.bank.norm(frame)
        return {k: round(self.bank.score(k, f, normalized=True), 3)
                for k in ("pick_f", "icon_pick", "icon_chongxian")}

    def _ocr_text(self, f_norm: np.ndarray) -> str:
        sub = crop(f_norm, ROI_TEXT)
        if sub.size == 0:
            return ""
        try:
            res, _ = _get_ocr()(sub)
        except Exception:
            return ""
        out = []
        for it in (res or []):
            txt, score = it[1], it[2]
            try:
                if float(score) >= MIN_CONF:
                    out.append(str(txt).strip())
            except (TypeError, ValueError):
                out.append(str(txt).strip())
        return " ".join(out).strip()

    def _locate_kc(self, f_norm: np.ndarray):
        '定位 F 键帽(激活提示的锚点)在归一化帧的最佳匹配 (score, x, y, w, h) 像素;无则 score=0。'
        t = self.bank._t["pick_f"]
        h, w = f_norm.shape[:2]
        x0, y0, _, _ = t.roi
        ox, oy = int(x0 * w), int(y0 * h)
        sub = preprocess_crop(f_norm, t.roi, t.pre)
        if sub is None or sub.size == 0 or not t.prepared:
            return (0.0, 0, 0, 0, 0)
        best = (0.0, 0, 0, 0, 0)
        for scaled_tpl, _ in t.prepared:
            th, tw = scaled_tpl.shape[:2]
            if th > sub.shape[0] or tw > sub.shape[1]:
                continue
            r = cv2.matchTemplate(sub, scaled_tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx > best[0]:
                best = (float(mx), ox + loc[0], oy + loc[1], tw, th)
        return best

    def _icon_score(self, name: str, preprocessed: np.ndarray) -> float:
        '在已预处理的当前激活提示图标框内匹配模板。'
        return match_prepared_scales(preprocessed, self.bank._t[name].prepared)

    
    def classify(self, frame: np.ndarray):
        '快路(无 OCR,~3ms):F 键帽锚定 + 图标分类。'
        if not self.ready:
            return ("none", None)
        f = self.bank.norm(frame)
        sc, kx, ky, kw, kh = self._locate_kc(f)
        if sc < PICK_F_TH:
            return ("none", f)
        
        
        h, w = f.shape[:2]
        pad = max(2, int(kh * 0.3))
        box = (int(ROI_ICON[0] * w), ky - pad, int(ROI_ICON[2] * w), ky + kh + pad)
        pick_tpl = self.bank._t["icon_pick"]
        chongxian_tpl = self.bank._t["icon_chongxian"]
        pick_sub = preprocess_box(f, box, pick_tpl.pre)
        if pick_sub.size == 0:
            return ("other", f)
        if self._icon_score("icon_pick", pick_sub) >= ICON_TH:
            return ("pick", f)
        chongxian_sub = (pick_sub if chongxian_tpl.pre == pick_tpl.pre
                         else preprocess_box(f, box, chongxian_tpl.pre))
        if self._icon_score("icon_chongxian", chongxian_sub) >= CX_TH:
            return ("chongxian", f)
        return ("other", f)

    def read_name(self, f_norm: np.ndarray) -> str:
        '慢路(OCR,~150ms):读提示文字。'
        return self._ocr_text(f_norm) if f_norm is not None else ""

    def judge(self, kind: str, name: str) -> tuple[bool, str, str]:
        '图标类型 + 文字 + 白/黑名单 → (按 F, 文字, 原因)。'
        for w in self.whitelist:
            if w and w in name:
                return (True, name or "材料", f"white:{w}")
        if kind == "chongxian":
            return (True, "重现", "chongxian")
        if kind == "pick":
            if not name:
                return (False, "", "no-text")         
                
            for b in self.blacklist:
                if b in name:
                    return (False, name, f"skip:{b}")
            return (True, name, "pick")
        return (False, name, "other-icon")           

    def decide(self, frame: np.ndarray) -> tuple[bool, str, str]:
        '整合一步(诊断/自测用):classify → 必要时 OCR → judge。'
        if not self.ready:
            return (False, "", "未标定")
        kind, f = self.classify(frame)
        if kind == "none":
            return (False, "", "no-prompt")
        if kind == "chongxian" and not self.whitelist:
            return (True, "重现", "chongxian")        
        return self.judge(kind, self.read_name(f))
