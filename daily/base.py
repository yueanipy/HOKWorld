'每日任务一条龙 —— 任务接口 DailyTask(所有任务模块实现它;仿 一条龙任务 的 Application)。'
from __future__ import annotations

from abc import ABC, abstractmethod


class TaskResult:
    SUCCESS = "success"     
    SKIP = "skip"           
    FAIL = "fail"           
    ABORT = "abort"         


class DailyTask(ABC):
    '一个每日任务。'

    
    task_id: str = ""
    
    name: str = ""

    def __init__(self, ctx) -> None:
        'ctx:一条龙运行上下文(见 orchestrator.DailyContext)——提供 capture / log /。'
        self.ctx = ctx

    def check_available(self) -> bool:
        '今天这个任务是否还需要做(默认 True;可按管理地图状态/朝闻道 ✓ 提前判空跑)。'
        return True

    @abstractmethod
    def run(self) -> str:
        '执行任务,返回 TaskResult。'
        raise NotImplementedError
