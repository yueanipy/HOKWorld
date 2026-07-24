'每日任务一条龙 —— 共享导航(多任务共用的"到某界面/某设施"的路)。'
from __future__ import annotations

import daily.recognizer as rec
from daily import regions as R
from runtime_guard import dev_log


def current_scene(f) -> str:
    '当前界面粗判(供导航/诊断)。'
    
    if rec.teleport_dialog(f)[0]:
        return "dialog"
    if rec.in_share_page(f):
        return "share"
    if rec.in_camera(f):
        return "camera"
    if rec.in_world_map(f):
        return "world_map"
    if rec.in_manage_map(f):
        return "manage_map"
    if rec.in_dispatch_page(f):
        return "dispatch"
    if rec.in_playbook(f):
        return "playbook"
    if rec.in_interest_circle(f):
        return "interest"
    if rec.member_drawer_open(f):
        return "member_drawer"
    if rec.friend_panel_open(f):
        return "friend_panel"
    if rec.in_residence(f):
        return "residence"
    if rec.in_esc_menu(f):
        return "esc_menu"
    return "world"


def back_to_world(ctx, max_esc: int = 8) -> bool:
    '连按 ESC 退回游戏世界(退出各级菜单/弹窗/相机/分享)。'
    for i in range(max_esc):
        if ctx.should_stop():
            return False
        f = ctx.grab()
        if f is None:
            return False
        
        
        if rec.in_world_hud(f):
            if i:
                dev_log(f"[daily] back_to_world: 第{i + 1}次快速确认角色 HUD")
            return True
        scene = current_scene(f)
        if scene == "world":
            return True
        dev_log(f"[daily] back_to_world: 第{i + 1}次 场景={scene} → ESC")
        ctx.press("esc")
        ctx.sleep(0.7)
    dev_log("[daily] back_to_world: 达上限仍未回到世界(界面可能异常)")
    return True


def open_esc_menu(ctx, timeout: float = 6.0) -> bool:
    '打开 ESC 系统菜单(磁贴阵列)。'
    f = ctx.grab()
    if f is not None and rec.in_esc_menu(f):
        return True
    ctx.press("esc")
    return bool(ctx.wait_until(rec.in_esc_menu, timeout=timeout, desc="打开ESC菜单"))


def enter_manage_map(ctx, timeout: float = 12.0) -> bool:
    '任意界面 → 居所·管理地图。'
    f = ctx.grab()
    if f is not None and rec.in_manage_map(f):
        return True
    if not open_esc_menu(ctx):
        return False
    
    f = ctx.grab()
    tile = (rec.find_tile(f, "居所") if f is not None else None) or R.PT_TILE_RESIDENCE
    ctx.click(tile)
    if not ctx.wait_until(rec.in_residence, timeout=timeout, desc="进入居所页"):
        return False
    
    f = ctx.grab()
    if f is not None and rec.in_manage_map(f):
        return True
    
    f = ctx.grab()
    tab = rec.find_word(f, R.ROI_RESIDENCE_TABS, "管理") if f is not None else None
    ctx.click(tab or R.PT_TAB_MANAGE)
    return bool(ctx.wait_until(rec.in_manage_map, timeout=timeout, desc="进入管理地图"))


def teleport_via_node(ctx, node_pt, timeout: float = 15.0) -> bool:
    '在管理地图传送到设施，并等待角色 HUD 连续稳定出现。'
    ctx.click(node_pt)
    dlg = ctx.wait_until(lambda f: rec.teleport_dialog(f)[0], timeout=6.0, desc="传送确认弹窗")
    if not dlg:
        return False
    f = ctx.grab()
    confirm_pt = rec.teleport_dialog(f)[2] if f is not None else R.PT_DIALOG_CONFIRM
    ctx.click(confirm_pt or R.PT_DIALOG_CONFIRM)
    
    
    hud_streak = 0

    def arrived(frame) -> bool:
        nonlocal hud_streak
        if rec.in_manage_map(frame) or rec.teleport_dialog(frame)[0]:
            hud_streak = 0
            return False
        if not rec.in_world_hud(frame):
            hud_streak = 0
            return False
        hud_streak += 1
        return hud_streak >= 2

    ok = bool(ctx.wait_until(
        arrived, timeout=timeout, interval=0.20, desc="传送完成并恢复角色HUD"))
    dev_log(f"[daily] 管理节点传送完成判定 hud_streak={hud_streak} ok={ok}")
    if ok:
        ctx.sleep(0.25)
    return ok
