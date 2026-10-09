"""End-to-end duplicate operations with a byte-accurate fake device."""

from __future__ import annotations

import hashlib
import json
import posixpath
import threading

import pytest

from adb_music_sync.duplicates import DuplicateManager, OperationCancelled, rewrite_playlist
from adb_music_sync.models import AdbTarget
from adb_music_sync.scanner import is_supported

STORAGE = "/storage/external_sd"
MUSIC = STORAGE + "/Music"
A = MUSIC + "/Artist/01 O'Clock 日本.flac"
B = MUSIC + "/Collection/Copy # & $.flac"
C = MUSIC + "/Third/copy.flac"
PLAYLIST = MUSIC + "/ChatGPT_Playlists/mix.m3u8"
TARGET = AdbTarget(transport_id=4)
BEFORE = (
    "#EXTM3U\r\n#EXTINF:-1,Artist — song\r\n../Collection/Copy # & $.flac\r\n../Artist/01 O'Clock 日本.flac\r\n"
).encode("utf-8-sig")


class ByteDevice:
    def __init__(self, files=None):
        self.files = dict(files or {A: b"music bytes", B: b"music bytes", PLAYLIST: BEFORE})
        self.dirs = {STORAGE, MUSIC}
        self.moves = []
        self.writes = []
        self.removals = []
        self.hashes = []
        self.fail_move = None
        self.aliases = {}
        self.targets = []
        for path in self.files:
            self._dirs(path)

    def _dirs(self, path):
        parent = posixpath.dirname(path)
        while parent and parent != "/":
            self.dirs.add(parent)
            parent = posixpath.dirname(parent)

    def list_files(self, root, *, target):
        self.targets.append(target)
        return sorted(p for p in self.files if p.startswith(root + "/"))

    def list_audio_files(self, root, *, target):
        return [p for p in self.list_files(root, target=target) if is_supported(p)]

    def shell_stat_sizes(self, paths, *, target):
        self.targets.append(target)
        return {p: len(self.files[p]) if p in self.files else None for p in paths}

    def canonical_path(self, path, *, target):
        self.targets.append(target)
        for prefix, dest in self.aliases.items():
            if path == prefix or path.startswith(prefix + "/"):
                return dest + path[len(prefix) :]
        return path

    def sha256(self, path, *, target):
        self.targets.append(target)
        self.hashes.append(path)
        return hashlib.sha256(self.files[path]).hexdigest()

    def read_bytes(self, path, *, target, **kwargs):
        self.targets.append(target)
        return self.files[path]

    def write_bytes(self, path, data, *, target):
        self.targets.append(target)
        self._dirs(path)
        self.files[path] = data
        self.writes.append(path)

    def path_exists(self, path, *, target):
        self.targets.append(target)
        return path in self.files or path in self.dirs

    def shell_mkdir(self, path, *, target):
        self.targets.append(target)
        self._dirs(path + "/placeholder")

    def move_no_replace(self, source, destination, *, target):
        self.targets.append(target)
        if self.fail_move is not None and len(self.moves) == self.fail_move:
            raise ConnectionError("USB disconnected")
        if self.path_exists(destination, target=target):
            raise ValueError("overwrite")
        self.files[destination] = self.files.pop(source)
        self.moves.append((source, destination))
        self._dirs(destination)

    def shell_rm(self, path, *, target):
        self.targets.append(target)
        self.removals.append(path)
        self.files.pop(path, None)


def manager(device):
    return DuplicateManager(device, STORAGE, TARGET)


def clean(device):
    m = manager(device)
    scan = m.scan("Music")
    session = m.quarantine(scan, {scan.groups[0].digest: A})
    return m, session


def test_scan_only_hashes_equal_sizes_and_ignores_different_audio():
    device = ByteDevice(
        {
            A: b"abc",
            B: b"abc",
            C: b"def",
            MUSIC + "/unique.mp3": b"different length",
            MUSIC + "/cover.jpg": b"abc",
        }
    )
    scan = manager(device).scan("Music")
    assert scan.file_count == 4
    assert scan.hashed_count == 3
    assert scan.groups[0].paths == tuple(sorted((A, B)))
    assert scan.redundant_bytes == 3
    assert set(device.hashes) == {A, B, C}
    assert all(t == TARGET for t in device.targets)
    assert device.moves == device.writes == device.removals == []


def test_round_trip_updates_playlist_and_restores_exact_bytes_after_restart():
    device = ByteDevice()
    original = dict(device.files)
    m, session = clean(device)
    assert A in device.files and B not in device.files
    assert device.files[PLAYLIST] == BEFORE.replace(
        b"../Collection/Copy # & $.flac", "../Artist/01 O'Clock 日本.flac".encode()
    )
    assert sum(len(data) for p, data in device.files.items() if is_supported(p)) == sum(
        len(data) for p, data in original.items() if is_supported(p)
    )
    assert m.sessions()[0][1] == "quarantined"
    manager(device).restore(session)
    assert {p: device.files[p] for p in original} == original
    assert manager(device).sessions() == []
    assert device.removals == []


def test_purge_checks_kept_copy_and_deletes_only_quarantined_duplicate():
    device = ByteDevice()
    m, session = clean(device)
    m.purge(session)
    assert device.files[A] == b"music bytes"
    assert len(device.removals) == 1
    assert device.removals[0].startswith(session + "/files/")
    assert B not in device.files
    assert m.sessions() == []
    with pytest.raises(ValueError):
        m.restore(session)


def test_changed_keep_blocks_purge():
    device = ByteDevice()
    m, session = clean(device)
    device.files[A] = b"edited"
    with pytest.raises(ValueError):
        m.purge(session)
    assert device.removals == []


def test_changed_after_scan_blocks_cleanup_without_moves_or_writes():
    device = ByteDevice()
    m = manager(device)
    scan = m.scan("Music")
    device.files[B] = b"changed"
    with pytest.raises(ValueError):
        m.quarantine(scan, {scan.groups[0].digest: A})
    assert device.moves == device.writes == []


def test_disconnect_midway_leaves_journal_and_restart_can_restore():
    device = ByteDevice({A: b"same", B: b"same", C: b"same", PLAYLIST: BEFORE})
    original = dict(device.files)
    device.fail_move = 1
    m = manager(device)
    scan = m.scan("Music")
    with pytest.raises(ConnectionError):
        m.quarantine(scan, {scan.groups[0].digest: A})
    sessions = manager(device).sessions()
    assert sessions[0][1] == "prepared"
    device.fail_move = None
    manager(device).restore(sessions[0][0])
    assert {p: device.files[p] for p in original} == original


@pytest.mark.parametrize("conflict", ["original", "playlist"])
def test_restore_conflicts_never_overwrite(conflict):
    device = ByteDevice()
    m, session = clean(device)
    path = B if conflict == "original" else PLAYLIST
    device.files[path] = b"my new content"
    moves_before = list(device.moves)
    writes_before = list(device.writes)
    with pytest.raises(ValueError):
        m.restore(session)
    assert device.files[path] == b"my new content"
    assert device.moves == moves_before
    assert device.writes == writes_before


def test_cancel_mid_cleanup_is_recoverable():
    device = ByteDevice()
    m = manager(device)
    scan = m.scan("Music")
    cancel = threading.Event()

    def progress(message):
        if message.startswith("В карантин:"):
            cancel.set()

    # Cancellation set during progress is observed at next operation boundary.
    with pytest.raises(OperationCancelled):
        m.quarantine(scan, {scan.groups[0].digest: A}, progress=progress, cancel=cancel)
    session = m.sessions()[0][0]
    manager(device).restore(session)
    assert A in device.files and B in device.files
    assert device.files[PLAYLIST] == BEFORE


def test_malformed_manifest_escape_is_rejected_before_mutation():
    device = ByteDevice()
    m, session = clean(device)
    manifest = session + "/manifest.json"
    journal = json.loads(device.files[manifest])
    journal["entries"][0]["original"] = "/system/file.mp3"
    device.files[manifest] = json.dumps(journal).encode()
    writes_before = list(device.writes)
    with pytest.raises(ValueError):
        m.restore(session)
    assert device.writes == writes_before


def test_symlink_escape_and_root_cleanup_rejected():
    device = ByteDevice()
    device.aliases[MUSIC + "/Collection"] = "/system"
    with pytest.raises(ValueError):
        manager(device).scan("Music")
    device.aliases = {}
    m = manager(device)
    scan = m.scan(STORAGE)
    with pytest.raises(ValueError):
        m.quarantine(scan, {scan.groups[0].digest: A})
    assert device.moves == device.writes == []


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_playlist_keeps_comments_eols_order_and_encoding(encoding):
    text = (
        "#EXTM3U\r\n#EXTINF:-1,Keep label\r\n../Collection/Copy # & $.flac\r\n" + A + "\nother.mp3"
    )
    result = rewrite_playlist(text.encode(encoding), PLAYLIST, {B: A})
    expected = text.replace("../Collection/Copy # & $.flac", "../Artist/01 O'Clock 日本.flac")
    assert result.decode(encoding) == expected
    assert result == expected.encode(encoding)


def test_legacy_playlist_blocks_cleanup_before_moving_audio():
    device = ByteDevice()
    device.files[PLAYLIST] = "Русская строка".encode("cp1251")
    m = manager(device)
    scan = m.scan("Music")
    with pytest.raises(ValueError):
        m.quarantine(scan, {scan.groups[0].digest: A})
    assert device.moves == device.writes == []


def test_unchecked_groups_do_nothing():
    device = ByteDevice()
    m = manager(device)
    with pytest.raises(ValueError):
        m.quarantine(m.scan("Music"), {})
    assert device.moves == device.writes == []


def test_scan_cancel_and_missing_files_fail_without_writes():
    device = ByteDevice()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        manager(device).scan("Music", cancel=cancel)
    assert not device.hashes
    assert not device.writes


def test_interrupted_restore_can_resume_without_overwriting():
    device = ByteDevice({A: b"same", B: b"same", C: b"same", PLAYLIST: BEFORE})
    m, session = clean(device)
    device.fail_move = len(device.moves) + 1
    with pytest.raises(ConnectionError):
        m.restore(session)
    assert m.sessions()[0][1] == "restoring"
    device.fail_move = None
    manager(device).restore(session)
    assert A in device.files and B in device.files and C in device.files
    assert device.files[PLAYLIST] == BEFORE


def test_interrupted_purge_can_resume_only_remaining_files(monkeypatch):
    device = ByteDevice({A: b"same", B: b"same", C: b"same", PLAYLIST: BEFORE})
    m, session = clean(device)
    original_rm = device.shell_rm
    calls = []

    def rm(path, **kwargs):
        if calls:
            raise ConnectionError("USB disconnected")
        calls.append(path)
        original_rm(path, **kwargs)

    monkeypatch.setattr(device, "shell_rm", rm)
    with pytest.raises(ConnectionError):
        m.purge(session)
    assert m.sessions()[0][1] == "purging"
    with pytest.raises(ValueError):
        m.restore(session)
    monkeypatch.setattr(device, "shell_rm", original_rm)
    m.purge(session)
    assert len(device.removals) == 2
    assert A in device.files


def test_missing_kept_file_blocks_purge():
    device = ByteDevice()
    m, session = clean(device)
    del device.files[A]
    with pytest.raises((ValueError, KeyError)):
        m.purge(session)
    assert not device.removals


def test_utf16_big_endian_playlist_keeps_bom():
    text = "#EXTM3U\r\n" + B + "\r\n"
    data = b"\xfe\xff" + text.encode("utf-16-be")
    assert rewrite_playlist(data, PLAYLIST, {B: A}) == b"\xfe\xff" + text.replace(B, A).encode(
        "utf-16-be"
    )


def test_real_maintenance_worker_releases_busy_on_success_and_failure():
    import time

    from PySide6.QtCore import QCoreApplication

    from adb_music_sync.controller import Controller
    from adb_music_sync.models import AppState

    app = QCoreApplication.instance() or QCoreApplication([])
    ctrl = Controller()
    ctrl.state = AppState.READY
    engine = object()
    ctrl.engine = engine
    results, errors, busy = [], [], []
    ctrl.maintenance_failed.connect(errors.append)
    ctrl.maintenance_busy_changed.connect(busy.append)

    def wait_worker():
        deadline = time.monotonic() + 2
        while ctrl._worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert ctrl._worker is None

    assert ctrl.run_maintenance(lambda: 42, results.append)
    assert ctrl.maintenance_busy
    ctrl.scan_library_async("not scanned")
    assert ctrl.state is AppState.READY
    wait_worker()
    assert results == [42]
    assert busy == [True, False]
    assert not ctrl.maintenance_busy

    def fail():
        raise ConnectionError("USB disconnected")

    assert ctrl.run_maintenance(fail, results.append)
    wait_worker()
    assert errors == ["USB disconnected"]
    assert ctrl.engine is engine
    assert not ctrl.maintenance_busy
    assert ctrl.state is AppState.READY
    ctrl.state = AppState.PAUSED
    assert not ctrl.run_maintenance(lambda: 0, results.append)
    ctrl.engine = None
    ctrl.shutdown()
