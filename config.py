"""用户配置:JSON 存于 %LOCALAPPDATA%\\HOKWorldScript\\config.json(覆盖更新 / 换机更新都保留)。

目前仅「时序抖动」。不再有「演练 / 真实输入」总开关——它们会**静默关掉游戏操作**(识别到却不按),
易被误当作脚本失灵;现在脚本一律真实操作。采集黑/白名单同样存用户目录,更新不丢。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from paths import config_path

DEFAULTS = {
    "timing_jitter": False,          # 时序/位移随机抖动(默认关闭)
    "game_path": "",                 # 《王者荣耀世界》启动器 exe 路径;留空=自动定位(注册表/开始菜单);自动找不到时手填
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

    def timing_jitter(self) -> bool:
        return bool(self._d.get("timing_jitter"))


# 进程内单例:UI 改设置后 save();各任务线程启动时读取一次。
cfg = Config()
