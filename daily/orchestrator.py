'每日任务一条龙 —— 调度器(按配置顺序跑任务,单任务失败跳过不中断)。'
from __future__ import annotations

from daily import navigation as nav
from daily.base import TaskResult
from daily.config import DailyConfig, TASK_REGISTRY
from daily.context import DailyContext
from daily.tasks import build_task
from runtime_guard import dev_log


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

    def _run_attempt(self, task_id: str, name: str, attempt: int) -> tuple[str, bool]:
        '从干净世界状态执行一轮，返回结果和任务是否自带整项重试。'
        handles_retry = False
        try:
            world_ready = nav.back_to_world(self.ctx)
            if self.ctx.should_stop():
                return TaskResult.ABORT, handles_retry
            if world_ready is False:
                self.log(f"[{name}] 未确认回到角色界面，本项不在错误页面盲目执行")
                dev_log(
                    f"[daily-run] task={task_id} attempt={attempt} "
                    "preflight_world=False")
                return TaskResult.FAIL, handles_retry
            task = build_task(task_id, self.ctx)
            if task is None:
                self.log(f"[{name}] 未实现，跳过")
                return TaskResult.SKIP, True
            handles_retry = bool(getattr(task, "handles_failure_retry", False))
            if not task.check_available():
                if attempt > 1:
                    self.log(f"[{name}] 重试前已无需执行，按已完成处理")
                    return TaskResult.SUCCESS, handles_retry
                self.log(f"[{name}] 无需执行，跳过")
                return TaskResult.SKIP, handles_retry
            suffix = "（重试）" if attempt > 1 else ""
            self.log(f"[{name}] 开始{suffix}")
            result = task.run() or TaskResult.SUCCESS
        except Exception as exc:
            dev_log(f"每日任务[{name}]第 {attempt} 次执行异常", exc)
            self.log(f"[{name}] 第 {attempt} 次出错:{type(exc).__name__}: {exc}")
            result = TaskResult.FAIL
        dev_log(
            f"[daily-run] task={task_id} name={name} attempt={attempt} "
            f"result={result} internal_retry={handles_retry}"
        )
        return result, handles_retry

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
                result, handles_retry = self._run_attempt(task_id, name, 1)
                if (
                    result == TaskResult.FAIL
                    and not handles_retry
                    and not self.ctx.should_stop()
                ):
                    self.log(f"[{name}] 首次失败，返回世界状态后完整重试一次")
                    dev_log(f"[daily-run] task={task_id} scheduling_retry=1")
                    result, _ = self._run_attempt(task_id, name, 2)
                elif result == TaskResult.FAIL and handles_retry:
                    self.log(f"[{name}] 任务内部已完成失败重试，不再重复整项")
                results[task_id] = result
                self.log(f"[{name}] 结束:{result}")
            self.on_progress(len(run_list), len(run_list))
            nav.back_to_world(self.ctx)
        finally:
            self.ctx.close()
        done = sum(1 for v in results.values() if v == TaskResult.SUCCESS)
        self.log(f"每日任务一条龙:完成 {done}/{len(run_list)}(明细:{results})")
        dev_log(f"[daily-run] summary={results} success={done}/{len(run_list)}")
        return results
