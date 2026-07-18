'每日任务实现(每个文件一个 DailyTask 子类)+ 工厂 buildtask(taskid, ctx)。'
from __future__ import annotations


def build_task(task_id: str, ctx):
    '按 taskid 造任务实例;未实现返回 None(调度器跳过)。'
    from daily.tasks.photo import PhotoTask
    from daily.tasks.dispatch import DispatchTask
    from daily.tasks.interest_like import InterestLikeTask
    from daily.tasks.playbook_claim import PlaybookClaimTask
    from daily.tasks.farm import FarmTask
    from daily.tasks.incubator import IncubatorTask
    from daily.tasks.friends import FriendsTask
    from daily.tasks.pet_feeding import PetFeedingTask
    from daily.tasks.daily_fishing import DailyFishingTask
    from daily.tasks.cooking import CookingTask
    from daily.tasks.alchemy import AlchemyTask

    factory = {
        "farm": FarmTask,
        "incubator": IncubatorTask,
        "dispatch": DispatchTask,
        "photo": PhotoTask,
        "interest_like": InterestLikeTask,
        "friends": FriendsTask,
        "pet_feeding": PetFeedingTask,
        "playbook_claim": PlaybookClaimTask,
        "daily_fishing": DailyFishingTask,
        "cooking": CookingTask,
        "alchemy": AlchemyTask,
    }
    cls = factory.get(task_id)
    return cls(ctx) if cls else None
