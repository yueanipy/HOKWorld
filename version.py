"""单一版本与应用元信息来源。GUI、安装器、更新检查、发布版本都从这里取值,避免各处版本号漂移。"""
from __future__ import annotations

__version__ = "0.3.3"

# 应用标识
APP_NAME = "HOKWorld"                  # 可执行文件名 / 窗口标题主名
APP_DISPLAY = "HOKWorld · 王者荣耀世界"   # 关于页展示名
USER_DATA_NAME = "HOKWorldScript"      # %LOCALAPPDATA% 下的用户数据目录名
INSTALLER_BASENAME = "HOKWorldScript"  # 安装器基名 → HOKWorldScript-<版本>-Setup.exe
PUBLISHER = "Peiyu Yuan"

# 在线更新:GitHub Releases 所在仓库(发布与下载都用这个仓库)
GITHUB_OWNER = "yueanipy"
GITHUB_REPO = "HOKWorld"


def version_tuple(v: str = __version__) -> tuple:
    """把 '0.0.1' / 'v0.0.1' 解析成可比较的整数元组(非数字段记 0)。"""
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
