'无黄框、无闪烁屏幕捕获 —— 直接采用 成熟游戏自动化方案 的"最兼容"做法。'
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np
import win32con
import cv2
import win32gui
import win32ui

from winenv import client_rect_on_screen
from runtime_guard import dev_log

NORM_W = 1920   
GDI_RESOURCE_IDLE_S = 120.0
GDI_RESOURCE_SWEEP_S = 30.0




_CAPTURE_LOCK = threading.Lock()


@dataclass(frozen=True)
class RegionCaptureMetrics:
    '最近一轮区域截图的低开销分阶段耗时。'

    regions: int = 0
    geometry_ms: float = 0.0
    blit_ms: float = 0.0
    compose_ms: float = 0.0
    copy_ms: float = 0.0
    total_ms: float = 0.0


@dataclass(frozen=True)
class RegionPatch:
    '归一化帧中的一个不可变像素块。'

    bounds: tuple[int, int, int, int]
    pixels: np.ndarray


class RegionFrame:
    '只保存已捕获 ROI 的轻量归一化帧。'

    __slots__ = ("_shape", "_patches")

    def __init__(self, width: int, height: int, patches) -> None:
        self._shape = (int(height), int(width), 3)
        checked = []
        for patch in patches:
            x0, y0, x1, y1 = patch.bounds
            expected = (max(0, y1 - y0), max(0, x1 - x0))
            if patch.pixels.shape[:2] != expected:
                raise ValueError("ROI 像素尺寸与归一化坐标不一致")
            patch.pixels.setflags(write=False)
            checked.append(patch)
        self._patches = tuple(checked)

    @property
    def shape(self) -> tuple[int, int, int]:
        return self._shape

    @property
    def ndim(self) -> int:
        return 3

    @property
    def size(self) -> int:
        h, w = self._shape[:2]
        return h * w * 3

    @property
    def nbytes(self) -> int:
        return sum(patch.pixels.nbytes for patch in self._patches)

    @property
    def patches(self) -> tuple[RegionPatch, ...]:
        return self._patches

    @staticmethod
    def _clip_box(box, width: int, height: int) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = (int(v) for v in box)
        x0, x1 = max(0, min(width, x0)), max(0, min(width, x1))
        y0, y1 = max(0, min(height, y0)), max(0, min(height, y1))
        return x0, y0, max(x0, x1), max(y0, y1)

    def crop_box(self, box) -> np.ndarray:
        '按归一化帧像素坐标裁切，仅组合相交的 ROI。'
        height, width = self._shape[:2]
        x0, y0, x1, y1 = self._clip_box(box, width, height)
        out_w, out_h = x1 - x0, y1 - y0
        if out_w <= 0 or out_h <= 0:
            return np.empty((0, 0, 3), np.uint8)

        
        
        for patch in reversed(self._patches):
            px0, py0, px1, py1 = patch.bounds
            intersects = max(x0, px0) < min(x1, px1) and max(y0, py0) < min(y1, py1)
            if not intersects:
                continue
            if px0 <= x0 and py0 <= y0 and px1 >= x1 and py1 >= y1:
                sub = patch.pixels[y0 - py0:y1 - py0, x0 - px0:x1 - px0]
                result = np.ascontiguousarray(sub)
                result.setflags(write=False)
                return result
            break

        result = np.zeros((out_h, out_w, 3), np.uint8)
        for patch in self._patches:
            px0, py0, px1, py1 = patch.bounds
            ix0, iy0 = max(x0, px0), max(y0, py0)
            ix1, iy1 = min(x1, px1), min(y1, py1)
            if ix0 >= ix1 or iy0 >= iy1:
                continue
            result[iy0 - y0:iy1 - y0, ix0 - x0:ix1 - x0] = patch.pixels[
                iy0 - py0:iy1 - py0, ix0 - px0:ix1 - px0]
        result.setflags(write=False)
        return result

    def crop_roi(self, roi) -> np.ndarray:
        '按归一化坐标裁切。'
        height, width = self._shape[:2]
        x0, y0, x1, y1 = roi
        return self.crop_box((int(x0 * width), int(y0 * height),
                              int(x1 * width), int(y1 * height)))

    def materialize(self) -> np.ndarray:
        '仅供兼容诊断使用，生成完整 1920 宽黑底画布。'
        height, width = self._shape[:2]
        result = np.zeros((height, width, 3), np.uint8)
        for patch in self._patches:
            x0, y0, x1, y1 = patch.bounds
            result[y0:y1, x0:x1] = patch.pixels
        result.setflags(write=False)
        return result


class GameCapture:
    '游戏客户区捕获:GDI BitBlt(SRCCOPY,无 CAPTUREBLT)。'

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.mode = "bitblt"
        self._desktop = None
        self._src_dc = None        
        self._mfc = None           
        self._res = {}             
        self._res_last_used = {}   
        self._next_res_sweep = 0.0
        self._last: np.ndarray | None = None
        self._canvas: np.ndarray | None = None   
        
        self._last_region_metrics = RegionCaptureMetrics()

    @property
    def last_region_metrics(self) -> RegionCaptureMetrics:
        return self._last_region_metrics

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
        key = (w, h)
        now = time.monotonic()
        self._prune_idle_resources(now, keep_key=key)
        pair = self._res.get(key)
        if pair is not None:
            self._res_last_used[key] = now
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
        self._res[key] = pair
        self._res_last_used[key] = now
        return pair

    def _release_resource(self, key: tuple[int, int]) -> None:
        '释放一个指定尺寸的兼容位图和内存 DC。'
        pair = self._res.pop(key, None)
        self._res_last_used.pop(key, None)
        if pair is None:
            return
        mem, bmp = pair
        try:
            win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        try:
            mem.DeleteDC()
        except Exception:
            pass

    def _prune_idle_resources(self, now: float, keep_key=None) -> None:
        '周期性释放长时间未使用的截图尺寸资源。'
        if now < self._next_res_sweep:
            return
        self._next_res_sweep = now + GDI_RESOURCE_SWEEP_S
        stale = [
            key for key, last_used in self._res_last_used.items()
            if key != keep_key and now - last_used >= GDI_RESOURCE_IDLE_S
        ]
        for key in stale:
            self._release_resource(key)
        if stale:
            dev_log(f"capture: 已释放 {len(stale)} 个闲置 GDI 截图尺寸资源")

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

    def _grab_region_canvas_at(self, roi, geometry) -> tuple[np.ndarray | None, float, float]:
        '使用本轮固定客户区几何截取一个 ROI，并返回 blit/合成耗时。'
        patch, blit_ms, compose_ms = self._grab_region_patch_at(roi, geometry)
        if patch is None:
            return self._canvas, blit_ms, compose_ms
        compose_started = time.perf_counter()
        norm_h = max(1, int(round(geometry[3] * NORM_W / geometry[2])))
        if self._canvas is None or self._canvas.shape[:2] != (norm_h, NORM_W):
            self._canvas = np.zeros((norm_h, NORM_W, 3), np.uint8)
        x0, y0, x1, y1 = patch.bounds
        self._canvas[y0:y1, x0:x1] = patch.pixels
        compose_ms += 1000.0 * (time.perf_counter() - compose_started)
        return self._canvas, blit_ms, compose_ms

    def _grab_region_patch_at(self, roi, geometry) -> tuple[RegionPatch | None, float, float]:
        '使用本轮固定客户区几何截取一个归一化 ROI 像素块。'
        x, y, w, h = geometry
        if w <= 0 or h <= 0:
            return None, 0.0, 0.0
        x0, y0, x1, y1 = roi
        sx0, sy0 = int(round(x0 * w)), int(round(y0 * h))
        sx1, sy1 = int(round(x1 * w)), int(round(y1 * h))
        rx, ry = x + sx0, y + sy0
        rw, rh = sx1 - sx0, sy1 - sy0
        if rw <= 0 or rh <= 0:
            return None, 0.0, 0.0

        blit_started = time.perf_counter()
        try:
            sub = self._blit(rx, ry, rw, rh)
        except Exception as exc:
            blit_ms = 1000.0 * (time.perf_counter() - blit_started)
            dev_log("capture: 区域 BitBlt 失败,重建 DC", exc)
            self._free()
            return None, blit_ms, 0.0
        blit_ms = 1000.0 * (time.perf_counter() - blit_started)

        compose_started = time.perf_counter()
        norm_h = max(1, int(round(h * NORM_W / w)))
        px0, py0 = int(round(x0 * NORM_W)), int(round(y0 * norm_h))
        dw = max(1, int(round((x1 - x0) * NORM_W)))
        dh = max(1, int(round((y1 - y0) * norm_h)))
        px0, py0 = max(0, px0), max(0, py0)
        px1, py1 = min(NORM_W, px0 + dw), min(norm_h, py0 + dh)
        pixels = cv2.resize(sub, (px1 - px0, py1 - py0), interpolation=cv2.INTER_AREA)
        pixels = np.ascontiguousarray(pixels)
        compose_ms = 1000.0 * (time.perf_counter() - compose_started)
        return RegionPatch((px0, py0, px1, py1), pixels), blit_ms, compose_ms

    def grab_region_canvas(self, roi) -> np.ndarray | None:
        '只截客户区 roi=(x0,y0,x1,y1)(归一化)子区域,贴回一张 1920 宽归一化黑底画布的对应位置后返回。'
        started = time.perf_counter()
        geometry_started = time.perf_counter()
        geometry = client_rect_on_screen(self.hwnd)
        geometry_ms = 1000.0 * (time.perf_counter() - geometry_started)
        frame, blit_ms, compose_ms = self._grab_region_canvas_at(roi, geometry)
        self._last_region_metrics = RegionCaptureMetrics(
            regions=1,
            geometry_ms=geometry_ms,
            blit_ms=blit_ms,
            compose_ms=compose_ms,
            total_ms=1000.0 * (time.perf_counter() - started),
        )
        return frame

    def grab_regions_canvas(self, rois) -> np.ndarray | None:
        '一次生成只包含给定 ROI 的干净归一化画布，供旧工具和诊断兼容。'
        rois = tuple(dict.fromkeys(tuple(r) for r in rois))
        if not rois:
            return None
        started = time.perf_counter()
        geometry_started = time.perf_counter()
        geometry = client_rect_on_screen(self.hwnd)
        geometry_ms = 1000.0 * (time.perf_counter() - geometry_started)

        
        
        compose_started = time.perf_counter()
        _, _, w, h = geometry
        if w <= 0 or h <= 0:
            return None
        norm_h = max(1, int(round(h * NORM_W / w)))
        self._canvas = np.zeros((norm_h, NORM_W, 3), np.uint8)
        compose_ms = 1000.0 * (time.perf_counter() - compose_started)
        frame = None
        blit_ms = 0.0
        for roi in rois:
            frame, roi_blit_ms, roi_compose_ms = self._grab_region_canvas_at(roi, geometry)
            blit_ms += roi_blit_ms
            compose_ms += roi_compose_ms

        result = frame
        copy_ms = 0.0
        self._last_region_metrics = RegionCaptureMetrics(
            regions=len(rois),
            geometry_ms=geometry_ms,
            blit_ms=blit_ms,
            compose_ms=compose_ms,
            copy_ms=copy_ms,
            total_ms=1000.0 * (time.perf_counter() - started),
        )
        return result

    def grab_regions_compact(self, rois) -> RegionFrame | None:
        '捕获多个 ROI 并直接发布轻量像素块，不创建整帧黑底画布。'
        rois = tuple(dict.fromkeys(tuple(r) for r in rois))
        if not rois:
            return None
        started = time.perf_counter()
        geometry_started = time.perf_counter()
        geometry = client_rect_on_screen(self.hwnd)
        geometry_ms = 1000.0 * (time.perf_counter() - geometry_started)
        _, _, w, h = geometry
        if w <= 0 or h <= 0:
            return None

        patches = []
        blit_ms = 0.0
        compose_ms = 0.0
        for roi in rois:
            patch, roi_blit_ms, roi_compose_ms = self._grab_region_patch_at(roi, geometry)
            blit_ms += roi_blit_ms
            compose_ms += roi_compose_ms
            if patch is None:
                self._last_region_metrics = RegionCaptureMetrics(
                    regions=len(rois), geometry_ms=geometry_ms, blit_ms=blit_ms,
                    compose_ms=compose_ms,
                    total_ms=1000.0 * (time.perf_counter() - started))
                return None
            patches.append(patch)

        norm_h = max(1, int(round(h * NORM_W / w)))
        frame = RegionFrame(NORM_W, norm_h, patches)
        self._last_region_metrics = RegionCaptureMetrics(
            regions=len(rois), geometry_ms=geometry_ms, blit_ms=blit_ms,
            compose_ms=compose_ms, copy_ms=0.0,
            total_ms=1000.0 * (time.perf_counter() - started))
        return frame

    def _free(self) -> None:
        for key in tuple(self._res):
            self._release_resource(key)
        self._res = {}
        self._res_last_used = {}
        self._next_res_sweep = 0.0
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
        self._last_region_metrics = RegionCaptureMetrics()
