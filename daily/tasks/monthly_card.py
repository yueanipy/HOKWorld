'月卡“汐月之礼”实时一次性检测。'
from __future__ import annotations

import json
import time
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Callable

import numpy as np

from daily import recognizer as rec
from runtime_guard import atomic_write_json, dev_log


TITLE_ROI = (0.38, 0.24, 0.62, 0.38)
REWARD_ROI = (0.43, 0.43, 0.57, 0.64)
HUD_RIGHT_ROI = (0.70, 0.02, 1.00, 0.96)
HUD_BOTTOM_ROI = (0.00, 0.82, 0.30, 1.00)
ROIS = (TITLE_ROI, REWARD_ROI, HUD_RIGHT_ROI, HUD_BOTTOM_ROI)
CLICK_POINT = (0.22, 0.58)  
ACTIVE_CHECK_INTERVAL = 1.0

TITLE_TEXT = "汐月之礼"
REWARD_TEXT = "晶珀"


def _compact(text: str) -> str:
    '去掉 OCR 可能插入的空白，不放宽关键字本身。'
    return "".join(str(text).split())


def read_monthly_card(frame: np.ndarray | None) -> tuple[bool, str, str]:
    '返回 (是否月卡, 标题区文字, 奖励区文字)。'
    if frame is None or frame.size == 0:
        return False, "", ""
    title = _compact(rec.ocr_text(frame, TITLE_ROI))
    if TITLE_TEXT not in title:
        return False, title, ""
    reward = _compact(rec.ocr_text(frame, REWARD_ROI))
    return REWARD_TEXT in reward, title, reward


def is_monthly_card(frame: np.ndarray | None) -> bool:
    '只返回月卡双关键字判定结果。'
    return read_monthly_card(frame)[0]


def read_world_hud(frame: np.ndarray | None) -> tuple[bool, tuple[str, ...]]:
    '正向确认普通角色 HUD；命中两个稳定边缘词才放行。'
    if frame is None or frame.size == 0:
        return False, ()
    hits = rec.world_hud_hits(frame)
    return len(hits) >= 2, hits


def _state_path() -> Path:
    try:
        from paths import user_data_dir
    except Exception:
        from config import user_data_dir
    return user_data_dir() / "monthly_card_state.json"


class MonthlyCardWatcher:
    '不创建独立线程；由实时检测循环按 due() 结果调用。'

    ROIS = ROIS

    def __init__(self, enabled: bool, log=print, state_path: Path | None = None,
                 date_provider: Callable[[], date] | None = None) -> None:
        self.enabled = bool(enabled)
        self.log = log
        self._path = Path(state_path) if state_path else _state_path()
        self._date_provider = date_provider or date.today
        self._done_date = ""
        self._clicked_pending_hud = False
        self._next_active_check = 0.0
        self._next_date_check = 0.0
        self._load_state()
        if self.done_today:
            self._schedule_midnight()

    @property
    def done_today(self) -> bool:
        return self._done_date == self._today()

    def due(self, now: float | None = None) -> bool:
        '到检测时刻才返回 True；当天完成后不再请求月卡区域截图。'
        if not self.enabled:
            return False
        now = time.monotonic() if now is None else now
        if self._done_date:
            if now < self._next_date_check:
                return False
            if self._done_date == self._today():
                self._schedule_midnight(now)
                return False
            self._done_date = ""
            self._save_state()
            self.log("每日奖励已刷新")
        if now < self._next_active_check:
            return False
        self._next_active_check = now + ACTIVE_CHECK_INTERVAL
        return True

    def detect(self, frame: np.ndarray | None) -> bool:
        return is_monthly_card(frame)

    def classify(self, frame: np.ndarray | None) -> tuple[str, tuple[str, ...]]:
        '返回 monthly / hud / pending；月卡标题命中时不额外做 HUD OCR。'
        if not self._clicked_pending_hud:
            monthly, _title, _reward = read_monthly_card(frame)
            if monthly:
                return "monthly", ()
        hud, hits = read_world_hud(frame)
        return ("hud", hits) if hud else ("pending", ())

    def mark_clicked(self) -> None:
        '点击后只等待 HUD，不重复识别或点击月卡浮层。'
        self._clicked_pending_hud = True

    def mark_done(self) -> None:
        self._done_date = self._today()
        self._schedule_midnight()
        self._save_state()

    def mark_hud_reached(self) -> None:
        '已经进入 HUD 说明本次登录无月卡或月卡已处理，当天不再识别。'
        self._clicked_pending_hud = False
        self.mark_done()

    def close(self) -> None:
        '兼容实时循环生命周期；OCR 引擎由进程共享，无独占资源。'

    def _today(self) -> str:
        return self._date_provider().isoformat()

    def _schedule_midnight(self, now_monotonic: float | None = None) -> None:
        '按本地时区计算下一次 0 点，等待期间不做日期轮询。'
        wall_now = datetime.now()
        next_day = wall_now.date() + timedelta(days=1)
        next_midnight = datetime.combine(next_day, datetime_time.min)
        delay = max(0.1, (next_midnight - wall_now).total_seconds())
        base = time.monotonic() if now_monotonic is None else now_monotonic
        self._next_date_check = base + delay

    def _load_state(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._done_date = str(data.get("done_date", ""))
        except Exception as exc:
            dev_log("月卡状态读取失败，本次按未完成处理", exc)
            self._done_date = ""

    def _save_state(self) -> None:
        try:
            atomic_write_json(self._path, {"done_date": self._done_date})
        except Exception as exc:
            dev_log("月卡状态保存失败", exc)
