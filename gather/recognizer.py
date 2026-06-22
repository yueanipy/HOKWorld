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

import numpy as np

from fishing.matcher import _get_ocr
from fishing.template_bank import FAST_SCALES, TemplateBank, crop

HERE = Path(__file__).resolve().parent


def _res(*parts) -> Path:
    """资源路径:发布版用 paths.resource_path(冻结后指向解包目录);
    开发版无 paths.py,退回源码相对(gather 的上级即项目根)。两边都能定位模板/名单。"""
    try:
        from paths import resource_path
        return resource_path(*parts)
    except Exception:
        return HERE.parent.joinpath(*parts)


TPL = _res("fishing", "templates", "raw")   # 采集图标模板(F 键帽 / 手型 / 重现)

ROI_KC = (0.54, 0.46, 0.63, 0.63)     # F 键帽搜索区
ROI_ICON = (0.58, 0.46, 0.67, 0.63)   # 键帽右侧图标搜索区
ROI_TEXT = (0.53, 0.45, 0.82, 0.63)   # 提示文字区(仅手型图标时才 OCR,查碰撞名单)
PICK_F_TH = 0.80                       # F 键帽:有没有提示(实测无提示≤0.67、真提示≥0.95,留余量给真机差异)
ICON_TH = 0.72                         # 手型(采集)图标:真手型≥0.845、最高非手型 0.653,取中间
CX_TH = 0.68                           # 重现图标(重现=1.0、非重现≤0.52)
MIN_CONF = 0.45

# 手型图标碰撞名单「内置项」:写在代码里 → 随版本更新自动生效,无需用户手动加。
# 用户自己增删的名单存到「用户数据目录」的同名文件,覆盖更新 / 换机更新都保留(不被新版覆盖)。
_SEED_BLACKLIST = ("渡石", "语印", "捕获", "启动滑索", "松开滑索", "启动冲云翼")
_ICONS = ("pick_f.png", "icon_pick.png", "icon_chongxian.png")


def _user_dir() -> Path:
    """名单/配置所在的用户可写目录(%LOCALAPPDATA%\\<应用>)。
    发布版用 paths.user_data_dir,开发版用 config.user_data_dir;都取不到则退回本地 gather/。"""
    try:
        from paths import user_data_dir          # 发布版(HOKWorldScript / Release)
        return user_data_dir()
    except Exception:
        pass
    try:
        from config import user_data_dir          # 开发版(HOKWorld)
        return user_data_dir()
    except Exception:
        return HERE


def _list_file(name: str) -> Path:
    """名单的用户目录路径;首次运行用 gather/ 下的同名模板初始化(模板缺失则建空文件)。"""
    user = _user_dir() / name
    if not user.exists():
        try:
            user.parent.mkdir(parents=True, exist_ok=True)
            tpl = _res("gather", name)          # 随程序分发的名单模板(发布版在解包目录)
            user.write_text(tpl.read_text(encoding="utf-8") if tpl.exists() else "", encoding="utf-8")
        except Exception:
            pass
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
    except Exception:
        pass
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

    # —— 拆成「快(无 OCR)」+「慢(OCR)」两步,让采集只在必要时读字、且每个提示只读一次 ——
    def classify(self, frame: np.ndarray):
        """快路(无 OCR,~3ms):只用模板分判图标类型。返回 (kind, f_norm)。
        kind:none(无 F 提示/未标定)/ pick(手型)/ chongxian(重现)/ other(别的图标)。
        f_norm 复用给 read_name,省一次归一化。"""
        if not self.ready:
            return ("none", None)
        f = self.bank.norm(frame)
        if self.bank.score("pick_f", f, normalized=True) < PICK_F_TH:
            return ("none", f)
        if self.bank.score("icon_pick", f, normalized=True) >= ICON_TH:
            return ("pick", f)
        if self.bank.score("icon_chongxian", f, normalized=True) >= CX_TH:
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
            for b in self.blacklist:
                if b in name:
                    return (False, name, f"skip:{b}")
            return (True, name or "材料", "pick")
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
