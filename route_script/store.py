'管理用户路线TXT和运行JSON缓存。'
from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from config import user_data_dir
from runtime_guard import atomic_write_json, atomic_write_text

from .model import (
    RouteEvent, RouteMetadata, RouteScript,
    normalize_route_name,
)
from .parser import format_text, load_json, parse_file, parse_text, source_hash


class RouteStore:
    '提供路线文件增删改查和缓存同步。'

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory else user_data_dir() / "route_scripts"
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalize_name(name: str) -> str:
        return normalize_route_name(name)

    def _conflicting_name(self, name: str, *, exclude: str = "") -> str | None:
        folded = self.normalize_name(name).casefold()
        excluded = str(exclude).casefold()
        return next((known for known in self.list_names()
                     if known.casefold() == folded and known.casefold() != excluded), None)

    def txt_path(self, name: str) -> Path:
        return self.directory / f"{self.normalize_name(name)}.txt"

    def json_path(self, name: str) -> Path:
        return self.directory / f"{self.normalize_name(name)}.json"

    def list_names(self) -> tuple[str, ...]:
        return tuple(sorted(
            (path.stem for path in self.directory.glob("*.txt")),
            key=lambda value: value.casefold(),
        ))

    def create(self, name: str, *, start_teleport: str = "") -> Path:
        clean = self.normalize_name(name)
        path = self.txt_path(clean)
        conflict = self._conflicting_name(clean)
        if path.exists() or conflict is not None:
            raise FileExistsError(f"路线已存在：{clean}")
        route = RouteScript(
            RouteMetadata(
                name=clean,
                start_teleport=start_teleport,
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
            (RouteEvent(0.0, "snapshot", {"name": "路线开始"}),),
        )
        atomic_write_text(path, format_text(route), encoding="utf-8")
        self.sync(clean)
        return path

    def duplicate(self, source: str, destination: str) -> Path:
        source_path = self.txt_path(source)
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        target_name = self.normalize_name(destination)
        target_path = self.txt_path(target_name)
        if target_path.exists() or self._conflicting_name(target_name) is not None:
            raise FileExistsError(f"路线已存在：{target_name}")
        source_snapshot = self.directory / "snapshots" / self.normalize_name(source)
        target_snapshot = self.directory / "snapshots" / target_name
        if target_snapshot.exists():
            raise FileExistsError(f"路线截图目录已存在：{target_name}")
        route = parse_file(source_path)
        metadata = replace(
            route.metadata,
            name=target_name,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        atomic_write_text(
            target_path,
            format_text(RouteScript(metadata, route.events)),
            encoding="utf-8",
        )
        try:
            self.sync(target_name)
            if source_snapshot.exists():
                shutil.copytree(source_snapshot, target_snapshot)
        except Exception:
            if target_snapshot.exists():
                shutil.rmtree(target_snapshot, ignore_errors=True)
            for new_path in (target_path, self.json_path(target_name)):
                try:
                    new_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        return target_path

    def rename(self, source: str, destination: str) -> Path:
        source_path = self.txt_path(source)
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        target_name = self.normalize_name(destination)
        target_path = self.txt_path(target_name)
        if (target_path.exists()
                or self._conflicting_name(target_name, exclude=source) is not None):
            raise FileExistsError(f"路线已存在：{target_name}")
        route = parse_file(source_path)
        updated = RouteScript(replace(route.metadata, name=target_name), route.events)
        source_snapshot = self.directory / "snapshots" / self.normalize_name(source)
        target_snapshot = self.directory / "snapshots" / target_name
        if target_snapshot.exists():
            raise FileExistsError(f"路线截图目录已存在：{target_name}")
        atomic_write_text(target_path, format_text(updated), encoding="utf-8")
        try:
            self.sync(target_name)
            if source_snapshot.exists():
                target_snapshot.parent.mkdir(parents=True, exist_ok=True)
                source_snapshot.rename(target_snapshot)
        except Exception:
            if target_snapshot.exists() and not source_snapshot.exists():
                try:
                    target_snapshot.rename(source_snapshot)
                except OSError:
                    pass
            for new_path in (target_path, self.json_path(target_name)):
                try:
                    new_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        try:
            source_path.unlink()
        except OSError as exc:
            raise OSError(
                f"新路线已安全保留，但旧路线文件删除失败：{source_path.name}") from exc
        try:
            self.json_path(source).unlink()
        except OSError:

            pass
        return target_path

    def delete(self, name: str) -> None:
        for path in (self.txt_path(name), self.json_path(name)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        snapshot_dir = self.directory / "snapshots" / self.normalize_name(name)
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)

    def sync(self, name: str) -> RouteScript:
        canonical_name = self.normalize_name(name)
        txt_path = self.txt_path(canonical_name)
        text = txt_path.read_text(encoding="utf-8")
        route = parse_text(text, canonical_name)


        if route.metadata.name != canonical_name:
            route = replace(
                route,
                metadata=replace(route.metadata, name=canonical_name),
            )
        route = replace(route, source_hash=source_hash(text))
        atomic_write_json(self.json_path(canonical_name), route.to_dict())
        return route

    def save(self, route: RouteScript, *, overwrite: bool = True) -> Path:
        route.validate()
        name = self.normalize_name(route.metadata.name)
        path = self.txt_path(name)
        if path.exists() and not overwrite:
            raise FileExistsError(f"路线已存在：{name}")
        text = format_text(route)
        atomic_write_text(path, text, encoding="utf-8")
        self.sync(name)
        return path

    def load(self, name: str) -> RouteScript:
        txt_path = self.txt_path(name)
        json_path = self.json_path(name)
        text = txt_path.read_text(encoding="utf-8")
        digest = source_hash(text)
        if json_path.exists():
            try:
                cached = load_json(json_path)
                if cached.source_hash == digest:
                    return cached
            except Exception:
                pass
        return self.sync(name)

    def sync_all(self) -> tuple[dict[str, RouteScript], dict[str, str]]:
        routes: dict[str, RouteScript] = {}
        errors: dict[str, str] = {}
        names = self.list_names()
        for name in names:
            try:
                routes[name] = self.sync(name)
            except Exception as exc:
                errors[name] = str(exc)
        known = set(names)
        for json_path in self.directory.glob("*.json"):
            if json_path.stem not in known:
                try:
                    json_path.unlink()
                except OSError:
                    pass
        return routes, errors

    def import_seed(self, seed: Path | str) -> Path | None:
        source = Path(seed)
        target = self.txt_path(source.stem)
        source_snapshot = source.parent / "snapshots" / source.stem
        target_snapshot = self.directory / "snapshots" / target.stem
        if (target.exists() or self._conflicting_name(source.stem) is not None
                or not source.exists()
                or (source_snapshot.exists() and target_snapshot.exists())):
            return None
        copied_snapshot = False
        try:
            shutil.copy2(source, target)
            if source_snapshot.exists():
                target_snapshot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_snapshot, target_snapshot)
                copied_snapshot = True
            self.sync(target.stem)
            return target
        except Exception:
            for created in (target, self.json_path(target.stem)):
                try:
                    created.unlink()
                except FileNotFoundError:
                    pass
            if copied_snapshot:
                shutil.rmtree(target_snapshot, ignore_errors=True)
            raise
