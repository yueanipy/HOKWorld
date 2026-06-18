"""HOKWord 录制器 — 采集《王者荣耀世界》画面帧 + 输入,用于后续自动化开发。

只读:绝不发送任何输入。点"开始"后,只在游戏窗口为**前台**时:
  - 连续截图(mss)游戏客户区 -> sessions/<id>/frames/NNNNNN_<ms>.jpg
  - 全局采集键鼠事件(pynput),含相对游戏客户区的归一化坐标 -> sessions/<id>/events.jsonl
  - 帧索引 -> sessions/<id>/frames.jsonl,会话信息 -> meta.json
游戏不在前台时(例如切到 VS Code / Alt-Tab 过程)自动不记录。
帧与事件用同一个 perf_counter 时钟对齐,便于事后还原"某刻画面 + 当时操作"。

用法:python recorder.py   (在游戏运行时启动,点开始/结束)
依赖:PySide6 mss opencv-python numpy pywin32 pynput
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
from pathlib import Path

import cv2
import mss
import numpy as np
import win32gui

from pynput import keyboard, mouse

GAME_TITLE = "王者荣耀世界"
HERE = Path(__file__).resolve().parent
SESSIONS = HERE / "sessions"
MOVE_THROTTLE_S = 0.05  # 鼠标移动事件最高 20Hz,避免日志爆炸
SAVE_MAX_W = 1920       # 存帧最大宽度;4K 客户区降采样到此,体积降约 4 倍(识别足够)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    # 以管理员重启"当前运行的入口脚本"。必须用 sys.argv[0],不能用 __file__——
    # __file__ 恒为 recorder.py(本函数定义处),会导致 app.py 提权时误启动录制器。
    import os
    script = os.path.abspath(sys.argv[0])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}"', None, 1)


# --------------------------- 游戏窗口工具 ---------------------------
def find_game_hwnd() -> int | None:
    found: list[int] = []

    def _cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == GAME_TITLE:
            found.append(h)

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int]:
    l, t, r, b = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (l, t))
    right, bottom = win32gui.ClientToScreen(hwnd, (r, b))
    return left, top, right - left, bottom - top


def is_foreground(hwnd: int) -> bool:
    return bool(hwnd) and win32gui.GetForegroundWindow() == hwnd and not win32gui.IsIconic(hwnd)


def _key_name(key) -> str:
    # pynput: 字符键有 .char;特殊键是 Key.<name>
    try:
        if hasattr(key, "char") and key.char is not None:
            return key.char
        return key.name  # type: ignore[attr-defined]
    except Exception:
        return str(key)


# --------------------------- 录制核心 ---------------------------
class Recorder:
    def __init__(self) -> None:
        self.running = False
        self.hwnd: int | None = None
        self.session_dir: Path | None = None
        self._t0 = 0.0
        self._fps = 10
        self.frame_count = 0
        self.event_count = 0
        self._cap_thread: threading.Thread | None = None
        self._kb: keyboard.Listener | None = None
        self._mouse: mouse.Listener | None = None
        self._ev_fp = None
        self._fr_fp = None
        self._lock = threading.Lock()
        self._last_move = 0.0
        self._client = (0, 0, 0, 0)  # 最近一次已知客户区(供事件归一化)

    # ---- 生命周期 ----
    def start(self, fps: int) -> str:
        if self.running:
            return "already running"
        hwnd = find_game_hwnd()
        if not hwnd:
            return f"未找到游戏窗口『{GAME_TITLE}』,请先运行游戏"
        self.hwnd = hwnd
        self._fps = max(1, min(60, int(fps)))
        sid = time.strftime("%Y%m%d_%H%M%S")
        self.session_dir = SESSIONS / sid
        (self.session_dir / "frames").mkdir(parents=True, exist_ok=True)
        self._ev_fp = open(self.session_dir / "events.jsonl", "w", encoding="utf-8")
        self._fr_fp = open(self.session_dir / "frames.jsonl", "w", encoding="utf-8")
        self.frame_count = 0
        self.event_count = 0
        self._t0 = time.perf_counter()
        self._wall_start = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.running = True

        self._cap_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._cap_thread.start()
        self._kb = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._mouse = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll,
                                     on_move=self._on_move)
        self._kb.start()
        self._mouse.start()
        return f"recording -> {self.session_dir}"

    def stop(self) -> str:
        if not self.running:
            return "not running"
        self.running = False
        if self._cap_thread:
            self._cap_thread.join(timeout=2.0)
        for lst in (self._kb, self._mouse):
            try:
                lst and lst.stop()
            except Exception:
                pass
        meta = {
            "game_title": GAME_TITLE,
            "hwnd": self.hwnd,
            "wall_start": getattr(self, "_wall_start", ""),
            "wall_end": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": round(time.perf_counter() - self._t0, 3),
            "fps_target": self._fps,
            "client_w": self._client[2],
            "client_h": self._client[3],
            "frames": self.frame_count,
            "events": self.event_count,
        }
        with self._lock:
            for fp in (self._ev_fp, self._fr_fp):
                try:
                    fp and fp.close()
                except Exception:
                    pass
        if self.session_dir:
            (self.session_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"stopped: {self.frame_count} frames, {self.event_count} events -> {self.session_dir}"

    def _now(self) -> float:
        return round(time.perf_counter() - self._t0, 4)

    # ---- 截图线程 ----
    def _capture_loop(self) -> None:
        period = 1.0 / self._fps
        with mss.mss() as sct:
            i = 0
            while self.running:
                t = time.perf_counter()
                if is_foreground(self.hwnd):
                    try:
                        x, y, w, h = client_rect_on_screen(self.hwnd)
                        self._client = (x, y, w, h)
                        if w > 0 and h > 0:
                            shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
                            frame = np.asarray(shot)[:, :, :3]
                            sh, sw = frame.shape[:2]
                            if sw > SAVE_MAX_W:  # 降采样存盘,省体积
                                frame = cv2.resize(frame, (SAVE_MAX_W, int(sh * SAVE_MAX_W / sw)),
                                                   interpolation=cv2.INTER_AREA)
                            fh, fw = frame.shape[:2]
                            ts = self._now()
                            fn = self.session_dir / "frames" / f"{i:06d}_{ts:.3f}.jpg"
                            cv2.imwrite(str(fn), frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
                            with self._lock:
                                self._fr_fp.write(json.dumps(
                                    {"i": i, "t": ts, "w": fw, "h": fh, "cw": w, "ch": h,
                                     "file": fn.name}) + "\n")
                            self.frame_count = i + 1
                            i += 1
                    except Exception:
                        pass
                dt = time.perf_counter() - t
                if dt < period:
                    time.sleep(period - dt)

    # ---- 事件写入(仅游戏前台) ----
    def _write(self, rec: dict) -> None:
        if not self.running or not is_foreground(self.hwnd):
            return
        rec["t"] = self._now()
        with self._lock:
            if self._ev_fp:
                self._ev_fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.event_count += 1

    def _norm(self, x: int, y: int) -> tuple[float, float]:
        cx, cy, cw, ch = self._client
        if cw <= 0 or ch <= 0:
            return -1.0, -1.0
        return round((x - cx) / cw, 4), round((y - cy) / ch, 4)

    def _on_press(self, key) -> None:
        self._write({"kind": "key_press", "key": _key_name(key)})

    def _on_release(self, key) -> None:
        self._write({"kind": "key_release", "key": _key_name(key)})

    def _on_click(self, x, y, button, pressed) -> None:
        nx, ny = self._norm(x, y)
        self._write({"kind": "mouse_click", "button": button.name, "pressed": pressed,
                     "x": x, "y": y, "nx": nx, "ny": ny})

    def _on_scroll(self, x, y, dx, dy) -> None:
        nx, ny = self._norm(x, y)
        self._write({"kind": "mouse_scroll", "dx": dx, "dy": dy, "x": x, "y": y, "nx": nx, "ny": ny})

    def _on_move(self, x, y) -> None:
        now = time.perf_counter()
        if now - self._last_move < MOVE_THROTTLE_S:
            return
        self._last_move = now
        nx, ny = self._norm(x, y)
        self._write({"kind": "mouse_move", "x": x, "y": y, "nx": nx, "ny": ny})


# --------------------------- GUI ---------------------------
def main() -> int:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QApplication, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
    )

    app = QApplication.instance() or QApplication(sys.argv)
    rec = Recorder()

    win = QWidget()
    win.setWindowTitle("HOKWord 录制器")
    win.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    win.resize(380, 230)
    root = QVBoxLayout(win)

    root.addWidget(QLabel("只录《王者荣耀世界》前台画面+操作;只读不发输入"))

    if not is_admin():
        warn = QLabel("⚠ 非管理员运行:游戏多为提权进程,键鼠采集会被 UIPI 拦截(只录到画面)。")
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#e0a030;")
        root.addWidget(warn)
        admin_btn = QPushButton("以管理员重启(才能采集键鼠)")
        admin_btn.clicked.connect(lambda: (relaunch_as_admin(), app.quit()))
        root.addWidget(admin_btn)

    frow = QHBoxLayout()
    frow.addWidget(QLabel("帧率 fps:"))
    fps_spin = QSpinBox()
    fps_spin.setRange(1, 30)
    fps_spin.setValue(10)
    frow.addWidget(fps_spin)
    frow.addStretch(1)
    root.addLayout(frow)

    status = QLabel("状态: 空闲")
    counts = QLabel("帧: 0 | 事件: 0")
    fg = QLabel("游戏前台: -")
    path_lbl = QLabel("会话: -")
    path_lbl.setWordWrap(True)
    for w in (status, fg, counts, path_lbl):
        root.addWidget(w)

    brow = QHBoxLayout()
    start_btn = QPushButton("▶ 开始")
    stop_btn = QPushButton("■ 结束")
    stop_btn.setEnabled(False)
    brow.addWidget(start_btn)
    brow.addWidget(stop_btn)
    root.addLayout(brow)

    def on_start():
        msg = rec.start(fps_spin.value())
        if rec.running:
            status.setText("状态: 录制中")
            path_lbl.setText(f"会话: {rec.session_dir}")
            start_btn.setEnabled(False)
            stop_btn.setEnabled(True)
            fps_spin.setEnabled(False)
        else:
            status.setText(f"状态: 无法开始 — {msg}")

    def on_stop():
        msg = rec.stop()
        status.setText("状态: 已停止")
        path_lbl.setText(msg)
        start_btn.setEnabled(True)
        stop_btn.setEnabled(False)
        fps_spin.setEnabled(True)

    start_btn.clicked.connect(on_start)
    stop_btn.clicked.connect(on_stop)

    def tick():
        counts.setText(f"帧: {rec.frame_count} | 事件: {rec.event_count}")
        if rec.hwnd:
            fg.setText(f"游戏前台: {'是' if is_foreground(rec.hwnd) else '否(暂停记录)'}")
        elif rec.running:
            fg.setText("游戏前台: 未找到窗口")

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(400)

    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
