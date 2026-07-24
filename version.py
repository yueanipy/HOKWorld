'单一版本与应用元信息来源。'
from __future__ import annotations

__version__ = "0.3.5"


APP_NAME = "HOKWorld"                  
APP_DISPLAY = "HOKWorld · 王者荣耀世界"   
USER_DATA_NAME = "HOKWorldScript"      
INSTALLER_BASENAME = "HOKWorldScript"  
PUBLISHER = "Peiyu Yuan"


GITHUB_OWNER = "yueanipy"
GITHUB_REPO = "HOKWorld"


def version_tuple(v: str = __version__) -> tuple:
    "把 '0.0.1' / 'v0.0.1' 解析成可比较的整数元组(非数字段记 0)。"
    s = (v or "").strip().lstrip("vV")
    out = []
    for part in s.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:4])
