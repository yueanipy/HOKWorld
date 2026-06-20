"""在线更新:基于 GitHub Releases 检查 / 下载 / 校验 / 拉起安装器。仅用标准库。

流程:查最新 Release → 比对版本(禁止降级)→ 下载 Setup.exe(可取消、带进度)
→ SHA-256 校验 → 启动安装器(独立进程)→ 主程序退出由调用方负责(绝不覆盖运行中的自身)。
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import threading
import urllib.request
from dataclasses import dataclass

from version import GITHUB_OWNER, GITHUB_REPO, __version__, version_tuple

API_LATEST = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
_UA = f"HOKWorld-Updater/{__version__}"


class CancelledError(Exception):
    """下载被用户取消。"""


def current_version() -> str:
    return __version__


def is_newer(remote: str, local: str | None = None) -> bool:
    """remote 是否严格新于 local(禁止降级:相等或更旧都返回 False)。"""
    return version_tuple(remote) > version_tuple(local or __version__)


@dataclass
class ReleaseInfo:
    version: str            # 规范化版本号(去掉前导 v)
    tag: str
    name: str
    notes: str              # 更新说明(Release body)
    installer_url: str | None
    installer_name: str | None
    sha256_url: str | None
    html_url: str

    @property
    def has_installer(self) -> bool:
        return bool(self.installer_url)


def _ctx():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


def _open(url: str, timeout: int = 15):
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout, context=_ctx())


def fetch_latest(timeout: int = 15) -> ReleaseInfo:
    """拉取最新 Release;无任何 Release 时 GitHub 返回 404 → 抛 HTTPError。"""
    with _open(API_LATEST, timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = data.get("tag_name") or ""
    version = (tag or data.get("name") or "").lstrip("vV").strip()
    installer_url = installer_name = sha_url = None
    for a in data.get("assets", []):
        name = a.get("name", "")
        low = name.lower()
        url = a.get("browser_download_url")
        if low.endswith(".sha256"):
            sha_url = url
        elif low.endswith("setup.exe") or (low.endswith(".exe") and installer_url is None):
            installer_url, installer_name = url, name
    return ReleaseInfo(
        version=version, tag=tag, name=data.get("name") or tag, notes=data.get("body") or "",
        installer_url=installer_url, installer_name=installer_name,
        sha256_url=sha_url, html_url=data.get("html_url") or "",
    )


def fetch_sha256(info: ReleaseInfo, timeout: int = 15) -> str | None:
    """下载 .sha256 旁车文件,取首段 64 位十六进制;没有则 None(无法校验)。"""
    if not info.sha256_url:
        return None
    try:
        with _open(info.sha256_url, timeout) as r:
            text = r.read().decode("utf-8", "ignore").strip()
    except Exception:
        return None
    token = text.split()[0] if text else ""
    return token.lower() if len(token) == 64 else None


def download(url: str, dest, progress=None, cancel: threading.Event | None = None,
             timeout: int = 30) -> str:
    """流式下载到 dest(先写 .part 再原子改名)。progress(done,total);cancel 置位即抛 CancelledError。"""
    dest = str(dest)
    tmp = dest + ".part"
    try:
        with _open(url, timeout) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise CancelledError()
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, dest)
    return dest


def sha256_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_sha256(path, expected: str | None) -> bool:
    """无 expected 视为校验失败(宁可拒绝也不装未校验包)。"""
    if not expected:
        return False
    return sha256_file(path).lower() == expected.lower()


def launch_installer(path) -> bool:
    """拉起安装器(独立进程)。返回是否成功启动;失败由调用方提示并继续用旧版。"""
    try:
        if hasattr(os, "startfile"):
            os.startfile(os.fspath(path))   # type: ignore[attr-defined]
            return True
        import subprocess
        subprocess.Popen([os.fspath(path)], close_fds=True)
        return True
    except Exception:
        return False
