"""无黄框、无闪烁屏幕捕获 —— 直接采用 BetterGI / MaaFramework 的"最兼容"做法。

**真正的根因(实测定位)**:旧版用 mss,而 mss 的 BitBlt **带了 `CAPTUREBLT` 标志**
(见 mss/windows/gdi.py:`BitBlt(..., SRCCOPY | CAPTUREBLT)`)。`CAPTUREBLT` 会把鼠标光标也
合成进截图,其**副作用就是让硬件光标不断重绘 → 忽明忽暗闪烁**(微软 GDI 文档明确记载)。
之前误以为"GDI 截屏天生闪",于是绕去 DXGI(会 access lost)/WGC(Win10 强制黄框)——其实都不必。

**BetterGI / MaaFramework 怎么解的**:它们的"最兼容、问题最少"截图方式,就是**普通 GDI BitBlt,
只用 `SRCCOPY`、不带 `CAPTUREBLT`**:
  · BetterGI:截图方式 "BitBlt"(官方说明:兼容性最好、问题最少);另有 WGC、DwmGetDxSharedSurface 作为高性能可选。
  · MaaFramework:`MaaWin32ScreencapMethod_ScreenDC`(兼容性 High)/`_GDI`;另有 `_FramePool`(WGC)、`_DXGI_DesktopDup`。
这样三者兼得:**无黄框**(非 WGC)、**无光标闪烁**(无 CAPTUREBLT)、**所有界面都稳**
(GDI 读 DWM 合成桌面,不像 DXGI 在每日任务/剧情里 access lost)。

本模块即用此法:从**桌面 DC** 做 `SRCCOPY` BitBlt,复用 DC/位图提速;输出与原 mss 完全一致的 BGR(H×W×3)
(实测与 mss 逐通道 corr=1.0,故识别阈值无需改)。窗口移动/缩放每帧按实时客户区重裁。
"""
from __future__ import annotations

import numpy as np
import win32con
import cv2
import win32gui
import win32ui

from winenv import client_rect_on_screen
from runtime_guard import dev_log

NORM_W = 1920   # 识别基准宽(同 fishing/template_bank.NORM_W);区域截图贴回此宽画布


class GameCapture:
    """游戏客户区捕获:GDI BitBlt(SRCCOPY,无 CAPTUREBLT)。无黄框、无光标闪烁、全界面稳定。"""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.mode = "bitblt"
        self._desktop = None
        self._src_dc = None        # 桌面窗口 DC 句柄
        self._mfc = None           # 桌面 DC(win32ui 包装)
        self._res = {}             # (w,h) -> (内存 DC, 兼容位图):按尺寸缓存复用
        self._last: np.ndarray | None = None
        self._canvas: np.ndarray | None = None   # 区域截图复用的归一化黑底画布

    def __enter__(self) -> "GameCapture":
        self.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False

    def start(self) -> str:
        self.mode = "bitblt"
        dev_log("capture: 启用 GDI BitBlt(SRCCOPY,无 CAPTUREBLT;无黄框、无闪烁、全界面稳定)")
        return self.mode

    def _ensure(self, w: int, h: int):
        """取(并按尺寸缓存)兼容内存 DC / 位图;桌面 DC 各尺寸共用。
        实时检测每 tick 交替截几个**不同尺寸**的小区域(如剧情:右上控制簇 + 确认框区),
        按尺寸缓存才不会逐次销毁重建 DC/位图。返回 (mem_dc, bmp)。"""
        pair = self._res.get((w, h))
        if pair is not None:
            return pair
        if self._mfc is None:
            self._desktop = win32gui.GetDesktopWindow()
            self._src_dc = win32gui.GetWindowDC(self._desktop)
            self._mfc = win32ui.CreateDCFromHandle(self._src_dc)
        if len(self._res) >= 8:    # 窗口缩放会产生新尺寸;条目过多整体重建,防 GDI 句柄堆积
            self._free()
            return self._ensure(w, h)
        mem = self._mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(self._mfc, w, h)
        mem.SelectObject(bmp)
        pair = (mem, bmp)
        self._res[(w, h)] = pair
        return pair

    def _blit(self, rx: int, ry: int, rw: int, rh: int) -> np.ndarray:
        """BitBlt 屏幕矩形 (rx,ry,rw,rh) → BGR(rh×rw×3)视图。只用 SRCCOPY(无 CAPTUREBLT → 不闪)。"""
        mem, bmp = self._ensure(rw, rh)
        mem.BitBlt((0, 0), (rw, rh), self._mfc, (rx, ry), win32con.SRCCOPY)
        bits = bmp.GetBitmapBits(True)                         # BGRA(top-down)
        return np.frombuffer(bits, np.uint8).reshape(rh, rw, 4)[:, :, :3]

    def grab(self) -> np.ndarray | None:
        """整客户区 BGR(H×W×3)。"""
        x, y, w, h = client_rect_on_screen(self.hwnd)
        if w <= 0 or h <= 0:
            return self._last
        try:
            self._last = np.ascontiguousarray(self._blit(x, y, w, h))   # 连续、可写,供 cv2/ocr 安全使用
            return self._last
        except Exception as exc:
            dev_log("capture: BitBlt 失败,重建 DC", exc)
            self._free()                                       # 窗口尺寸突变/DC 失效 → 重建后下帧恢复
            return self._last

    def grab_region_canvas(self, roi) -> np.ndarray | None:
        """**只截**客户区 roi=(x0,y0,x1,y1)(归一化)子区域,贴回一张 1920 宽归一化黑底画布的对应位置后返回。
        对识别器等价于"整帧归一化",但只 BitBlt 了一小块、省掉绝大部分拷贝(4K 全屏 ~60ms → 一小块 ~7ms)。
        要求 roi 覆盖该识别器用到的全部 ROI(画布其余处为黑、不参与匹配)。
        画布跨调用复用:同一 tick 连续截多个小区域(如剧情:右上控制簇 + 确认框区)会**叠加**贴到
        同一画布 → 比截一个大并集更省;识别器只读"本 tick 刚贴过"的 ROI 即可保证新鲜。"""
        x, y, w, h = client_rect_on_screen(self.hwnd)
        if w <= 0 or h <= 0:
            return self._canvas
        x0, y0, x1, y1 = roi
        rx, ry = x + int(round(x0 * w)), y + int(round(y0 * h))
        rw, rh = int(round((x1 - x0) * w)), int(round((y1 - y0) * h))
        if rw <= 0 or rh <= 0:
            return self._canvas
        try:
            sub = self._blit(rx, ry, rw, rh)
        except Exception as exc:
            dev_log("capture: 区域 BitBlt 失败,重建 DC", exc)
            self._free()
            return self._canvas
        norm_h = max(1, int(round(h * NORM_W / w)))
        if self._canvas is None or self._canvas.shape[:2] != (norm_h, NORM_W):
            self._canvas = np.zeros((norm_h, NORM_W, 3), np.uint8)
        dw = max(1, int(round((x1 - x0) * NORM_W)))
        dh = max(1, int(round((y1 - y0) * norm_h)))
        px, py = int(round(x0 * NORM_W)), int(round(y0 * norm_h))
        self._canvas[py:py + dh, px:px + dw] = cv2.resize(sub, (dw, dh), interpolation=cv2.INTER_AREA)
        return self._canvas

    def _free(self) -> None:
        for mem, bmp in self._res.values():
            try:
                win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
            try:
                mem.DeleteDC()
            except Exception:
                pass
        self._res = {}
        try:
            if self._mfc is not None:
                self._mfc.DeleteDC()
        except Exception:
            pass
        try:
            if self._src_dc is not None:
                win32gui.ReleaseDC(self._desktop, self._src_dc)
        except Exception:
            pass
        self._mfc = self._src_dc = None

    def stop(self) -> None:
        self._free()
        self._last = None
        self._canvas = None
