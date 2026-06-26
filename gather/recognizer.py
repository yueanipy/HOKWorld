"""HOKWord 实时采集识别(图标分类 + 文字兜底)。

王者的 F 提示键帽对所有交互**完全一样**,但**键帽右侧的图标按交互类型不同**:
采集材料 / 开宝箱 = 白色「手」图标;重现 = 圆形图标;对话 NPC / 商店 / 制作 / 剧情 等
各有各的图标。于是改成**按图标分类**(比按文字维护黑名单省事得多,新材料天然都用同一手型图标):

  正向门 = 检测到 F 键帽(屏幕中右锚点)→ 确有 F 交互提示;
  看键帽右侧图标:
    · 手型(采集)图标 → 可采(材料/宝箱)→ 按 F;
    · 重现图标 → 按 F;
    · 其它图标(NPC/商店/制作/对话/剧情…)→ 跳过(无需逐个文字登记)。
  唯一例外:**个别交互的图标与采集手型重合**(如「渡石手」),光看图标会误判 →
  对手型图标的提示再 OCR 文字,命中 gather/blacklist.txt 里的碰撞名单则跳过。

图标/键帽模板用 4K 录像标定:实测手型在 材料/宝箱/渡石 ≥0.85、在 NPC/商店/制作/重现 ≤0.52;
重现图标在重现 =1.0、其余 ≤0.52,分离度极大。复用 fishing 的 TemplateBank + OCR 单例 +
1920 归一化,任意 16:9 分辨率通用。碰撞名单见 gather/blacklist.txt(见到误按就加一行)。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from fishing.matcher import _get_ocr
from fishing.template_bank import FAST_SCALES, PREPROCESS, TemplateBank, crop, match_scales
from runtime_guard import atomic_write_text, dev_log

HERE = Path(__file__).resolve().parent


def _res(*parts) -> Path:
    """资源路径:发布版用 paths.resource_path(冻结后指向解包目录);
    开发版无 paths.py,退回源码相对(gather 的上级即项目根)。两边都能定位模板/名单。"""
    try:
        from paths import resource_path
        return resource_path(*parts)
    except Exception as exc:
        dev_log("paths.resource_path 不可用,退回源码路径", exc)
        return HERE.parent.joinpath(*parts)


TPL = _res("fishing", "templates", "raw")   # 采集图标模板(F 键帽 / 手型 / 重现)

ROI_KC = (0.54, 0.46, 0.63, 0.63)     # F 键帽搜索区
ROI_ICON = (0.58, 0.46, 0.67, 0.63)   # 键帽右侧图标搜索区
ROI_TEXT = (0.53, 0.45, 0.82, 0.63)   # 提示文字区(仅手型图标时才 OCR,查碰撞名单)
# 采集只看屏幕中部这一小块(覆盖上面所有 ROI + 键帽行锚定余量)。picker 用 cap.grab_region_canvas(GATHER_REGION)
# 只截这块(~10% 面积)→ 截图从 4K 全屏 ~60ms 降到 ~7ms,采集识别快一个数量级。改这里务必同步放大覆盖范围。
GATHER_REGION = (0.47, 0.39, 0.87, 0.71)
PICK_F_TH = 0.80                       # F 键帽:有没有提示(实测无提示≤0.67、真提示≥0.95,留余量给真机差异)
ICON_TH = 0.72                         # 手型(采集)图标:真手型≥0.845、最高非手型 0.653,取中间
CX_TH = 0.68                           # 重现图标(重现=1.0、非重现≤0.52)
MIN_CONF = 0.45

# 手型图标碰撞名单「内置项」:写在代码里 → 随版本更新自动生效,无需用户手动加。
# 用户自己增删的名单存到「用户数据目录」的同名文件,覆盖更新 / 换机更新都保留(不被新版覆盖)。
_SEED_BLACKLIST = ("渡石", "语印", "捕获", "启动滑索", "松开滑索", "启动冲云翼", "铸星")
_ICONS = ("pick_f.png", "icon_pick.png", "icon_chongxian.png")


def _user_dir() -> Path:
    """名单/配置所在的用户可写目录(%LOCALAPPDATA%\\<应用>)。
    发布版用 paths.user_data_dir,开发版用 config.user_data_dir;都取不到则退回本地 gather/。"""
    try:
        from paths import user_data_dir          # 发布版(HOKWorldScript / Release)
        return user_data_dir()
    except Exception as exc:
        dev_log("paths.user_data_dir 不可用", exc)
    try:
        from config import user_data_dir          # 开发版(HOKWorld)
        return user_data_dir()
    except Exception as exc:
        dev_log("config.user_data_dir 不可用,退回 gather 目录", exc)
        return HERE


def _list_file(name: str) -> Path:
    """名单的用户目录路径;首次运行用 gather/ 下的同名模板初始化(模板缺失则建空文件)。"""
    user = _user_dir() / name
    if not user.exists():
        try:
            tpl = _res("gather", name)          # 随程序分发的名单模板(发布版在解包目录)
            atomic_write_text(user, tpl.read_text(encoding="utf-8") if tpl.exists() else "", encoding="utf-8")
        except Exception as exc:
            dev_log(f"名单初始化失败: {user}", exc)
    return user


def blacklist_file() -> Path:
    """碰撞名单(用户可编辑)文件路径——供「设置」编辑;采集运行时也读它。"""
    return _list_file("blacklist.txt")


def whitelist_file() -> Path:
    """白名单(用户可编辑)文件路径。"""
    return _list_file("whitelist.txt")


def _load_list(name: str, seed=()) -> list[str]:
    """从用户目录读名单(首次用模板初始化),并入内置 seed。用户增删随更新保留。"""
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
    """实时采集识别:F 键帽(有无提示)+ 图标分类(可采/重现/其它)+ 文字兜底(手型碰撞)。"""

    def __init__(self) -> None:
        self.bank = TemplateBank(TPL)
        self.blacklist = _load_list("blacklist.txt", _SEED_BLACKLIST)   # 手型碰撞:命中→跳
        self.whitelist = _load_list("whitelist.txt")                     # 强制采:命中→采(最高优先)
        self.ready = all((TPL / f).exists() for f in _ICONS)
        if self.ready:
            self.bank.register("pick_f", "pick_f.png", ROI_KC, PICK_F_TH, pre="gray", scales=FAST_SCALES)
            self.bank.register("icon_pick", "icon_pick.png", ROI_ICON, ICON_TH, pre="gray", scales=FAST_SCALES)
            self.bank.register("icon_chongxian", "icon_chongxian.png", ROI_ICON, CX_TH, pre="gray", scales=FAST_SCALES)

    def scores(self, frame: np.ndarray) -> dict:
        """调试/标定用:三个模板分。"""
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
        """定位 **F 键帽**(激活提示的锚点)在归一化帧的最佳匹配 (score, x, y, w, h) 像素;无则 score=0。
        多个 F 提示竖排叠在屏幕中央时,只有"激活那一条"带 F 键帽——以它为锚,才能只读激活提示的图标。"""
        t = self.bank._t["pick_f"]
        h, w = f_norm.shape[:2]
        x0, y0, _, _ = t.roi
        ox, oy = int(x0 * w), int(y0 * h)
        sub = PREPROCESS[t.pre](crop(f_norm, t.roi))
        tpl = t.tpl
        if sub is None or sub.size == 0 or tpl.size == 0:
            return (0.0, 0, 0, 0, 0)
        best = (0.0, 0, 0, 0, 0)
        for s in t.scales:
            th, tw = int(tpl.shape[0] * s), int(tpl.shape[1] * s)
            if th < 8 or tw < 8 or th > sub.shape[0] or tw > sub.shape[1]:
                continue
            r = cv2.matchTemplate(sub, cv2.resize(tpl, (tw, th)), cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx > best[0]:
                best = (float(mx), ox + loc[0], oy + loc[1], tw, th)
        return best

    def _icon_score(self, name: str, f_norm: np.ndarray, box) -> float:
        """在像素 box=(x0,y0,x1,y1) 内匹配某图标模板,返回最佳分(box 由 F 键帽锚出 → 只看激活提示那一行)。"""
        t = self.bank._t[name]
        h, w = f_norm.shape[:2]
        x0, y0, x1, y1 = box
        sub = f_norm[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
        if sub.size == 0:
            return 0.0
        return match_scales(PREPROCESS[t.pre](sub), t.tpl, t.scales)

    # —— 拆成「快(无 OCR)」+「慢(OCR)」两步,让采集只在必要时读字、且每个提示只读一次 ——
    def classify(self, frame: np.ndarray):
        """快路(无 OCR,~3ms):F 键帽锚定 + 图标分类。返回 (kind, f_norm)。
        kind:none(无 F 提示/未标定)/ pick(手型)/ chongxian(重现)/ other(别的图标)。
        **关键(修复竖排叠提示乱按 F)**:图标只读"激活提示(带 F 键帽)那一行"——按 F 永远作用于激活提示,
        故绝不能在固定大竖条里匹配到下面/中间那条可采图标(那条 F 够不着,会一直空摁)。f_norm 复用给 read_name。"""
        if not self.ready:
            return ("none", None)
        f = self.bank.norm(frame)
        sc, kx, ky, kw, kh = self._locate_kc(f)
        if sc < PICK_F_TH:
            return ("none", f)
        # 图标框:X 用原 ROI_ICON(已在真机标定、不动);**Y 锚到键帽那一行**(高度≈键帽,上下留 0.3 余量)。
        # 这样只读"激活提示(带 F 键帽)"的图标,不再在固定大竖条里误配到上下叠着的别条提示图标。
        h, w = f.shape[:2]
        pad = max(2, int(kh * 0.3))
        box = (int(ROI_ICON[0] * w), ky - pad, int(ROI_ICON[2] * w), ky + kh + pad)
        if self._icon_score("icon_pick", f, box) >= ICON_TH:
            return ("pick", f)
        if self._icon_score("icon_chongxian", f, box) >= CX_TH:
            return ("chongxian", f)
        return ("other", f)

    def read_name(self, f_norm: np.ndarray) -> str:
        """慢路(OCR,~150ms):读提示文字。传 classify 返回的 f_norm。"""
        return self._ocr_text(f_norm) if f_norm is not None else ""

    def judge(self, kind: str, name: str) -> tuple[bool, str, str]:
        """图标类型 + 文字 + 白/黑名单 → (按 F, 文字, 原因)。
        优先级:白名单(强制采) > 黑名单(碰撞跳) > 图标默认(手型/重现采、其它跳)。"""
        for w in self.whitelist:
            if w and w in name:
                return (True, name or "材料", f"white:{w}")
        if kind == "chongxian":
            return (True, "重现", "chongxian")
        if kind == "pick":
            if not name:
                return (False, "", "no-text")         # **名字没读清 → 绝不按**(可能是没读清的碰撞名单,如语印/渡石);
                #                                       交给上层下一帧重读:读到材料名才采、读到黑名单则跳
            for b in self.blacklist:
                if b in name:
                    return (False, name, f"skip:{b}")
            return (True, name, "pick")
        return (False, name, "other-icon")           # 别的图标(NPC/商店/制作/对话…)→ 跳

    def decide(self, frame: np.ndarray) -> tuple[bool, str, str]:
        """整合一步(诊断/自测用):classify → 必要时 OCR → judge。
        原因:未标定 / no-prompt / white:<词> / skip:<词> / pick / chongxian / other-icon。"""
        if not self.ready:
            return (False, "", "未标定")
        kind, f = self.classify(frame)
        if kind == "none":
            return (False, "", "no-prompt")
        if kind == "chongxian" and not self.whitelist:
            return (True, "重现", "chongxian")        # 重现图标唯一,无碰撞 → 免 OCR
        return self.judge(kind, self.read_name(f))
