"""运行环境路径:区分「随程序分发的只读资源」与「用户可写数据」。

- 资源(钓鱼模板、图标、Qt 插件等)随程序走:冻结后在 PyInstaller 解包目录,源码下在项目目录。
- 本机数据(配置 / 日志 / 缓存 / 采集名单 / 调试帧 / 成功截图)写**程序同级的 data\\**
  (随程序、不进 Windows 用户目录;本程序不区分用户角色)。覆盖升级保留,卸载随 {app}\\data 清。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


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
    """本机数据目录:**随程序**走(打包后 = exe 同级的 data\\;源码 = 项目根 data\\),
    不放进 Windows 用户目录(本程序不区分用户角色)。覆盖升级保留(不在安装器 [Files] 内),
    卸载随 {app}\\data 一并清。配置 / 日志 / 缓存 / 采集名单都在这。"""
    if is_frozen():
        base = Path(sys.executable).resolve().parent   # 安装目录(exe 同级)
    else:
        base = Path(__file__).resolve().parent          # 源码项目根
    d = base / "data"
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


def sessions_dir() -> Path:
    return _sub("sessions")


def screenshots_dir() -> Path:
    return _sub("屏幕截图")


def config_path() -> Path:
    return user_data_dir() / "config.json"
