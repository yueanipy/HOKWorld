"""在录制会话上回放钓鱼识别,打印状态时间线 + 关键时刻分数,用于校准。

用法: python fishing/replay_test.py [session_dir]   (默认最新会话)
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fishing.matcher import FishingRecognizer  # noqa: E402

HERE = Path(__file__).resolve().parent.parent


def _latest_session() -> Path:
    ds = sorted(glob.glob(str(HERE / "sessions" / "*")), reverse=True)
    return Path(ds[0])


def _ft(f: str) -> float:
    return float(os.path.basename(f).split("_")[1].replace(".jpg", ""))


def main() -> int:
    sess = Path(sys.argv[1]) if len(sys.argv) > 1 else _latest_session()
    frames = sorted(glob.glob(str(sess / "frames" / "*.jpg")))
    if not frames:
        print("no frames in", sess)
        return 2
    rec = FishingRecognizer()

    # 读输入事件(抛竿/拉杆点击时刻),用于对照
    events = []
    ev_path = sess / "events.jsonl"
    if ev_path.exists():
        for line in open(ev_path, encoding="utf-8"):
            e = json.loads(line)
            if e["kind"] == "mouse_click" and e.get("pressed"):
                events.append(e["t"])

    rows = []
    for f in frames:
        st, sc = rec.classify(cv2.imread(f))
        rows.append((_ft(f), st, sc))

    # 压缩成连续状态段
    print(f"session: {sess.name}  frames: {len(rows)}  clicks: {len(events)}")
    print("=== state timeline (compressed) ===")
    seg_start = rows[0][0]
    seg_state = rows[0][1]
    for i in range(1, len(rows)):
        if rows[i][1] != seg_state:
            print(f"  {seg_start:6.2f} - {rows[i][0]:6.2f}  {seg_state}")
            seg_start = rows[i][0]
            seg_state = rows[i][1]
    print(f"  {seg_start:6.2f} - {rows[-1][0]:6.2f}  {seg_state}")

    # 点击时刻附近的识别(应为 READY=抛竿 或 HOOK=拉杆)
    print("=== scores at click times ===")
    for ct in events:
        near = min(rows, key=lambda r: abs(r[0] - ct))
        print(f"  click t={ct:6.2f}  -> frame t={near[0]:.2f} {near[1]}  {near[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
