'用户配置：设置页开关与本地路径。'
from __future__ import annotations

import copy
import json
from pathlib import Path

from runtime_guard import atomic_write_json, dev_log

DEFAULTS = {
    "monthly_card_enabled": True,     
    "game_path": "",                  
    "daily_auto_launch_game": True,   
    "close_script_when_game_exits": False,  
    "auto_water_interval_minutes": 90, 
    "auto_water_close_game": False,    
    "auto_water_shutdown_hours": 0,    
}


def user_data_dir() -> Path:
    '返回发布版用户数据目录。'
    from paths import user_data_dir as paths_user_data_dir
    return paths_user_data_dir()


def _config_path() -> Path:
    return user_data_dir() / "config.json"


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else _config_path()
        self._d = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            dev_log(f"配置读取失败,使用默认值: {self._path}", exc)
            return
        if isinstance(raw, dict):
            for k in DEFAULTS:
                if k in raw:
                    self._d[k] = raw[k]

    def save(self) -> None:
        try:
            atomic_write_json(self._path, self._d)
        except Exception as exc:
            dev_log(f"配置保存失败: {self._path}", exc)

    def get(self, key: str):
        return self._d.get(key, DEFAULTS.get(key))

    def set(self, key: str, value, save: bool = True) -> None:
        self._d[key] = value
        if save:
            self.save()


cfg = Config()
