"""HOKWord 剧情识别 v2 —— BetterGI AutoSkip 式「正向门」(看真实剧情视频重写)。

旧版重大 bug:把「没有游戏 HUD」当成「在剧情里」→ 切到日常/活动面板(同样没 HUD)被乱点
(光标飞到周活跃宝箱连点)。根因:用"否定信号"(无 HUD)当剧情判据,而菜单也满足。

新设计(参考 better-genshin-impact 的 Bv.IsInTalkUi:模板匹配一个剧情态专有 UI 件):
**只在 positively 识别到剧情 UI 时才动作,其它一律不动。** 王者剧情态的通用指纹 = 右上控制簇里的
**[F9] 抓拍**(看 SRC/剧情/*.mp4 抽帧:可跳过 [F9]抓拍[Esc]跳过 / 不可跳过 不可跳过[F9]抓拍 /
自动对话 [F9]抓拍(F5)自动 / 对话选项 全有它;普通游戏/菜单/日常面板全没有)。模板在 1920 归一化下
F9 键帽:剧情帧≈0.99、游戏/菜单≤0.74,分离极大 → 当门最稳(键帽是不透明灰框,背景无关)。

**为什么对菜单安全**:菜单/游戏没有 F9 抓拍 → 不过门 → 落到 idle 不动。唯一在"非剧情"时会动的是
**黑屏过场**(中三分之一近黑 0.6~0.985,仿 BetterGI ClickBlackGameScreen)——菜单/游戏达不到近黑。

**门内优雅降级(关键稳健性)**:进了剧情门后,任何细分误判都不致命——因为剧情里"点一下"都只会
推进剧情(不会像菜单那样误触功能)。故:可跳过→ESC(快),否则→右侧有可点文字就点最上一条
(真选项=点第一项,旁白右侧余字=也只是推进),没有就点中性点推进。再见/退出 选项→交还用户。

识别:门/可跳过/确认框 走模板(fishing/template_bank,多尺度但因同为 1920 故贴近 1.0);
选项只在"门内且不可跳过"时 OCR 一次右侧区(省 OCR、且菜单已被门挡在外)。
模板从真实 4K 剧情帧裁切,存 story/templates/raw,阈值由 story/replay_test.py 在 5 段视频标定。

**两步、按需 OCR**:classify 纯模板(快,~15-35ms)只给粗状态;只有"门内非可跳过"时 skipper 才
限频调 read_options(OCR ~150ms)查选项。好处:可跳过/游戏/菜单全程零 OCR → 循环飞快、推进跟手。
**只在 positively 在剧情时动作**:黑屏过场/菜单/游戏一律 idle 不动(不再点黑屏、不再鼠标微动)——
这样"剧情结束/段间黑屏"会立即停手、不会误点成攻击,再靠 F9 门快速重判是否进入下一段剧情。

classify(frame) → (state, pt):
  confirm 确认框金「跳过」在 → 点它(pt)。**独立于门**(确认框会盖住右上抓拍)。
  gate    模板预筛过门(也许在剧情)→ skipper 限频调 read_bar(OCR)精判 skip/story/none 再决定动作。
  idle    其它(游戏 / 菜单 / 日常面板 / 黑屏过场 / 加载)→ **不动作**。
read_bar(frame) → "skip"|"story"|"none":门内 OCR 精判(可跳过→ESC / 非可跳过→推进 / 假阳→不动)。
read_options(frame) → ("hold"|"choice"|"none", pt):门内非可跳过时怎么推进(见方法注释)。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from fishing.matcher import _get_ocr
from fishing.template_bank import PREPROCESS, TemplateBank, crop

HERE = Path(__file__).resolve().parent


def _res(*parts) -> Path:
    """资源路径:发布版 paths.resource_path(冻结后指向解包目录);开发版退回源码相对。"""
    try:
        from paths import resource_path
        return resource_path("story", *parts)
    except Exception:
        return HERE.joinpath(*parts)


TPL = _res("templates", "raw")

# ROI 归一化 (x0,y0,x1,y1)
ROI_TR = (0.78, 0.0, 1.0, 0.085)        # 右上控制簇:[F9]抓拍 / [Esc]跳过 / (F5)自动 / 不可跳过
ROI_CONFIRM = (0.40, 0.58, 0.80, 0.84)  # 确认框金「跳过」按钮区(是否跳过本段剧情)
ROI_DLG_TITLE = (0.30, 0.26, 0.70, 0.46)  # 确认框标题「是否跳过本段剧情」(OCR 复核,防菜单金按钮误触发 confirm)
ROI_OPT_TEXT = (0.63, 0.34, 0.98, 0.80) # 对话选项文字区(右侧竖排,**上半**);下边 0.80 刻意避开底部居中字幕
                                        # → 旁白字幕(y≈0.87)不算选项 → 走快速中性点推进;真选项(y≈0.4~0.75)才点

# 区域截图(skipper 用 cap.grab_region_canvas 只截这些小块贴 1920 画布,代替整帧 4K 抓取+缩放):
# 每块 = 覆盖对应 ROI 并四周各留 ~1% 余量(吸收 round(贴图)vs int(裁切)的取整差)。
# 每 tick 截 REGION_TR + REGION_CONFIRM(合计 ~14% 面积);OCR 限频时按需补 REGION_OPT / REGION_DLG_TITLE。
# 等价性由 story/_region_validate.py 在 5 段 4K 剧情视频上离线验证(状态/分数/点击点双路径一致)。
REGION_TR = (0.77, 0.0, 1.0, 0.10)            # 覆盖 ROI_TR(classify 门 + read_bar)
REGION_CONFIRM = (0.39, 0.57, 0.81, 0.85)     # 覆盖 ROI_CONFIRM(classify 确认框)
REGION_DLG_TITLE = (0.29, 0.25, 0.71, 0.47)   # 覆盖 ROI_DLG_TITLE(is_skip_dialog 复核)
REGION_OPT = (0.62, 0.33, 0.99, 0.81)         # 覆盖 ROI_OPT_TEXT(read_options)

# 多尺度集:模板与实时帧都归一化到 1920 宽 → 贴近 1.0,留 ±5% 容不同分辨率/游戏内 UI 缩放的渲染差
# (5 尺度 ±10% → 3 尺度 ±5%:replay_test 132 帧逐帧状态与 5 尺度完全一致、正例分数不变——最佳尺度
# 本就落在 0.95~1.05 内;每 tick 模板匹配次数 -40%。更宽会在均匀区误相关,禁用)
SCALES = (0.95, 1.0, 1.05)

# 阈值(story/replay_test.py 在 5 段真实视频标定)
GATE_LO = 0.60      # 模板**预筛**门(廉价):max(F9,Esc)≥此 = "也许在剧情",才去 OCR 精判。键帽/抓拍字在本作是
                    # **半透明**(背景透上来)→ 模板分随背景 0.67~1.0 飘、okww 二值/白掩膜反而更差(实测),故模板只当粗筛;
                    # 真机:剧情≥0.67、游戏≤0.60。精确判定走 read_bar(OCR 抓拍/跳过,背景无关,MaaNTE/okww 文字识别那一套)。
TH_ESC = 0.90       # Esc 键帽注册占位(**勿作判定!**)。2026-07 真机证伪:键帽半透明,暗场景真跳过帧
                    # 长期 <0.90、无 Esc 的帧又能到 0.86 → 分布重叠,模板分辨不出"键帽在/不在",
                    # 曾用它做 ESC 双因子把第一段话拦了 8s。可跳过/不可跳过一律由 read_bar OCR 精判
TH_CONFIRM = 0.75   # 确认框金「跳过」:确认框 0.95~1.0、其它≤0.49 → 0.75 极稳
MIN_CONF = 0.45     # OCR 置信度过滤
MIN_OPT_LEN = 2     # 选项文字最短字数

EXIT_WORDS = ("再见", "退出", "离开", "结束对话")  # 选项含此类 → 交还用户手动


class StoryRecognizer:
    """剧情识别:F9 正向门 + 模板细分 + (门内)OCR 选项。menus/游戏一律挡在门外。"""

    def __init__(self) -> None:
        self.bank = TemplateBank(TPL)
        reg = {
            "kc_f9": ("kc_f9.png", ROI_TR, GATE_LO),   # 注册阈值仅占位;classify 用 max(f9,esc)≥GATE_LO 自判
            "kc_esc": ("kc_esc.png", ROI_TR, TH_ESC),
            "confirm_skip": ("confirm_skip.png", ROI_CONFIRM, TH_CONFIRM),
        }
        self.ready = all((TPL / f).exists() for f, *_ in reg.values())
        if self.ready:
            for n, (f, roi, th) in reg.items():
                self.bank.register(n, f, roi, th, pre="gray", scales=SCALES)

    # ---- 模板分 / 定位 ----
    def _score(self, name: str, f_norm: np.ndarray) -> float:
        return self.bank.score(name, f_norm, normalized=True)

    def _locate(self, name: str, f_norm: np.ndarray):
        """返回 (best_score, cx_norm, cy_norm):模板 ROI 内多尺度匹配,给出最佳点中心。"""
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

    # ---- OCR:门内非可跳过时,读右侧可点文字(选项/旁白余字),返回从上到下 ----
    def _right_lines(self, f_norm: np.ndarray):
        """[(text, cx_norm, cy_norm), ...] 按 y 从上到下。用于"点最上一条"推进/选项。"""
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
        """**纯模板、快(无 OCR ~15-35ms)**,把当前帧归到一个粗状态 + 点击点。
        故意不在这里点黑屏/不微动:任何"非 positively 在剧情"的状态都返回 idle=不动
        (修复"切活动/日常面板光标闪烁"与"剧情结束/黑屏过场后误点=攻击"——见 skipper 注释)。
          confirm 确认框金「跳过」在 → 点它(pt)。独立于门(确认框遮住右上抓拍)。
          gate    模板预筛过门(也许在剧情)→ 交 skipper:限频 read_bar(OCR)精判 skip/story/none 再动作。
          idle    其它(游戏 / 菜单 / 日常面板 / 黑屏过场 / 加载)→ **不动作**。
        入参可以是整帧(replay 回放),也可以是 grab_region_canvas 的 1920 画布(实时,已归一化
        → norm() 直接原样返回,免掉整帧 4K 缩放;所有 ROI 都在画布已贴的区域内)。"""
        if not self.ready:
            return ("idle", None)
        f = self.bank.norm(frame)
        cs, cx, cy = self._locate("confirm_skip", f)
        if cs >= TH_CONFIRM:
            return ("confirm", (cx, cy))
        # 门=F9 抓拍 或 Esc 跳过(两者都是剧情专有键帽;Esc 独立判,不被 F9 漏配挡住)。
        # Esc 在 = 可跳过段(同时也是最强剧情信号)→ skip;否则 F9 在 = 非可跳过/自动对话 → story。
        # 模板只做廉价预筛:F9≥GATE_LO 或 Esc≥GATE_LO = "也许在剧情" → 返回 "gate",由 skipper 限频
        # OCR(read_bar)精判 skip/story/none(背景无关)。半透明 UI 下模板分会随背景飘,故不在这里下定论。
        # or 短路与 max(...)≥GATE_LO 判定等价;剧情帧 F9 几乎必过门 → 每 tick 省掉 Esc 的整轮多尺度匹配。
        if self._score("kc_f9", f) >= GATE_LO or self._score("kc_esc", f) >= GATE_LO:
            return ("gate", None)
        return ("idle", None)

    def read_bar(self, frame: np.ndarray) -> str:
        """**OCR 右上控制条(背景无关),剧情精判主力(MaaNTE/okww 文字识别那一套)**,由 skipper 限频调用:
          'skip'  有「跳过」且非「不可跳过」→ 可跳过 → 按 ESC(别再点击推进)。
          'story' 有 抓拍 / 自动 / 不可跳过(无「跳过」)→ 在剧情、非可跳过 → 点击推进 / 选项。
          'none'  右上没读到任何剧情控制字 → 不在剧情(模板预筛的假阳:菜单/游戏/过场)→ 不动作。"""
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
        # 剧情**专有**字才算 story:抓拍(F9,所有剧情态都在)/ 不可跳过(不可+跳过一起)。
        # **不认「自动」**(菜单有自动战斗/寻路);**「不可」也要带「跳过」**——否则菜单的不可用/不可领取/
        # 不可购买会被误判 story → 乱点 →"鼠标闪烁"。
        if ("抓拍" in txt) or ("不可" in txt and "跳过" in txt):
            return "story"
        return "none"

    def is_skip_dialog(self, frame: np.ndarray) -> bool:
        """OCR 复核确认框标题含「本段」(是否跳过**本段**剧情)。confirm_skip 模板是块**金按钮**,
        TM_CCOEFF 会在菜单里的其它金按钮(领取/确定/前往…)上误配 → 必须用标题字复核,菜单金按钮没有「本段」。"""
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
        """**慢(OCR ~150ms),由 skipper 限频调用**:门内非可跳过时读右侧可点文字决定怎么推进。
          ("hold", None)      选项含再见/退出 → 交还用户手动,别点。
          ("choice", (x,y))   有可点文字 → 点最上一条(真选项=默认第一项;旁白余字=也只是推进)。
          ("none", None)      右侧无文字 → 普通不可跳过对话 → 点中性点快速推进。"""
        f = self.bank.norm(frame)
        lines = self._right_lines(f)
        if not lines:
            return ("none", None)
        if any(any(x in t for x in EXIT_WORDS) for t, _, _ in lines):
            return ("hold", None)
        _, lx, ly = lines[0]
        return ("choice", (lx, ly))

    # ---- 标定/诊断 ----
    def scores(self, frame: np.ndarray) -> dict:
        if not self.ready:
            return {}
        f = self.bank.norm(frame)
        return {
            "f9": round(self._score("kc_f9", f), 3),
            "esc": round(self._score("kc_esc", f), 3),
            "confirm": round(self._locate("confirm_skip", f)[0], 3),
        }
