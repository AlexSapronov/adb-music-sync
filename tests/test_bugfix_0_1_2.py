"""Regression tests for the 0.1.2 bug fixes.

1. write probe on a not-yet-existing destination (mkdir before touch).
2. DeviceOfflineError treated as a recoverable disconnect state.
3. .part cleanup does not mask disconnect/offline; rename only after success.
4. Frozen entry point uses absolute imports and a --smoke-test mode.
"""

from __future__ import annotations

import pytest
from fake_adb import FakeAdbClient

from adb_music_sync.errors import (
    DeviceDisconnectedError,
    DeviceOfflineError,
    StorageUnavailableError,
)
from adb_music_sync.models import (
    AppState,
    LocalFileRef,
    StorageTarget,
    TransferItem,
    TransferPlan,
    TransferStatus,
)
from adb_music_sync.storage import StorageManager
from adb_music_sync.transfer import TransferEngine


def _storage(mp="/storage/A12B-34CD"):
    return StorageTarget(
        mount_path=mp, label="SD", free_bytes=1_000_000, total_bytes=2_000_000, is_removable=True
    )


def _item(rel, size):
    return TransferItem(source=LocalFileRef(local_path=f"/local/{rel}", rel_path=rel, size=size))


def _engine(client, rels, storage=None):
    plan = TransferPlan(items=[_item(rel, sz) for rel, sz in rels])
    return TransferEngine(
        client=client,
        storage=storage or _storage(),
        destination="/storage/A12B-34CD/Music",
        plan=plan,
        serial="DEVICE1",
    )


# --------------------------------------------------------------------------- #
# Bug 1: write probe for a new destination
# --------------------------------------------------------------------------- #


def test_probe_writable_existing_destination():
    c = FakeAdbClient.with_device()
    sm = StorageManager(c)
    # destination already exists
    c.remote = {"/storage/A12B-34CD/Music": None}
    sm.probe_writable("/storage/A12B-34CD/Music", serial="DEVICE1")
    # destination dir survives, no probe file left behind
    assert "/storage/A12B-34CD/Music" in c.remote
    assert not any("write-test" in p for p in c.remote)


def test_probe_writable_creates_missing_destination():
    c = FakeAdbClient.with_device()
    sm = StorageManager(c)
    # destination does NOT exist -> mkdir issued before touch, then probe succeeds
    sm.probe_writable("/storage/A12B-34CD/Music/My Library", serial="DEVICE1")
    assert "/storage/A12B-34CD/Music/My Library" in c.mkdirs
    # destination directory survives (never removed), probe file cleaned up
    assert not any("write-test" in p for p in c.remote)


def test_probe_writable_read_only_raises_storage_unavailable():
    c = FakeAdbClient.with_device()
    c.read_only = True
    sm = StorageManager(c)
    with pytest.raises(StorageUnavailableError):
        sm.probe_writable("/storage/A12B-34CD/Music", serial="DEVICE1")


def test_probe_writable_disconnected_raises_disconnected():
    c = FakeAdbClient.with_device()
    c.disconnect = True
    sm = StorageManager(c)
    with pytest.raises(DeviceDisconnectedError):
        sm.probe_writable("/storage/A12B-34CD/Music", serial="DEVICE1")


def test_probe_writable_offline_raises_offline():
    c = FakeAdbClient.with_device()
    c.offline = True
    sm = StorageManager(c)
    with pytest.raises(DeviceOfflineError):
        sm.probe_writable("/storage/A12B-34CD/Music", serial="DEVICE1")


# --------------------------------------------------------------------------- #
# Bug 3: DeviceOfflineError is a recoverable disconnect state
# --------------------------------------------------------------------------- #


def test_offline_sets_disconnected_and_keeps_completed():
    c = FakeAdbClient.with_device()
    c.remote = {}
    e = _engine(c, [("a.mp3", 10), ("b.mp3", 20)])
    e.build_queue({"a.mp3": None, "b.mp3": None})

    # first push ok, second hits offline
    state = {"n": 0}

    def fake_push(local, remote, *, serial=None):
        state["n"] += 1
        if state["n"] == 2:
            raise DeviceOfflineError("device offline")
        c.remote[remote] = 10
        return None

    orig = c.push
    c.push = fake_push
    try:
        with pytest.raises(DeviceOfflineError):
            e.run()
    finally:
        c.push = orig

    assert e.state is AppState.DISCONNECTED
    assert e.plan.items[0].status is TransferStatus.OK
    assert e.plan.items[1].status is TransferStatus.PENDING
    assert e._error_count() == 0


def test_offline_resume_completes_without_resending():
    c = FakeAdbClient.with_device()
    c.remote = {}
    e = _engine(c, [("a.mp3", 10), ("b.mp3", 20)])
    e.build_queue({"a.mp3": None, "b.mp3": None})

    pushes = {"a": 0, "b": 0}

    def flaky_push(local, remote, *, serial=None):
        if remote.endswith("a.mp3.part"):
            pushes["a"] += 1
        if remote.endswith("b.mp3.part"):
            pushes["b"] += 1
            if pushes["b"] == 1:
                raise DeviceOfflineError("device offline")
        c.remote[remote] = 20
        return None

    def ok_push(local, remote, *, serial=None):
        if remote.endswith("a.mp3.part"):
            pushes["a"] += 1
        if remote.endswith("b.mp3.part"):
            pushes["b"] += 1
        c.remote[remote] = 20
        return None

    orig = c.push
    c.push = flaky_push
    try:
        with pytest.raises(DeviceOfflineError):
            e.run()
    finally:
        c.push = orig

    # "reconnect": push now succeeds, complete the remaining item
    c.push = ok_push
    try:
        e.resume()
        e.run()
    finally:
        c.push = orig

    assert e.state is AppState.COMPLETED
    assert e.plan.items[0].status is TransferStatus.OK
    assert e.plan.items[1].status is TransferStatus.OK
    # a.mp3 (completed) was pushed exactly once, never re-sent after reconnect
    assert pushes["a"] == 1
    assert pushes["b"] == 2


# --------------------------------------------------------------------------- #
# Bug 4: .part cleanup does not mask disconnect/offline
# --------------------------------------------------------------------------- #


def test_cleanup_part_error_does_not_mask_original_exception():
    c = FakeAdbClient.with_device()
    e = _engine(c, [("a.mp3", 10)])
    e.build_queue({"a.mp3": None})

    orig_push = c.push
    orig_rm = c.shell_rm

    def bad_push(local, remote, *, serial=None):
        raise DeviceDisconnectedError("device disconnected")

    def bad_rm(path, *, serial=None):
        raise DeviceDisconnectedError("still disconnected")

    c.push = bad_push
    c.shell_rm = bad_rm
    try:
        with pytest.raises(DeviceDisconnectedError) as ei:
            e.transfer_one(e.plan.items[0])
    finally:
        c.push = orig_push
        c.shell_rm = orig_rm

    assert "disconnected" in str(ei.value)
    assert e.plan.items[0].status is TransferStatus.PENDING


def test_transfer_one_renames_only_after_success():
    c = FakeAdbClient.with_device()
    e = _engine(c, [("a.mp3", 10)])
    e.build_queue({"a.mp3": None})

    mv_calls = []

    def record_mv(src, dst, *, serial=None):
        mv_calls.append((src, dst))
        if src in c.remote:
            c.remote[dst] = c.remote.pop(src)

    orig_mv = c.shell_mv
    c.shell_mv = record_mv
    try:
        e.transfer_one(e.plan.items[0])
    finally:
        c.shell_mv = orig_mv

    assert mv_calls == [("/storage/A12B-34CD/Music/a.mp3.part", "/storage/A12B-34CD/Music/a.mp3")]
    assert e.plan.items[0].status is TransferStatus.OK


def test_stale_part_overwritten_on_resume():
    """A leftover .part is not treated as complete and is overwritten on resume."""
    c = FakeAdbClient.with_device()
    # simulate stale .part already on device (leftover from a prior disconnect)
    c.remote = {"/storage/A12B-34CD/Music/a.mp3.part": 0}
    e = _engine(c, [("a.mp3", 10)])
    e.build_queue({"a.mp3": None})  # final a.mp3 not present -> PENDING

    assert e.plan.items[0].status is TransferStatus.PENDING

    e.resume()
    e.run()

    assert e.state is AppState.COMPLETED
    assert e.plan.items[0].status is TransferStatus.OK


# --------------------------------------------------------------------------- #
# Bug 5 (packaging): entry point uses absolute imports + --smoke-test
# --------------------------------------------------------------------------- #


def test_main_smoke_test_returns_zero(monkeypatch):
    import adb_music_sync.__main__ as m

    monkeypatch.setattr("sys.argv", ["adb-music-sync", "--smoke-test"])
    assert m.main() == 0


def test_entry_point_uses_absolute_import_only():
    import ast
    import pathlib

    src = pathlib.Path("src/adb_music_sync/__main__.py").read_text()
    tree = ast.parse(src)
    relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0]
    # no relative imports anywhere (level > 0), and the GUI run is imported absolutely
    assert relative == []
    assert "from adb_music_sync.gui import run" in src
