"""Catalog export uses real file paths without downloading or modifying audio."""

import json
import posixpath
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fake_adb import FakeAdbClient

from adb_music_sync.adb import AdbClient
from adb_music_sync.catalog import export_catalog
from adb_music_sync.errors import AdbCommandError, InvalidDestinationError
from adb_music_sync.models import AdbTarget, Device, DeviceState

TARGET = AdbTarget(transport_id=7)
ROOT = "/storage/external_sd"
MUSIC = ROOT + "/Music"


def test_export_unicode_and_poweramp_paths(tmp_path, monkeypatch):
    paths = [MUSIC + "/日本語/Альбом/01 Love & 'Music' #1.FLAC", MUSIC + "/Song.mp3"]
    client = AdbClient(adb_path="adb")
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "\0".join(
                    paths
                    + [
                        MUSIC + "/cover.jpg",
                        MUSIC + "/Song.mp3.part",
                        MUSIC + "/old.m3u8",
                        paths[0],
                    ]
                )
                + "\0"
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", run)
    output = tmp_path / "catalog.json"
    data = export_catalog(client, ROOT, "Music", output, target=TARGET)
    assert data["track_count"] == 2
    assert json.loads(output.read_text(encoding="utf-8")) == data
    assert "日本語" in output.read_text(encoding="utf-8")
    for track in data["tracks"]:
        assert (
            posixpath.normpath(posixpath.join(data["playlist_directory"], track["playlist_path"]))
            == track["path"]
        )
        assert track["m3u_compatible"]
    assert len(calls) == 1
    assert calls[0][0][:4] == ["adb", "-t", "7", "exec-out"]
    assert calls[0][1]["text"] is False
    assert data["metadata_source"] == "file_and_folder_names_only"


def test_quoted_root_and_linebreak_names(tmp_path, monkeypatch):
    client = AdbClient(adb_path="adb")
    folder = "Music ' & $ ` 日本"
    path = ROOT + "/" + folder + "/odd\r\nname.mp3"
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout=(path + "\0").encode(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    data = export_catalog(client, ROOT, folder, tmp_path / "catalog.json", target=TARGET)
    assert "'\\''" in commands[0]
    assert data["tracks"][0]["path"] == path
    assert not data["tracks"][0]["m3u_compatible"]


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX shell required"
)
def test_actual_find_preserves_names(tmp_path, monkeypatch):
    music = tmp_path / "Music ' & 日本"
    music.mkdir()
    for name in ["Альбом\r\n01.flac", "01 # $ `.mp3", "cover.jpg"]:
        (music / name).touch()
    client = AdbClient(adb_path="adb")
    original_run = subprocess.run

    def run(cmd, **kwargs):
        return original_run(["sh", "-c", cmd[-1]], **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = client.list_audio_files(str(music), target=TARGET)
    assert result == sorted([str(music / "Альбом\r\n01.flac"), str(music / "01 # $ `.mp3")])


@pytest.mark.parametrize("mode", ["failure", "outside", "invalid_utf8"])
def test_failed_export_keeps_previous_file(tmp_path, monkeypatch, mode):
    output = tmp_path / "catalog.json"
    output.write_text("previous", encoding="utf-8")
    client = AdbClient(adb_path="adb")

    def run(cmd, **kwargs):
        if mode == "failure":
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"Permission denied")
        raw = b"/other/song.mp3\0" if mode == "outside" else b"/bad/\xff.mp3\0"
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises((AdbCommandError, ValueError, UnicodeDecodeError)):
        export_catalog(client, ROOT, "Music", output, target=TARGET)
    assert output.read_text() == "previous"
    assert list(tmp_path.glob("*.tmp")) == []


def test_empty_catalog_and_invalid_destination(tmp_path, monkeypatch):
    client = FakeAdbClient.with_device()
    monkeypatch.setattr(client, "list_audio_files", lambda *args, **kwargs: [])
    output = tmp_path / "empty.json"
    assert export_catalog(client, ROOT, "Music", output, target=TARGET)["track_count"] == 0
    with pytest.raises(InvalidDestinationError):
        export_catalog(client, ROOT, "../system", output, target=TARGET)
    assert client.pushed == client.mkdirs == client.removed == []


def test_controller_export_without_pc_scan_and_preserves_state(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication

    from adb_music_sync.controller import Controller
    from adb_music_sync.models import AppState, StorageTarget

    app = QCoreApplication.instance() or QCoreApplication([])
    ctrl = Controller()
    ctrl.client = FakeAdbClient.with_device()
    ctrl.selected_device = Device(serial="?", state=DeviceState.DEVICE, transport_id=7)
    ctrl.selected_storage = StorageTarget(mount_path=ROOT, label="SD", is_removable=True)
    ctrl.state = AppState.READY
    engine = object()
    ctrl.engine = engine
    calls = []
    monkeypatch.setattr(
        ctrl.client,
        "list_audio_files",
        lambda root, **kwargs: calls.append((root, kwargs)) or [MUSIC + "/Song.mp3"],
    )
    monkeypatch.setattr(ctrl, "_run_background", lambda fn, done: done(fn()))
    events = []
    ctrl.catalog_exported.connect(lambda path, count: events.append((path, count)))
    output = str(tmp_path / "catalog.json")
    ctrl.export_catalog_async("Music", output)
    assert events == [(output, 1)]
    assert calls[0][1]["target"].transport_id == 7
    assert ctrl.scan_result is None
    assert ctrl.state is AppState.READY
    assert ctrl.engine is engine
    ctrl._on_worker_finished(None)
    assert not ctrl.catalog_busy
    assert app is not None


def test_controller_rejects_busy_export(monkeypatch):
    from PySide6.QtCore import QCoreApplication

    from adb_music_sync.controller import Controller
    from adb_music_sync.models import AppState

    app = QCoreApplication.instance() or QCoreApplication([])
    ctrl = Controller()
    ctrl.state = AppState.PAUSED
    messages = []
    ctrl.catalog_failed.connect(messages.append)
    monkeypatch.setattr(ctrl, "_run_background", lambda *args: pytest.fail("Worker must not start"))
    ctrl.export_catalog_async("Music", "unused.json")
    assert messages
    assert not ctrl.catalog_busy
    assert app is not None
