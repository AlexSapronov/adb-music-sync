"""Exercise duplicate ADB primitives through actual shell scripts on POSIX."""

import base64
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from adb_music_sync.adb import AdbClient
from adb_music_sync.duplicates import DuplicateManager
from adb_music_sync.errors import AdbCommandError
from adb_music_sync.models import AdbTarget


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX tools required"
)
def test_real_shell_quarantine_restore_and_purge(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    first = music / "01 O'Clock 日本 & # $.flac"
    second = music / "copy $(touch INJECTED).flac"
    first.write_bytes(b"same audio")
    second.write_bytes(first.read_bytes())
    playlist = music / "mix.m3u8"
    original_playlist = ("#EXTM3U\n" + second.name + "\n").encode("utf-8-sig")
    playlist.write_bytes(original_playlist)
    original_run = subprocess.run
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[:3] == ["adb", "-t", "9"]
        if cmd[3] == "push":
            return original_run(["cp", cmd[4], cmd[5]], **kwargs)
        assert cmd[3] in ("shell", "exec-out")
        return original_run(["sh", "-c", " ".join(cmd[4:])], **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    m = DuplicateManager(AdbClient("adb"), str(tmp_path), AdbTarget(transport_id=9))
    scan = m.scan("Music")
    assert len(scan.groups) == 1
    session = m.quarantine(scan, {scan.groups[0].digest: str(first)})
    assert not second.exists()
    assert first.exists()
    assert playlist.read_bytes() == original_playlist.replace(
        second.name.encode(), first.name.encode()
    )
    m.restore(session)
    assert second.read_bytes() == first.read_bytes()
    assert playlist.read_bytes() == original_playlist
    scan = m.scan("Music")
    session = m.quarantine(scan, {scan.groups[0].digest: str(first)})
    m.purge(session)
    assert first.exists() and not second.exists()
    assert not (tmp_path / "INJECTED").exists()
    assert calls


@pytest.mark.parametrize("bad", ["not a hash", "", "a" * 63, "z" * 64])
def test_hash_rejects_malformed_response(monkeypatch, bad):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=bad, stderr=""),
    )
    with pytest.raises(AdbCommandError):
        AdbClient("adb").sha256("/Music/file.mp3", target=AdbTarget(serial="device"))


def test_read_bytes_rejects_truncated_payload(monkeypatch):
    client = AdbClient("adb")
    monkeypatch.setattr(client, "shell_stat_size", lambda *args, **kwargs: 100)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=base64.b64encode(b"short").decode(), stderr=""
        ),
    )
    with pytest.raises(AdbCommandError):
        client.read_bytes("/Music/mix.m3u8", target=AdbTarget(serial="device"))
