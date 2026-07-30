'清理安装版运行后遗留的调试帧和旧更新安装器。'
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def cleanup_runtime_artifacts() -> tuple[list[str], list[str]]:
    '安装版启动时清理非必要运行残留，源码开发环境不处理。'
    if not getattr(sys, "frozen", False):
        return [], []

    app_dir = Path(sys.executable).resolve().parent
    removed: list[str] = []
    errors: list[str] = []

    for directory in (
        app_dir / "data" / "sessions",
        app_dir / "_internal" / "sessions",
    ):
        if not directory.exists():
            continue
        try:
            shutil.rmtree(directory)
            removed.append(str(directory.relative_to(app_dir)))
        except OSError as exc:
            errors.append(f"{directory}: {exc}")

    updates = app_dir / "data" / "updates"
    if updates.is_dir():
        patterns = ("HOKWorldScript-*-Setup.exe", "*.part", "*.sha256")
        candidates = {path for pattern in patterns for path in updates.glob(pattern)}
        for path in candidates:
            try:
                path.unlink()
                removed.append(str(path.relative_to(app_dir)))
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        try:
            updates.rmdir()
        except OSError:
            pass

    return removed, errors
