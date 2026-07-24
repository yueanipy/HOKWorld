'每日任务一条龙 —— 配置(仿 一条龙任务框架 的 apporder + apprunlist)。'
from __future__ import annotations

import json
from pathlib import Path



TASK_REGISTRY: dict[str, str] = {
    "farm": "农贸作物",
    "incubator": "培养箱",
    "dispatch": "宠物派遣",
    "pet_feeding": "宠物喂食",
    "photo": "拍照",
    "interest_like": "兴趣圈点赞",
    "friends": "好友家浇水",
    "playbook_claim": "朝闻道·领每日活跃度",
    "daily_fishing": "每日钓鱼",
    "cooking": "烹饪",
    "alchemy": "制药",
}

DEFAULT_ORDER = [
    "farm",
    "incubator",
    "dispatch",
    "pet_feeding",
    "photo",
    "interest_like",
    "friends",
    "daily_fishing",
    "cooking",
    "alchemy",
    "playbook_claim",
]
DEFAULT_ENABLED: set[str] = set()

DISPATCH_REGIONS: tuple[str, ...] = (
    "稷下学院", "秘禁之地", "稷下前山", "梦语湖", "界北", "彩云盆地",
    "观星群山", "织梦原", "奇门秘境", "春溪原", "龙旗谷", "西云落高地",
    "春溪古战场", "东云落高地",
)
DEFAULT_DISPATCH_REGIONS: tuple[str, str, str] = (
    "东云落高地", "春溪原", "彩云盆地",
)


def _config_path() -> Path:
    '用户数据目录下的 dailyonedragon.json(发布版 paths.userdatadir,dev config.userdatadir)。'
    try:
        from paths import user_data_dir
    except Exception:
        from config import user_data_dir
    return user_data_dir() / "daily_onedragon.json"


class DailyConfig:
    '一条龙配置读写(一条龙任务 风:order 可排、enabled 可开关、params 可调)。'

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _config_path()
        self._d = {"order": list(DEFAULT_ORDER), "enabled": list(DEFAULT_ENABLED), "params": {}}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                d = json.loads(self._path.read_text(encoding="utf-8"))
                
                order = [t for t in d.get("order", []) if t in TASK_REGISTRY]
                order += [t for t in DEFAULT_ORDER if t not in order]
                self._d["order"] = order
                enabled = [t for t in d.get("enabled", DEFAULT_ENABLED) if t in TASK_REGISTRY]
                self._d["enabled"] = enabled
                self._d["params"] = d.get("params", {})
        except Exception:
            pass   

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._d, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    
    @property
    def order(self) -> list[str]:
        return list(self._d["order"])

    def set_order(self, new_order: list[str]) -> None:
        '整体设置顺序(UI 拖拽后调用):滤掉未知、补齐遗漏,再存盘。'
        order = [t for t in new_order if t in TASK_REGISTRY]
        order += [t for t in DEFAULT_ORDER if t not in order]
        self._d["order"] = order
        self.save()

    def move_up(self, task_id: str) -> None:
        o = self._d["order"]
        i = o.index(task_id) if task_id in o else -1
        if i > 0:
            o[i - 1], o[i] = o[i], o[i - 1]
            self.save()

    def move_down(self, task_id: str) -> None:
        o = self._d["order"]
        i = o.index(task_id) if task_id in o else -1
        if 0 <= i < len(o) - 1:
            o[i + 1], o[i] = o[i], o[i + 1]
            self.save()

    
    def is_enabled(self, task_id: str) -> bool:
        return task_id in self._d["enabled"]

    def set_enabled(self, task_id: str, on: bool) -> None:
        en = set(self._d["enabled"])
        en.add(task_id) if on else en.discard(task_id)
        self._d["enabled"] = [t for t in self._d["order"] if t in en]
        self.save()

    
    def param(self, task_id: str, key: str, default=None):
        return self._d["params"].get(task_id, {}).get(key, default)

    def set_param(self, task_id: str, key: str, value) -> None:
        self._d["params"].setdefault(task_id, {})[key] = value
        self.save()

    def dispatch_regions(self) -> list[str]:
        '返回三个互不重复的宠物派遣地区。'
        raw = self.param("dispatch", "regions", DEFAULT_DISPATCH_REGIONS)
        values = raw if isinstance(raw, (list, tuple)) else ()
        selected: list[str] = []
        for name in (*values, *DEFAULT_DISPATCH_REGIONS, *DISPATCH_REGIONS):
            name = str(name)
            if name in DISPATCH_REGIONS and name not in selected:
                selected.append(name)
            if len(selected) >= 3:
                break
        return selected

    def set_dispatch_regions(self, regions) -> None:
        '保存三个互不重复的宠物派遣地区。'
        selected = [str(name) for name in regions]
        if (len(selected) != 3 or len(set(selected)) != 3
                or any(name not in DISPATCH_REGIONS for name in selected)):
            raise ValueError("宠物派遣地区必须是三个不同的有效地区")
        self.set_param("dispatch", "regions", selected)

    def run_list(self) -> list[str]:
        '最终要跑的任务(按 order,滤掉未启用)。'
        return [t for t in self._d["order"] if t in set(self._d["enabled"])]
