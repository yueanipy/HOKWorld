'每日任务一条龙 —— 调度器(按配置顺序跑任务,单任务失败跳过不中断)。'
from __future__ import annotations

from daily import navigation as nav
from daily.base import TaskResult
from daily.config import DailyConfig, TASK_REGISTRY
from daily.context import DailyContext
from daily.tasks import build_task


class DailyOrchestrator:
    def __init__(self, log=print, on_progress=lambda done, total: None) -> None:
        self.log = log
        self.on_progress = on_progress
        self.ctx = DailyContext(log=log)
        self.config = DailyConfig()

    def stop(self) -> None:
        self.ctx.stop()

    def set_paused(self, on: bool) -> None:
        self.ctx.set_paused(on)

    def run(self) -> dict:
        '跑一条龙。'
        results: dict[str, str] = {}
        run_list = self.config.run_list()
        if not run_list:
            self.log("一条龙:没有启用的任务(去设置里勾选)")
            return results
        if not self.ctx.start():
            return results
        self.log(f"每日任务一条龙:开始(共 {len(run_list)} 项;F12 急停)")
        try:
            for i, task_id in enumerate(run_list):
                if self.ctx.should_stop():
                    self.log("一条龙已停止")
                    break
                name = TASK_REGISTRY.get(task_id, task_id)
                self.on_progress(i, len(run_list))
                nav.back_to_world(self.ctx)                 
                task = build_task(task_id, self.ctx)
                if task is None:
                    self.log(f"[{name}] 未实现,跳过")
                    results[task_id] = TaskResult.SKIP
                    continue
                try:
                    if not task.check_available():
                        self.log(f"[{name}] 无需执行,跳过")
                        results[task_id] = TaskResult.SKIP
                        continue
                    self.log(f"[{name}] 开始")
                    res = task.run()
                    results[task_id] = res or TaskResult.SUCCESS
                    self.log(f"[{name}] 结束:{results[task_id]}")
                except Exception as exc:
                    from runtime_guard import dev_log
                    dev_log(f"每日任务[{name}]异常", exc)
                    self.log(f"[{name}] 出错跳过:{type(exc).__name__}: {exc}")
                    results[task_id] = TaskResult.FAIL
            self.on_progress(len(run_list), len(run_list))
            nav.back_to_world(self.ctx)
        finally:
            self.ctx.close()
        done = sum(1 for v in results.values() if v == TaskResult.SUCCESS)
        self.log(f"每日任务一条龙:完成 {done}/{len(run_list)}(明细:{results})")
        return results
