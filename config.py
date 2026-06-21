"""用户配置:JSON 存于 %LOCALAPPDATA%\\HOKWorldScript\\config.json。

默认安全配置:演练模式开启、真实输入关闭、时序抖动关闭——
首次运行只识别不发送任何键鼠,需在「设置」里显式开启「真实输入」后才会操作游戏。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from paths import config_path

DEFAULTS = {
    "dry_run": True,                 # 演练模式:只识别、不发送输入
    "real_input": False,             # 真实输入总开关(默认关闭)
    "timing_jitter": False,          # 时序/位移随机抖动(默认关闭)
}


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else config_path()
        self._d = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            return
        if isinstance(raw, dict):
            for k in DEFAULTS:
                if k in raw:
                    self._d[k] = raw[k]

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._d, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get(self, key: str):
        return self._d.get(key, DEFAULTS.get(key))

    def set(self, key: str, value, save: bool = True) -> None:
        self._d[key] = value
        if save:
            self.save()

    # ---- 语义助手 ----
    def inputs_armed(self) -> bool:
        """是否允许真实发送键鼠:真实输入开 且 演练模式关。"""
        return bool(self._d.get("real_input")) and not bool(self._d.get("dry_run"))

    def timing_jitter(self) -> bool:
        return bool(self._d.get("timing_jitter"))


# 进程内单例:UI 改设置后 save();各任务线程启动时读取一次。
cfg = Config()
