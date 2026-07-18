'每日任务：荣耀塔烹饪台寻路，并确认累计制作三份食物。'
from daily.tasks._glory_tower_route import TowerRouteSpec
from daily.tasks.alchemy import AlchemyTask


class CookingTask(AlchemyTask):
    '复用制药制作闭环；世界路线中的所有水平动作做左右镜像。'

    task_id = "cooking"
    name = "烹饪（制作三份）"
    CRAFT_LABEL = "烹饪"
    RECIPE_LABEL = "菜谱"
    DEV_TAG = "cooking"

    route = TowerRouteSpec(
        target_word="烹饪",
        
        turn_total_px=-1000,
        coarse_pulses=8,
        recovery_side="d",
        fine_pulses=5,
        continuous_stop_word="烹饪",
        continuous_walk_timeout_s=4.0,
        turn_landmark_word="荣耀塔",
        post_landmark_turn_px=0,
        turn_landmark_timeout_s=2.0,
        
        
        
        telescope_pre_turn_steps=2,
        telescope_pre_turn_step_s=0.09,
        telescope_pre_turn_pause_s=0.08,
        telescope_turn_step_px=-150,
        telescope_turn_max_px=900,
        telescope_turn_stages_px=(-400, -200, -150, -150),
        telescope_move_after_first_stage=True,
        telescope_turn_timeout_s=1.6,
        telescope_post_turn_walk_s=0.0,
        telescope_recovery_side="",
        telescope_recovery_step_s=0.08,
        telescope_recovery_scan_px=(120, -240, 120),
        interact_on_arrival=True,
        select_lower_prompt_on_stack=True,
    )
