'自动战斗配置读取与校验。'
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from config import user_data_dir
from runtime_guard import atomic_write_json


KEYBOARD_VK = {
    **{chr(code).lower(): code for code in range(0x41, 0x5B)},
    **{str(number): 0x30 + number for number in range(10)},
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "space": 0x20,
    "tab": 0x09,
    "esc": 0x1B,
}
MOUSE_KEYS = {"mouse_left": "left", "mouse_right": "right", "mouse_middle": "middle"}
SUPPORTED_KEYS = tuple(KEYBOARD_VK) + tuple(MOUSE_KEYS)
WAIT_ACTION_KEY = "wait"
_INVALID_PROFILE_NAME_CHARS = frozenset('<>:"/\\|?*#')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
BUNDLED_PROFILE_DIR = (
    Path(__file__).resolve().parents[1] / "assets" / "combat" / "profiles"
)
BUNDLED_PROFILE_NAMES = ("主C_辅助示例.txt", "孙尚香.txt")

DEFAULT_PROFILE = {
    "version": 1,
    "name": "凯 + 西施示例",
    "target_lock_on_start": True,
    "loop": True,
    "reaction_recovery_ms": 350,
    "dodge_key": "shift",
    "dodge_recovery_ms": 450,
    "stun_window_ms": 2500,
    "skill_cooldowns": {
        "e": 10000,
        "q": 14000,
        "r": 18000,
        "g": 60000,
    },
    "skill_order": ["e", "q", "r", "g"],
    "basic_attack_interval_ms": 180,
    "initial_hero": "",
    "hero_order": ["凯", "西施"],
    "ultimate_durations_ms": {"凯": 15000, "西施": 2000},
    "switch_key": "tab",
    "rotation": [],
    "secondary_sequences": {},
    "ultimate_sequences": {},
}

DEFAULT_TEXT_PROFILE = """# 自动战斗完整示例：凯（主C）+ 西施（辅助）
#
# 阅读顺序：先看基础设置，再看下面三个流程段。
# 第一段是主C普通流程；第二段是主C大招流程；第三段是辅助流程。
# 方括号中的名字用于界面显示，也用于区分主C和辅助。
#
# 动作格式：按键,按住秒,按下后等待秒
# 例如 e,0.055,0.475 表示：按住E 0.055秒，从按下E开始计时，
# 到0.475秒时执行下一行动作。等待时间包含按键本身的按住时间。
# wait(0.3)只等待0.3秒，不发送任何按键。
#
# 脚本会自动处理中键锁定、X/Z反制和红光闪避。
# 辅助流程到期时会自动按Tab切入，流程完成后自动按Tab切回主C。
# T只写在主C大招流程中；选择“可直接大招”后，大招可用时才执行该流程。

# ==================== 注意事项 ====================
#
# 1. 大招识别一直在后台运行。
#    无论主C普通流程执行到哪一行，只要确认大招可用，就会临时执行
#    [大招:凯]；大招流程结束后，继续执行原来尚未完成的普通流程。
#
# 2. 不要在主C普通流程中直接写T。
#    普通流程中的T会无条件按下，不判断大招是否可用；需要自动判断时，
#    应把T和大招连招全部写在[大招:凯]下面。
#
# 3. X、Z和红光闪避的优先级高于普通技能。
#    它们可以在动作等待期间插入，处理完成后继续当前流程。
#
# 4. [辅助:西施]会自动完成“Tab切入→辅助连招→Tab切回”。
#    辅助流程内不需要再写Tab，否则可能切回错误角色。
#
# 5. 每行#后面的内容只是说明，脚本不会把注释当成按键或参数。
#    推荐保持“主C普通流程→主C大招流程→辅助流程”的排列顺序，便于阅读。

# ==================== 基础设置 ====================

# 方案：只用于下拉列表和日志显示，不影响技能逻辑。
方案: 凯 + 西施示例

# 大招持续秒：主C开大后15秒内不切辅助；辅助大招保护2秒。
大招持续秒: 主C=15,辅助=2

# 技能冷却：脚本按E、Q、R、G顺序检查；多个技能同时可用时优先左侧。
# E=10秒，Q=14秒，R=18秒，G=60秒。
技能冷却: e=10,q=14,r=18,g=60


# ==================== 第一段：主C普通流程 ====================

[凯]

# E起手，等待技能动作进入稳定阶段。
e,0.055,0.475

# E后闪避，再接两次普攻组成连续输出。
shift,0.055,0.135
mouse_left,0.045,0.225
mouse_left,0.045,0.425

# 释放Q；随后纯等待0.3秒，避免下一个动作覆盖Q。
q,0.055,0.205
wait(0.3)

# Q后再次释放E，并补一次普攻。
e,0.055,0.535
mouse_left,0.045,0.265

# 释放R；随后闪避并接一次普攻收尾。
r,0.055,0.505
shift,0.085,0.165
mouse_left,0.045,0.225


# ==================== 第二段：主C大招流程 ====================

[大招:凯]

# 大招识别为可用后按T，等待开场动作结束。
t,0.065,0.915

# 释放G唤灵；G按基础设置中的60秒冷却重新计时。
g,0.055,0.405

# 大招状态下释放E，再连续普攻八次。
e,0.055,0.475
mouse_left,0.045,0.215
mouse_left,0.045,0.215
mouse_left,0.045,0.245
mouse_left,0.045,0.225
mouse_left,0.045,0.265
mouse_left,0.045,0.265
mouse_left,0.045,0.265
mouse_left,0.045,0.265


# ==================== 第三段：辅助流程 ====================

[辅助:西施]

# 辅助技能冷却：只计算E、Q、R；最大冷却R=30秒决定下次切入时间。
技能冷却: e=10,q=14,r=30

# 脚本会先自动按Tab切入西施。
# 如果西施大招可用，脚本会先按T；不可用时直接执行下面的连招。

# 两次E接Q和R，完成第一组技能。
e,0.055,0.555
e,0.055,0.555
q,0.055,0.555
r,0.055,0.335

# R后再执行两次E和一次Q；结束后脚本自动按Tab切回凯。
e,0.055,0.555
e,0.055,0.555
q,0.055,0.575
"""

NEW_TEXT_PROFILE = """# 自动战斗新建方案
# 时间统一使用秒；动作格式为：按键,按住秒,按下后等待秒。
# wait(0.5)表示只等待0.5秒，不发送输入。
# 冷却写0表示尚未启用；填写动作后再改成角色实际冷却时间。
方案: 新建方案
# 大招持续期间禁止切换角色。
大招持续秒: 主C=15,辅助=2
# 主C技能冷却；同时可用时按从左到右的顺序选择。
技能冷却: e=0,q=0,r=0,g=0

# 第一段是主C普通流程。
[主C]

# 第二段是主C大招流程；启用时至少写入一次T。
[大招:主C]

# 第三段是唯一辅助流程；最大冷却时间决定再次切入间隔。
[辅助:辅助]
技能冷却: e=0,q=0,r=0
"""

class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class CombatAction:
    key: str
    hold_ms: int = 55
    wait_ms: int = 250

    @property
    def vk(self) -> int | None:
        return KEYBOARD_VK.get(self.key)

    @property
    def mouse_button(self) -> str | None:
        return MOUSE_KEYS.get(self.key)


@dataclass(frozen=True)
class CooldownSkill:
    key: str
    cooldown_ms: int


@dataclass(frozen=True)
class SecondarySequence:
    hero: str
    switch_after_ms: int
    actions: tuple[CombatAction, ...]
    skill_cooldowns: tuple[CooldownSkill, ...] = ()


@dataclass(frozen=True)
class CombatProfile:
    name: str
    target_lock_on_start: bool
    loop: bool
    reaction_recovery_ms: int
    dodge_key: str
    dodge_recovery_ms: int
    stun_window_ms: int
    skill_cooldowns: tuple[CooldownSkill, ...]
    basic_attack_interval_ms: int
    initial_hero: str
    hero_order: tuple[str, ...]
    ultimate_durations_ms: dict[str, int]
    switch_key: str
    rotation: tuple[CombatAction, ...]
    secondary_sequences: tuple[SecondarySequence, ...]
    ultimate_sequences: dict[str, tuple[CombatAction, ...]]

    @classmethod
    def load(cls, path: Path | None = None) -> "CombatProfile":
        if path is None:
            file_path = compile_text_profile()
        else:
            file_path = Path(path)
            if file_path.suffix.lower() == ".txt":
                file_path = compile_text_profile(file_path)
            else:
                ensure_default_profile(file_path)
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ProfileError(f"战斗配置读取失败: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProfileError("战斗配置根节点必须是对象")
        if int(raw.get("version", 0)) != 1:
            raise ProfileError("仅支持 version=1 的战斗配置")
        hero_order = _parse_heroes(
            raw.get("hero_order")
            or [str(raw.get("initial_hero") or "角色1").strip()]
        )

        initial_hero = hero_order[0]
        switch_key = str(raw.get("switch_key") or "tab").strip().lower()
        if switch_key not in SUPPORTED_KEYS:
            raise ProfileError(f"switch_key 不支持按键 {switch_key!r}")
        dodge_key = str(raw.get("dodge_key") or "shift").strip().lower()
        if dodge_key not in SUPPORTED_KEYS:
            raise ProfileError(f"dodge_key 不支持按键 {dodge_key!r}")
        ultimate_raw = raw.get("ultimate_sequences")
        if ultimate_raw is None and "ultimate_sequence" in raw:
            ultimate_raw = {initial_hero: raw.get("ultimate_sequence")}
        if ultimate_raw is None:
            ultimate_raw = {}
        if not isinstance(ultimate_raw, dict):
            raise ProfileError("ultimate_sequences 必须是对象")
        ultimate_sequences = {
            str(hero).strip(): _parse_actions(actions, f"ultimate_sequences.{hero}")
            for hero, actions in ultimate_raw.items()
        }
        unknown_ultimate_heroes = set(ultimate_sequences) - set(hero_order)
        if unknown_ultimate_heroes:
            raise ProfileError(
                "大招流程包含未配置英雄: "
                + ", ".join(sorted(unknown_ultimate_heroes))
            )
        secondary_ultimate_heroes = [
            hero
            for hero, actions in ultimate_sequences.items()
            if hero != initial_hero and actions
        ]
        if secondary_ultimate_heroes:
            raise ProfileError("只允许配置第一段主C的大招流程")
        main_ultimate = ultimate_sequences.get(initial_hero, ())
        if main_ultimate and not any(action.key == "t" for action in main_ultimate):
            raise ProfileError("主C大招流程必须包含 T")
        secondary_sequences = _parse_secondary_sequences(
            raw.get("secondary_sequences"),
            hero_order,
            initial_hero,
        )
        ultimate_durations_ms = _parse_hero_durations(
            raw.get("ultimate_durations_ms"),
            hero_order,
        )
        return cls(
            name=str(raw.get("name") or "未命名战斗方案").strip(),
            target_lock_on_start=bool(raw.get("target_lock_on_start", True)),
            loop=bool(raw.get("loop", True)),
            reaction_recovery_ms=_bounded_int(
                raw.get("reaction_recovery_ms", 350), 0, 5000, "reaction_recovery_ms"),
            dodge_key=dodge_key,
            dodge_recovery_ms=_bounded_int(
                raw.get("dodge_recovery_ms", 450), 100, 5000, "dodge_recovery_ms"),
            stun_window_ms=_bounded_int(
                raw.get("stun_window_ms", 2500), 100, 15000, "stun_window_ms"),
            skill_cooldowns=_parse_skill_cooldowns(
                raw.get("skill_cooldowns"),
                raw.get("skill_order"),
            ),
            basic_attack_interval_ms=_bounded_int(
                raw.get("basic_attack_interval_ms", 180),
                60,
                2000,
                "basic_attack_interval_ms",
            ),
            initial_hero=initial_hero,
            hero_order=hero_order,
            ultimate_durations_ms=ultimate_durations_ms,
            switch_key=switch_key,
            rotation=_parse_actions(raw.get("rotation"), "rotation"),
            secondary_sequences=secondary_sequences,
            ultimate_sequences=ultimate_sequences,
        )

    def ultimate_for(self, hero: str) -> tuple[CombatAction, ...]:
        return self.ultimate_sequences.get(hero, ())

    def ultimate_duration_ms(self, hero: str) -> int:
        return self.ultimate_durations_ms.get(hero, 2000)

    @property
    def has_ultimate_sequence(self) -> bool:
        return any(self.ultimate_sequences.values())

    @property
    def has_any_action(self) -> bool:
        return bool(
            self.rotation
            or self.has_ultimate_sequence
            or any(sequence.actions for sequence in self.secondary_sequences)
        )

    @property
    def role_summary(self) -> str:
        '返回配置实际解析出的主C和辅助，不依赖方案名称。'
        auxiliaries = [
            hero for hero in self.hero_order if hero != self.initial_hero
        ]
        if not auxiliaries:
            return f"主C：{self.initial_hero} · 单角色"
        return f"主C：{self.initial_hero} · 辅助：{'、'.join(auxiliaries)}"

    def rotation_action(self, key: str) -> CombatAction | None:
        return next((action for action in self.rotation if action.key == key), None)

    def rotation_segment(self, key: str) -> tuple[CombatAction, ...]:
        '返回指定技能第一次出现时的动作段。'
        segments = self.rotation_segments(key)
        return segments[0] if segments else ()

    def rotation_segments(self, key: str) -> tuple[tuple[CombatAction, ...], ...]:
        '返回技能动作段；wait 后的技能作为当前技能的明确衔接。'
        skill_keys = {skill.key for skill in self.skill_cooldowns}
        segments = []
        for start, start_action in enumerate(self.rotation):
            if start_action.key != key:
                continue
            segment = []
            for index, action in enumerate(self.rotation[start:], start=start):
                if index > start and action.key in skill_keys:
                    if not segment or segment[-1].key != WAIT_ACTION_KEY:
                        break
                segment.append(action)
            segments.append(tuple(segment))
        return tuple(segments)


def _seed_bundled_profiles(directory: Path) -> None:
    '把缺失的内置TXT方案复制到用户目录，不覆盖用户修改。'
    if not BUNDLED_PROFILE_DIR.is_dir():
        return
    from runtime_guard import atomic_write_text
    for name in BUNDLED_PROFILE_NAMES:
        source = BUNDLED_PROFILE_DIR / name
        target = directory / name
        if source.is_file() and not target.exists():
            atomic_write_text(target, source.read_text(encoding="utf-8"))


def profile_dir() -> Path:
    directory = user_data_dir() / "combat_profiles"
    directory.mkdir(parents=True, exist_ok=True)
    _migrate_profile_role_names(directory)
    _seed_bundled_profiles(directory)
    return directory


def _migrate_profile_role_names(directory: Path) -> None:
    '将旧角色定位名称迁移为主C和辅助。'
    for source in tuple(directory.iterdir()):
        if source.suffix.lower() not in {".txt", ".json"}:
            continue
        target_name = source.name.replace("主战", "主C").replace("副战", "辅助")
        if target_name == source.name:
            continue
        target = source.with_name(target_name)
        index = 2
        while target.exists():
            target = source.with_name(
                f"{Path(target_name).stem}{index}{source.suffix}"
            )
            index += 1
        source.replace(target)


def profile_path() -> Path:
    return profile_dir() / "主C_辅助示例.txt"


def compiled_profile_path(text_path: Path | None = None) -> Path:
    source = Path(text_path) if text_path else profile_path()
    return source.with_suffix(".json")


def ensure_default_profile(path: Path | None = None) -> Path:
    file_path = Path(path) if path else profile_path()
    if not file_path.exists():
        if file_path.suffix.lower() == ".txt":
            from runtime_guard import atomic_write_text
            atomic_write_text(file_path, DEFAULT_TEXT_PROFILE)
        else:
            atomic_write_json(file_path, DEFAULT_PROFILE)
    return file_path


def compile_text_profile(path: Path | None = None) -> Path:
    '将用户可编辑的 TXT 战斗方案转换成内部 JSON。'
    text_path = ensure_default_profile(Path(path) if path else profile_path())
    text = text_path.read_text(encoding="utf-8")
    cleaned = _remove_dodge_followup_section(
        _normalize_single_secondary(_remove_yolo_settings(text))
    )
    if cleaned != text:
        from runtime_guard import atomic_write_text
        atomic_write_text(text_path, cleaned)
    raw = parse_text_profile(cleaned)
    raw["source_txt"] = text_path.name
    raw["source_mtime_ns"] = text_path.stat().st_mtime_ns
    output = compiled_profile_path(text_path)
    try:
        existing = json.loads(output.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing = None
    if existing != raw:
        atomic_write_json(output, raw)
    return output


def _remove_yolo_settings(text: str) -> str:
    '移除曾写入用户方案的YOLO设置，恢复普通战斗配置。'
    result = []
    skipping_yolo_section = False
    setting_names = ("YOLO模型", "YOLO置信度", "YOLO确认帧")
    for line in text.splitlines():
        content = line.strip()
        if content.startswith("[") and content.endswith("]"):
            if content == "[YOLO响应]":
                skipping_yolo_section = True
                continue
            skipping_yolo_section = False
        if skipping_yolo_section:
            continue
        if any(
            content.startswith(f"{name}:") or content.startswith(f"{name}=")
            for name in setting_names
        ):
            continue
        result.append(line)
    return "\n".join(result).rstrip() + "\n"


def _remove_dodge_followup_section(text: str) -> str:
    '移除旧方案中的全局闪避后动作段。'
    result = []
    skipping = False
    for line in text.splitlines():
        content = line.strip()
        if content.startswith("[") and content.endswith("]"):
            if content == "[闪避后]":
                skipping = True
                continue
            skipping = False
        if not skipping:
            result.append(line)
    return "\n".join(result).rstrip() + "\n"


def _normalize_single_secondary(text: str) -> str:
    '兼容旧方案的辅助名称，并移除辅助独立大招段。'
    text = text.replace("副战1", "副战")
    main_hero = ""
    for line in text.splitlines():
        content = line.split("#", 1)[0].strip()
        if content.startswith("英雄顺序:") or content.startswith("英雄顺序="):
            separator = ":" if ":" in content else "="
            values = [
                value.strip()
                for value in content.split(separator, 1)[1].replace("，", ",").split(",")
                if value.strip()
            ]
            if values:
                main_hero = values[0]
            break
    if not main_hero:
        return text.rstrip() + "\n"

    result = []
    skipping_secondary_ultimate = False
    for line in text.splitlines():
        content = line.strip()
        if content.startswith("[") and content.endswith("]"):
            title = content[1:-1].strip()
            if title.startswith("大招:"):
                skipping_secondary_ultimate = title[3:].strip() != main_hero
                if skipping_secondary_ultimate:
                    continue
            else:
                skipping_secondary_ultimate = False
        if skipping_secondary_ultimate:
            continue
        result.append(line)
    return "\n".join(result).rstrip() + "\n"


def list_profile_paths() -> tuple[Path, ...]:
    '返回可选择的全部 TXT 战斗方案。'
    directory = profile_dir()
    paths = tuple(directory.glob("*.txt"))
    if not paths:
        ensure_default_profile(directory / "主C_辅助示例.txt")
        paths = tuple(directory.glob("*.txt"))
    return tuple(sorted(
        paths,
        key=lambda path: path.name.casefold(),
    ))


def delete_profile(path: Path) -> tuple[Path, ...]:
    '删除用户目录内同名的TXT方案和JSON运行缓存。'
    source = Path(path).resolve()
    directory = profile_dir().resolve()
    if source.suffix.lower() != ".txt" or source.parent != directory:
        raise ValueError("只能删除自动战斗方案目录内的TXT文件")
    if not source.exists():
        raise FileNotFoundError(f"方案不存在: {source.name}")

    removed = []
    compiled = source.with_suffix(".json")
    if compiled.exists():
        compiled.unlink()
        removed.append(compiled)
    source.unlink()
    removed.append(source)
    return tuple(removed)


def sync_profile_jsons(
    paths: tuple[Path, ...] | list[Path] | None = None,
) -> tuple[tuple[Path, ...], tuple[Path, ...], dict[str, str]]:
    '同步TXT与同名JSON，并删除没有对应TXT的JSON。'
    sources = tuple(paths) if paths is not None else list_profile_paths()
    compiled = []
    removed = []
    errors = {}
    directories = {source.parent.resolve() for source in sources}
    if not directories:
        directories = {profile_dir().resolve()}
    expected_json = {
        source.with_suffix(".json").resolve()
        for source in sources
    }
    for directory in directories:
        for json_path in directory.glob("*.json"):
            if json_path.resolve() in expected_json:
                continue
            try:
                json_path.unlink()
                removed.append(json_path)
            except OSError as exc:
                errors[json_path.name] = f"删除无关联JSON失败: {exc}"
    for source in sources:
        try:
            compiled.append(compile_text_profile(source))
        except Exception as exc:
            errors[source.name] = str(exc)
            stale_json = source.with_suffix(".json")
            if stale_json.exists():
                try:
                    stale_json.unlink()
                    removed.append(stale_json)
                except OSError as remove_exc:
                    errors[source.name] += (
                        f"；删除旧JSON失败: {remove_exc}"
                    )
    return tuple(compiled), tuple(removed), errors


def choose_profile_path(
    paths: tuple[Path, ...] | list[Path],
    preferred: str = "",
) -> Path | None:
    '选择方案；旧选择失效时优先使用最近修改的文件。'
    candidates = tuple(paths)
    if not candidates:
        return None
    by_name = {path.name: path for path in candidates}
    if preferred in by_name:
        return by_name[preferred]
    if preferred:
        return max(
            candidates,
            key=lambda path: (
                path.stat().st_mtime_ns,
                path.name.casefold(),
            ),
        )
    return by_name.get("主C_辅助示例.txt", candidates[0])


def duplicate_profile(source: Path | None = None) -> Path:
    '复制现有方案，生成可独立编辑的新 TXT 文件。'
    source_path = ensure_default_profile(Path(source) if source else profile_path())
    stem = f"{source_path.stem}_副本"
    destination = source_path.with_name(f"{stem}.txt")
    index = 2
    while destination.exists():
        destination = source_path.with_name(f"{stem}{index}.txt")
        index += 1
    from runtime_guard import atomic_write_text
    atomic_write_text(destination, source_path.read_text(encoding="utf-8"))
    return destination


def normalize_profile_name(name: str) -> str:
    '校验用户输入并返回不含扩展名的方案名。'
    stem = str(name or "").strip()
    if stem.lower().endswith(".txt"):
        stem = stem[:-4].rstrip()
    if not stem:
        raise ValueError("方案名不能为空")
    if stem in {".", ".."} or stem.endswith("."):
        raise ValueError("方案名不能以句点结尾")
    if any(char in _INVALID_PROFILE_NAME_CHARS for char in stem):
        raise ValueError("方案名不能包含 < > : \" / \\ | ? * #")
    if any(ord(char) < 32 for char in stem):
        raise ValueError("方案名不能包含控制字符")
    if stem.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("该名称是系统保留名称，请更换方案名")
    if len(stem) > 120:
        raise ValueError("方案名不能超过120个字符")
    return stem


def create_blank_profile(name: str | None = None) -> Path:
    '创建只含说明和三个流程段的新战斗方案。'
    directory = profile_dir()
    if name is None:
        destination = directory / "新建方案.txt"
        index = 2
        while destination.exists():
            destination = directory / f"新建方案{index}.txt"
            index += 1
    else:
        profile_name = normalize_profile_name(name)
        destination = directory / f"{profile_name}.txt"
        if destination.exists():
            raise FileExistsError(f"方案已存在：{destination.name}")
    from runtime_guard import atomic_write_text
    content = NEW_TEXT_PROFILE
    if name is not None:
        content = re.sub(
            r"(?m)^方案:\s*.*$",
            lambda _match: f"方案: {profile_name}",
            content,
            count=1,
        )
    atomic_write_text(destination, content)
    return destination


def parse_text_profile(text: str) -> dict:
    '解析中文 TXT 战斗方案。'
    raw = dict(DEFAULT_PROFILE)
    raw["rotation"] = []
    raw["secondary_sequences"] = {}
    raw["ultimate_sequences"] = {}
    raw["ultimate_durations_ms"] = {}
    raw["hero_order"] = []
    raw["skill_order"] = None
    section: tuple[str, str | None] | None = None
    section_number = 0
    positional_main = ""
    aliases = {
        "方案": "name",
        "锁定目标": "target_lock_on_start",
        "循环": "loop",
        "初始英雄": "initial_hero",
        "英雄顺序": "hero_order",
        "大招持续秒": "ultimate_durations_seconds",
        "切换按键": "switch_key",
        "反制恢复秒": "reaction_recovery_seconds",
        "闪避按键": "dodge_key",
        "闪避恢复秒": "dodge_recovery_seconds",
        "昏迷窗口秒": "stun_window_seconds",
        "技能冷却": "skill_cooldowns",
        "技能顺序": "skill_order",
        "普攻间隔秒": "basic_attack_interval_seconds",
        "反制恢复毫秒": "reaction_recovery_ms",
        "闪避恢复毫秒": "dodge_recovery_ms",
        "昏迷窗口毫秒": "stun_window_ms",
    }
    for line_number, original in enumerate(text.splitlines(), start=1):
        content = original.split("#", 1)[0].strip()
        if not content:
            continue
        if content.startswith("[") and content.endswith("]"):
            title = content[1:-1].strip()
            section_number += 1
            if title == "普通流程":
                section = ("rotation", None)
            elif title.startswith("辅助:") and title[3:].strip():
                hero = title[3:].strip()
                raw["secondary_sequences"].setdefault(
                    hero, {"switch_after_ms": 0, "actions": []})
                section = ("secondary", hero)
            elif title.startswith("副英雄:") and title[4:].strip():
                hero = title[4:].strip()
                raw["secondary_sequences"].setdefault(
                    hero, {"switch_after_ms": 0, "actions": []})
                section = ("secondary", hero)
            elif title.startswith("大招:") and title[3:].strip():
                hero = title[3:].strip()
                raw["ultimate_sequences"].setdefault(hero, [])
                section = ("ultimate", hero)
            elif section_number == 1:
                positional_main = title
                section = ("rotation", None)
            elif section_number == 2:
                hero = positional_main or "主C"
                raw["ultimate_sequences"].setdefault(hero, [])
                section = ("ultimate", hero)
            elif section_number == 3:
                hero = title
                raw["secondary_sequences"].setdefault(
                    hero, {"switch_after_ms": 0, "actions": []})
                section = ("secondary", hero)
            else:
                raise ProfileError(f"TXT 第 {line_number} 行是不支持的段落: {content}")
            continue
        if section is None:
            separator = ":" if ":" in content else "=" if "=" in content else None
            if separator is None:
                raise ProfileError(f"TXT 第 {line_number} 行缺少冒号")
            label, value = (part.strip() for part in content.split(separator, 1))
            key = aliases.get(label)
            if key is None:
                raise ProfileError(f"TXT 第 {line_number} 行是不支持的设置: {label}")
            if key in ("target_lock_on_start", "loop"):
                raw[key] = value.lower() in ("是", "true", "1", "开", "开启")
            elif key == "hero_order":
                raw[key] = [part.strip() for part in value.replace("，", ",").split(",")
                            if part.strip()]
            elif key == "ultimate_durations_seconds":
                raw["ultimate_durations_ms"] = _parse_text_hero_durations(
                    value, line_number)
            elif key == "reaction_recovery_seconds":
                raw["reaction_recovery_ms"] = _seconds_to_ms(
                    value, 0.0, 5.0, label)
            elif key == "dodge_recovery_seconds":
                raw["dodge_recovery_ms"] = _seconds_to_ms(
                    value, 0.1, 5.0, label)
            elif key == "stun_window_seconds":
                raw["stun_window_ms"] = _seconds_to_ms(
                    value, 0.1, 15.0, label)
            elif key == "basic_attack_interval_seconds":
                raw["basic_attack_interval_ms"] = _seconds_to_ms(
                    value, 0.06, 2.0, label)
            elif key == "skill_cooldowns":
                raw["skill_cooldowns"] = _parse_text_skill_cooldowns(
                    value, line_number)
            elif key == "skill_order":
                raw["skill_order"] = _parse_text_skill_order(
                    value, line_number)
            elif key in (
                "reaction_recovery_ms",
                "dodge_recovery_ms",
                "stun_window_ms",
            ):
                raw[key] = _bounded_int(
                    value,
                    0 if key == "reaction_recovery_ms" else 100,
                    15000 if key == "stun_window_ms" else 5000,
                    label,
                )
            else:
                raw[key] = value
            continue
        if section[0] == "secondary" and (
            content.startswith("切人秒:") or content.startswith("切人秒=")
        ):
            separator = ":" if ":" in content else "="
            _label, value = (part.strip() for part in content.split(separator, 1))
            raw["secondary_sequences"][section[1]]["switch_after_ms"] = _seconds_to_ms(
                value, 0.1, 600.0, f"{section[1]}切人时间")
            continue
        if section[0] == "secondary" and (
            content.startswith("技能冷却:") or content.startswith("技能冷却=")
        ):
            separator = ":" if ":" in content else "="
            _label, value = (part.strip() for part in content.split(separator, 1))
            raw["secondary_sequences"][section[1]]["skill_cooldowns"] = (
                _parse_text_skill_cooldowns(value, line_number)
            )
            continue
        wait_match = re.fullmatch(
            r"wait\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*\)",
            content,
            flags=re.IGNORECASE,
        )
        if wait_match:
            action = {
                "key": WAIT_ACTION_KEY,
                "hold_ms": 0,
                "wait_ms": _seconds_to_ms(
                    wait_match.group(1),
                    0.0,
                    60.0,
                    f"TXT 第 {line_number} 行等待时间",
                ),
            }
            if section[0] == "rotation":
                raw["rotation"].append(action)
            elif section[0] == "secondary":
                raw["secondary_sequences"][section[1]]["actions"].append(action)
            else:
                raw["ultimate_sequences"][section[1]].append(action)
            continue
        parts = [part.strip() for part in content.replace("，", ",").split(",")]
        if len(parts) != 3:
            raise ProfileError(
                f"TXT 第 {line_number} 行动作格式应为: 按键,按住秒,按下后等待秒")
        hold_ms, wait_ms = _parse_action_times(
            parts[1], parts[2], line_number)
        action = {
            "key": parts[0].lower(),
            "hold_ms": hold_ms,
            "wait_ms": wait_ms,
        }
        if section[0] == "rotation":
            raw["rotation"].append(action)
        elif section[0] == "secondary":
            raw["secondary_sequences"][section[1]]["actions"].append(action)
        else:
            raw["ultimate_sequences"][section[1]].append(action)
    if not raw["hero_order"]:
        secondary_heroes = list(raw["secondary_sequences"])
        if positional_main:
            main_hero = positional_main
        elif raw["ultimate_sequences"]:
            main_hero = next(iter(raw["ultimate_sequences"]))
        else:
            duration_heroes = [
                hero
                for hero in raw["ultimate_durations_ms"]
                if hero not in secondary_heroes
            ]
            main_hero = duration_heroes[0] if duration_heroes else "主C"
        raw["hero_order"] = [
            main_hero,
            *(hero for hero in secondary_heroes if hero != main_hero),
        ]
    return raw


def _parse_action_times(hold: str, wait: str, line_number: int) -> tuple[int, int]:
    '解析秒单位动作时间，并兼容旧版整数毫秒。'
    try:
        hold_number = float(hold)
        wait_number = float(wait)
    except ValueError as exc:
        raise ProfileError(f"TXT 第 {line_number} 行时间必须是数字") from exc
    legacy_milliseconds = hold_number > 5 or wait_number > 60
    if legacy_milliseconds:
        return (
            _bounded_int(round(hold_number), 10, 5000, f"TXT 第 {line_number} 行按住时间"),
            _bounded_int(round(wait_number), 0, 60000, f"TXT 第 {line_number} 行等待时间"),
        )
    return (
        _seconds_to_ms(hold_number, 0.01, 5.0, f"TXT 第 {line_number} 行按住时间"),
        _seconds_to_ms(wait_number, 0.0, 60.0, f"TXT 第 {line_number} 行等待时间"),
    )


def _parse_text_skill_cooldowns(value: str, line_number: int) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in value.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ProfileError(
                f"TXT 第 {line_number} 行技能冷却格式应为: e=10,q=14"
            )
        key, seconds = (part.strip().lower() for part in item.split("=", 1))
        if key not in KEYBOARD_VK:
            raise ProfileError(f"TXT 第 {line_number} 行技能按键不支持: {key}")
        result[key] = _seconds_to_ms(
            seconds, 0.0, 600.0, f"TXT 第 {line_number} 行 {key} 冷却"
        )
    if not result:
        raise ProfileError(f"TXT 第 {line_number} 行技能冷却不能为空")
    return result


def _parse_text_skill_order(value: str, line_number: int) -> list[str]:
    order = [
        item.strip().lower()
        for item in value.replace("，", ",").split(",")
        if item.strip()
    ]
    if not order:
        raise ProfileError(f"TXT 第 {line_number} 行技能顺序不能为空")
    if len(set(order)) != len(order):
        raise ProfileError(f"TXT 第 {line_number} 行技能顺序不能包含重复按键")
    unsupported = [key for key in order if key not in KEYBOARD_VK]
    if unsupported:
        raise ProfileError(
            f"TXT 第 {line_number} 行技能按键不支持: {unsupported[0]}"
        )
    return order


def _parse_text_hero_durations(value: str, line_number: int) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in value.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ProfileError(
                f"TXT 第 {line_number} 行大招持续格式应为: 主C=2,辅助=2"
            )
        hero, seconds = (part.strip() for part in item.split("=", 1))
        if not hero:
            raise ProfileError(f"TXT 第 {line_number} 行大招角色不能为空")
        result[hero] = _seconds_to_ms(
            seconds, 0.1, 30.0, f"TXT 第 {line_number} 行 {hero} 大招持续"
        )
    if not result:
        raise ProfileError(f"TXT 第 {line_number} 行大招持续不能为空")
    return result


def _parse_hero_durations(
    raw,
    hero_order: tuple[str, ...],
) -> dict[str, int]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProfileError("ultimate_durations_ms 必须是对象")
    normalized = {
        str(hero).strip(): value
        for hero, value in raw.items()
    }
    role_aliases = (
        (("主C", "主c", "主战"), 0),
        (("辅助", "副战", "副英雄"), 1),
    )
    for aliases, index in role_aliases:
        role_value = None
        for alias in aliases:
            if alias in normalized:
                value = normalized.pop(alias)
                if role_value is None:
                    role_value = value
        if role_value is not None and index < len(hero_order):
            normalized.setdefault(hero_order[index], role_value)
    unknown = set(normalized) - set(hero_order)
    if unknown:
        raise ProfileError(f"大招持续时间包含未配置英雄: {', '.join(sorted(unknown))}")
    return {
        hero: _bounded_int(
            normalized.get(hero, 2000),
            100,
            30000,
            f"ultimate_durations_ms.{hero}",
        )
        for hero in hero_order
    }


def _parse_skill_cooldowns(raw, configured_order=None) -> tuple[CooldownSkill, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ProfileError("skill_cooldowns 必须是对象")
    if len(raw) > 12:
        raise ProfileError("skill_cooldowns 最多允许 12 个技能")
    configured: dict[str, int] = {}
    for key, cooldown in raw.items():
        normalized_key = str(key).strip().lower()
        if normalized_key not in KEYBOARD_VK:
            raise ProfileError(f"技能冷却不支持按键 {normalized_key!r}")
        configured[normalized_key] = _bounded_int(
            cooldown,
            0,
            600000,
            f"skill_cooldowns.{normalized_key}",
        )
    normalized = {
        key: cooldown
        for key, cooldown in configured.items()
        if cooldown > 0
    }
    if configured_order is None:
        order = list(normalized)
    elif not isinstance(configured_order, list):
        raise ProfileError("skill_order 必须是数组")
    else:
        order = [str(key).strip().lower() for key in configured_order]
        if len(set(order)) != len(order):
            raise ProfileError("skill_order 不能包含重复按键")
        unknown = [key for key in order if key not in configured]
        if unknown:
            raise ProfileError(f"skill_order 包含未配置冷却的技能 {unknown[0]!r}")
        order = [key for key in order if key in normalized]
        order.extend(key for key in normalized if key not in order)
    return tuple(
        CooldownSkill(key, normalized[key])
        for key in order
    )


def _seconds_to_ms(value, minimum: float, maximum: float, label: str) -> int:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"{label}必须是秒数") from exc
    if not minimum <= seconds <= maximum:
        raise ProfileError(f"{label}必须在 {minimum}~{maximum} 秒之间")
    return int(round(seconds * 1000))


def _bounded_int(value, minimum: int, maximum: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"{label} 必须是整数") from exc
    if not minimum <= number <= maximum:
        raise ProfileError(f"{label} 必须在 {minimum}~{maximum} 之间")
    return number


def _parse_actions(raw, label: str) -> tuple[CombatAction, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProfileError(f"{label} 必须是数组")
    if len(raw) > 500:
        raise ProfileError(f"{label} 最多允许 500 个动作")
    actions = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ProfileError(f"{label}[{index}] 必须是对象")
        key = str(item.get("key") or "").strip().lower()
        if key not in SUPPORTED_KEYS and key != WAIT_ACTION_KEY:
            raise ProfileError(
                f"{label}[{index}] 不支持按键 {key!r}，可用: {', '.join(SUPPORTED_KEYS)}")
        if key == WAIT_ACTION_KEY:
            actions.append(CombatAction(
                key=key,
                hold_ms=0,
                wait_ms=_bounded_int(
                    item.get("wait_ms", 0),
                    0,
                    60000,
                    f"{label}[{index}].wait_ms",
                ),
            ))
            continue
        actions.append(CombatAction(
            key=key,
            hold_ms=_bounded_int(
                item.get("hold_ms", 55), 10, 5000, f"{label}[{index}].hold_ms"),
            wait_ms=_bounded_int(
                item.get("wait_ms", 250), 0, 60000, f"{label}[{index}].wait_ms"),
        ))
    return tuple(actions)


def _parse_heroes(raw) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ProfileError("hero_order 必须是数组")
    heroes = tuple(str(hero).strip() for hero in raw if str(hero).strip())
    if not 1 <= len(heroes) <= 2:
        raise ProfileError("hero_order 只能包含一个主C和最多一个辅助")
    if len(set(heroes)) != len(heroes):
        raise ProfileError("hero_order 不能有重复英雄")
    return heroes


def _parse_secondary_sequences(
    raw,
    hero_order: tuple[str, ...],
    initial_hero: str,
) -> tuple[SecondarySequence, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ProfileError("secondary_sequences 必须是对象")
    sequences = []
    for hero, item in raw.items():
        hero_name = str(hero).strip()
        if hero_name not in hero_order:
            raise ProfileError(f"辅助 {hero_name} 不在英雄顺序中")
        if hero_name == initial_hero:
            raise ProfileError("主C不能同时配置为辅助")
        if not isinstance(item, dict):
            raise ProfileError(f"辅助 {hero_name} 配置必须是对象")
        actions = _parse_actions(
            item.get("actions"), f"secondary_sequences.{hero_name}")
        cooldowns = _parse_skill_cooldowns(item.get("skill_cooldowns"))
        raw_switch_after = item.get("switch_after_ms", 0)
        if not actions and not cooldowns and int(raw_switch_after or 0) <= 0:

            continue
        if not actions:
            raise ProfileError(f"辅助 {hero_name} 必须至少配置一个动作")
        if cooldowns:
            switch_after_ms = max(skill.cooldown_ms for skill in cooldowns)
        else:
            if int(raw_switch_after or 0) <= 0:
                raise ProfileError(
                    f"辅助 {hero_name} 有动作时至少一个技能冷却必须大于0"
                )
            switch_after_ms = _bounded_int(
                raw_switch_after,
                100,
                600000,
                f"{hero_name}.switch_after_ms",
            )
        if cooldowns:
            longest = max(skill.cooldown_ms for skill in cooldowns)
            longest_keys = {
                skill.key for skill in cooldowns if skill.cooldown_ms == longest
            }
            if not any(action.key in longest_keys for action in actions):
                names = "/".join(key.upper() for key in sorted(longest_keys))
                raise ProfileError(
                    f"辅助 {hero_name} 的最长冷却技能 {names} 未写入动作流程"
                )
        sequences.append(SecondarySequence(
            hero=hero_name,
            switch_after_ms=switch_after_ms,
            actions=actions,
            skill_cooldowns=cooldowns,
        ))
    return tuple(sequences)
