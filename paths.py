'运行环境路径:区分「随程序分发的只读资源」与「用户可写数据」。'
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_dev() -> bool:
    '开发模式:源码运行即视为开发;打包后默认关闭(可用 HOKWORLDDEV=1 临时打开排错)。'
    return (not is_frozen()) or os.environ.get("HOKWORLD_DEV") == "1"


def resource_root() -> Path:
    '只读资源根目录。'
    if is_frozen():
        
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    "拼出资源绝对路径,例:resourcepath('assets', 'app.ico')。"
    return resource_root().joinpath(*parts)


def user_data_dir() -> Path:
    '本机数据目录:随程序走(打包后 = exe 同级的 data\\。'
    if is_frozen():
        base = Path(sys.executable).resolve().parent   
    else:
        base = Path(__file__).resolve().parent          
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



def config_path() -> Path:
    return user_data_dir() / "config.json"
