'无黄框、无闪烁屏幕捕获 —— 直接采用 成熟游戏自动化方案 的"最兼容"做法。'
from __future__ import annotations

import threading

import numpy as np
import win32con
import cv2
import win32gui
import win32ui

from winenv import client_rect_on_screen
from runtime_guard import dev_log

NORM_W = 1920   




_CAPTURE_LOCK = threading.Lock()


class GameCapture:
    '游戏客户区捕获:GDI BitBlt(SRCCOPY,无 CAPTUREBLT)。'

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.mode = "bitblt"
        self._desktop = None
        self._src_dc = None        
        self._mfc = None           
        self._res = {}             
        self._last: np.ndarray | None = None
        self._canvas: np.ndarray | None = None   

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

    def rebind(self, hwnd: int) -> None:
        '把现有捕获器切换到另一个正式游戏窗口，不重建桌面 DC。'
        hwnd = int(hwnd or 0)
        if not hwnd or hwnd == self.hwnd:
            return
        old = self.hwnd
        self.hwnd = hwnd
        
        self._last = None
        self._canvas = None
        dev_log(f"capture: 游戏窗口重绑定 {old} -> {hwnd}")

    def _ensure(self, w: int, h: int):
        '取(并按尺寸缓存)兼容内存 DC / 位图;桌面 DC 各尺寸共用。'
        pair = self._res.get((w, h))
        if pair is not None:
            return pair
        if self._mfc is None:
            self._desktop = win32gui.GetDesktopWindow()
            self._src_dc = win32gui.GetWindowDC(self._desktop)
            self._mfc = win32ui.CreateDCFromHandle(self._src_dc)
        if len(self._res) >= 8:    
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
        'BitBlt 屏幕矩形 (rx,ry,rw,rh) → BGR(rh×rw×3)视图。'
        with _CAPTURE_LOCK:
            mem, bmp = self._ensure(rw, rh)
            mem.BitBlt((0, 0), (rw, rh), self._mfc, (rx, ry), win32con.SRCCOPY)
            bits = bmp.GetBitmapBits(True)                     
        return np.frombuffer(bits, np.uint8).reshape(rh, rw, 4)[:, :, :3]

    def grab(self) -> np.ndarray | None:
        '整客户区 BGR(H×W×3)。'
        x, y, w, h = client_rect_on_screen(self.hwnd)
        if w <= 0 or h <= 0:
            return self._last
        try:
            self._last = np.ascontiguousarray(self._blit(x, y, w, h))   
            return self._last
        except Exception as exc:
            dev_log("capture: BitBlt 失败,重建 DC", exc)
            self._free()                                       
            return self._last

    def grab_region_canvas(self, roi) -> np.ndarray | None:
        '只截客户区 roi=(x0,y0,x1,y1)(归一化)子区域,贴回一张 1920 宽归一化黑底画布的对应位置后返回。'
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

    def grab_regions_canvas(self, rois) -> np.ndarray | None:
        '一次生成只包含给定 ROI 的干净归一化画布。'
        rois = tuple(dict.fromkeys(tuple(r) for r in rois))
        if not rois:
            return None
        if self._canvas is not None:
            self._canvas.fill(0)
        frame = None
        for roi in rois:
            frame = self.grab_region_canvas(roi)
        return None if frame is None else frame.copy()

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
