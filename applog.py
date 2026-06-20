"""统一应用日志:所有软件状态(启动、任务进度、识别结果、更新事件、异常)集中写入
%LOCALAPPDATA%\\HOKWorldScript\\logs\\hokworld.log。带轮转,避免无限增长。

GUI 里出现的每条状态行都会同时落到这里;无论是否开发模式都记录,便于他人反馈问题。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from paths import is_dev, logs_dir

_LOGGER: logging.Logger | None = None
LOG_FILE = logs_dir() / "hokworld.log"


def get_logger() -> logging.Logger:
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    lg = logging.getLogger("hokworld")
    lg.setLevel(logging.DEBUG if is_dev() else logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        try:
            fh = RotatingFileHandler(
                LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            lg.addHandler(fh)
        except Exception:
            pass
    _LOGGER = lg
    return lg


def log(msg: str, level: int = logging.INFO) -> None:
    """记录一条状态。异常绝不外抛(日志失败不应影响主流程)。"""
    try:
        get_logger().log(level, msg)
    except Exception:
        pass


def debug(msg: str) -> None:
    log(msg, logging.DEBUG)


def exception(msg: str) -> None:
    try:
        get_logger().exception(msg)
    except Exception:
        pass
