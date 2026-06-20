"""运行环境路径:区分「随程序分发的只读资源」与「用户可写数据」。

- 资源(钓鱼模板、图标、Qt 插件等)随程序走:冻结后在 PyInstaller 解包目录,源码下在项目目录。
- 用户数据(配置 / 日志 / 缓存 / 更新包 / 调试帧 / 成功截图)一律写
  %LOCALAPPDATA%\\HOKWorldScript;安装目录保持只读,卸载或覆盖升级都不动用户数据。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from version import USER_DATA_NAME


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_dev() -> bool:
    """开发模式:源码运行即视为开发;打包后默认关闭(可用 HOKWORLD_DEV=1 临时打开排错)。
    非开发模式下不向本地写调试帧/成功截图,只留统一日志。"""
    return (not is_frozen()) or os.environ.get("HOKWORLD_DEV") == "1"


def resource_root() -> Path:
    """只读资源根目录。"""
    if is_frozen():
        # onedir 打包:sys._MEIPASS 指向随 exe 的 _internal 资源目录
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """拼出资源绝对路径,例:resource_path('assets', 'app.ico')。"""
    return resource_root().joinpath(*parts)


def user_data_dir() -> Path:
    """%LOCALAPPDATA%\\HOKWorldScript;无该环境变量时回退到用户目录。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else (Path.home() / "AppData" / "Local")
    d = root / USER_DATA_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sub(name: str) -> Path:
    d = user_data_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    return _sub("logs")


def cache_dir() -> Path:
    return _sub("cache")


def updates_dir() -> Path:
    return _sub("updates")


def sessions_dir() -> Path:
    return _sub("sessions")


def screenshots_dir() -> Path:
    return _sub("屏幕截图")


def config_path() -> Path:
    return user_data_dir() / "config.json"
