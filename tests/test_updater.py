import hashlib
import json
import threading

import pytest

import updater


# ---------- 版本比较:禁止降级 ----------
def test_is_newer_blocks_downgrade_and_equal():
    assert updater.is_newer("0.0.2", "0.0.1") is True
    assert updater.is_newer("1.0.0", "0.9.9") is True
    assert updater.is_newer("0.0.1", "0.0.1") is False     # 相等不更新
    assert updater.is_newer("0.0.1", "0.0.2") is False     # 旧版禁止降级


# ---------- SHA-256 校验 ----------
def test_sha256_file_and_verify(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello world")
    digest = hashlib.sha256(b"hello world").hexdigest()
    assert updater.sha256_file(p) == digest
    assert updater.verify_sha256(p, digest) is True
    assert updater.verify_sha256(p, digest.upper()) is True   # 大小写不敏感
    assert updater.verify_sha256(p, "00" * 32) is False
    assert updater.verify_sha256(p, None) is False            # 无校验值即失败


# ---------- GitHub Release 解析 ----------
class _Resp:
    def __init__(self, payload=b"", headers=None):
        self._payload = payload
        self.headers = headers or {}

    def read(self, n=-1):
        if n is None or n < 0:
            data, self._payload = self._payload, b""
            return data
        data, self._payload = self._payload[:n], self._payload[n:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_latest_parses_assets(monkeypatch):
    rel = {
        "tag_name": "v0.0.2", "name": "HOKWorld 0.0.2", "body": "修了若干 bug",
        "html_url": "https://github.com/yueanipy/HOKWorld/releases/tag/v0.0.2",
        "assets": [
            {"name": "HOKWorldScript-0.0.2-Setup.exe",
             "browser_download_url": "https://dl/Setup.exe"},
            {"name": "HOKWorldScript-0.0.2-Setup.exe.sha256",
             "browser_download_url": "https://dl/Setup.exe.sha256"},
        ],
    }
    monkeypatch.setattr(updater, "_open",
                        lambda url, timeout=15: _Resp(json.dumps(rel).encode()))
    info = updater.fetch_latest()
    assert info.version == "0.0.2"
    assert info.has_installer
    assert info.installer_url == "https://dl/Setup.exe"
    assert info.sha256_url == "https://dl/Setup.exe.sha256"
    assert info.notes == "修了若干 bug"


def test_fetch_sha256_extracts_hex(monkeypatch):
    info = updater.ReleaseInfo(
        version="0.0.2", tag="v0.0.2", name="x", notes="",
        installer_url="u", installer_name="n",
        sha256_url="https://dl/Setup.exe.sha256", html_url="h")
    h = "a" * 64
    monkeypatch.setattr(updater, "_open",
                        lambda url, timeout=15: _Resp(f"{h}  HOKWorldScript-0.0.2-Setup.exe".encode()))
    assert updater.fetch_sha256(info) == h


# ---------- 下载:取消 ----------
def test_download_cancel_raises_and_cleans(monkeypatch, tmp_path):
    cancel = threading.Event()
    cancel.set()   # 一开始就取消
    monkeypatch.setattr(updater, "_open",
                        lambda url, timeout=30: _Resp(b"x" * 1000, {"Content-Length": "1000"}))
    dest = tmp_path / "out.bin"
    with pytest.raises(updater.CancelledError):
        updater.download("https://dl/x", dest, cancel=cancel)
    assert not (tmp_path / "out.bin.part").exists()   # 中断的临时文件已清理
    assert not dest.exists()


def test_download_writes_with_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_open",
                        lambda url, timeout=30: _Resp(b"abcde" * 100, {"Content-Length": "500"}))
    seen = []
    dest = tmp_path / "out.bin"
    out = updater.download("https://dl/x", dest, progress=lambda d, t: seen.append((d, t)))
    assert dest.read_bytes() == b"abcde" * 100
    assert seen and seen[-1][0] == 500 and seen[-1][1] == 500
