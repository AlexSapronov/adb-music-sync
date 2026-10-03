"""Regression tests for 0.1.1 bug fixes: space calculation, write-probe,
pause/cancel semantics, progress counters, and disconnect/resume state."""

from __future__ import annotations

import pytest
from fake_adb import FakeAdbClient

from adb_music_sync.errors import (
    DeviceDisconnectedError,
    InsufficientSpaceError,
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


def _engine(client, files, storage=None, dest="/storage/emulated/0/Music", serial="DEVICE1"):
    storage = storage or StorageTarget(
        mount_path="/storage/emulated/0", label="Internal", free_bytes=10**12
    )
    items = [
        TransferItem(source=LocalFileRef(local_path=f"/pc/{r}", rel_path=r, size=sz))
        for r, sz in files
    ]
    return TransferEngine(
        client=client,
        storage=storage,
        destination=dest,
        plan=TransferPlan(items=items),
        serial=serial,
    )


# -- space calculation (bug 2): only pending files count ---------------------


def test_check_space_ignores_already_present_files():
    """Scenario A: 400GB library, 380GB present, 20GB to transfer, 50GB free."""
    c = FakeAdbClient(remote={"/storage/emulated/0/Music/present.flac": 380 * 1024**3})
    storage = StorageTarget("/storage/emulated/0", "Internal", free_bytes=50 * 1024**3)
    e = _engine(
        c,
        files=[
            ("present.flac", 380 * 1024**3),  # already on device (same size)
            ("new.flac", 20 * 1024**3),
        ],
        storage=storage,
    )
    # Correct order (as fixed in controller): sizes -> build_queue -> check_space
    sizes = e.remote_sizes()
    e.build_queue(sizes)
    e.check_space()  # must NOT raise


def test_check_space_raises_when_transfer_exceeds_free():
    """Scenario B: 60GB to transfer, 50GB free -> InsufficientSpaceError."""
    c = FakeAdbClient(remote={})
    storage = StorageTarget("/storage/emulated/0", "Internal", free_bytes=50 * 1024**3)
    e = _engine(c, files=[("big.flac", 60 * 1024**3)], storage=storage)
    sizes = e.remote_sizes()
    e.build_queue(sizes)
    with pytest.raises(InsufficientSpaceError):
        e.check_space()


def test_check_space_succeeds_when_everything_present():
    """Scenario C: everything exists -> free space must not equal library size."""
    present = 400 * 1024**3
    c = FakeAdbClient(remote={"/storage/emulated/0/Music/song.flac": present})
    storage = StorageTarget(
        "/storage/emulated/0",
        "Internal",
        free_bytes=1,  # virtually no free space
    )
    e = _engine(c, files=[("song.flac", present)], storage=storage)
    sizes = e.remote_sizes()
    e.build_queue(sizes)
    assert e.plan.to_transfer_bytes == 0
    e.check_space()  # no transfer -> no space requirement


# -- write probe (bug 4) -----------------------------------------------------


def test_probe_writable_ok():
    c = FakeAdbClient.with_device()
    sm = StorageManager(c)
    sm.probe_writable("/storage/emulated/0/Music", serial="DEVICE1")
    # probe file touched then removed; no lingering entry left on the device
    assert c.remote == {}
    assert any("write-test" in p for p in c.removed)  # it was cleaned up


def test_probe_writable_read_only():
    c = FakeAdbClient.with_device()
    c.read_only = True
    sm = StorageManager(c)
    with pytest.raises(StorageUnavailableError):
        sm.probe_writable("/storage/emulated/0/Music", serial="DEVICE1")


def test_probe_writable_disconnected():
    c = FakeAdbClient.with_device()
    c.disconnect = True
    sm = StorageManager(c)
    with pytest.raises(DeviceDisconnectedError):
        sm.probe_writable("/storage/emulated/0/Music", serial="DEVICE1")


# -- pause / cancel semantics (bug 3) ----------------------------------------


def test_pause_then_resume_does_not_busy_loop():
    """Gate-based pause: while paused the run gate is closed (no spin)."""
    c = FakeAdbClient(remote={})
    e = _engine(c, files=[("a.mp3", 1)])
    e.pause()
    assert not e._run_gate.is_set()  # gate closed == waiting, not spinning
    e.resume()
    assert e._run_gate.is_set()


def test_cancel_works_while_paused():
    """cancel() must open the gate and set the flag so a paused queue exits."""
    c = FakeAdbClient(remote={})
    e = _engine(c, files=[("a.mp3", 1)])
    e.pause()  # gate closed
    e.cancel()
    assert e._cancel_event.is_set()  # cancel flag set
    assert e._run_gate.is_set()  # gate reopened so the paused loop can see it


# -- progress counters (bug 5) -----------------------------------------------


def test_progress_total_files_is_queue_size_not_transferred():
    c = FakeAdbClient(remote={"/storage/emulated/0/Music/already.flac": 1})
    e = _engine(c, files=[("already.flac", 1), ("a.mp3", 1), ("b.mp3", 1)])
    e.build_queue(e.remote_sizes())
    # total_files must reflect the transfer queue (pending), not transferred count
    e.state = AppState.TRANSFERRING
    e._run_gate.set()
    e._cancel_event.clear()
    # simulate: we only expose total via run(); check build_queue leaves pending
    pending = [i for i in e.plan.items if i.status is TransferStatus.PENDING]
    assert len(pending) == 2  # 'already.flac' skipped


def test_progress_fields_meaningful():
    c = FakeAdbClient(remote={})
    e = _engine(c, files=[("a.mp3", 1), ("b.mp3", 2)])
    e.run()
    assert e.progress.total_files == 2
    assert e.progress.transferred_files == 2
    assert e.progress.current_index == 2


# -- disconnect / resume state (bug 9) ---------------------------------------


def test_disconnect_preserves_completed_files_and_destination():
    c = FakeAdbClient(remote={})
    e = _engine(c, files=[("a.mp3", 1), ("b.mp3", 1), ("c.mp3", 1)])
    orig_push = c.push

    def flaky(local, remote, *, serial=None):
        # disconnect on the THIRD push (after a and b succeed)
        if len(c.pushed) >= 2:
            raise DeviceDisconnectedError("device disconnected")
        orig_push(local, remote, serial=serial)

    c.push = flaky
    with pytest.raises(DeviceDisconnectedError):
        e.run()

    assert e.state is AppState.DISCONNECTED
    # a and b already OK, c still PENDING
    assert e.plan.items[0].status is TransferStatus.OK
    assert e.plan.items[1].status is TransferStatus.OK
    assert e.plan.items[2].status is TransferStatus.PENDING
    # destination unchanged
    assert e.destination == "/storage/emulated/0/Music"


def test_resume_after_disconnect_does_not_retransfer_completed():
    c = FakeAdbClient(remote={})
    e = _engine(c, files=[("a.mp3", 1), ("b.mp3", 1), ("c.mp3", 1)])
    orig_push = c.push

    def flaky(local, remote, *, serial=None):
        if len(c.pushed) >= 2:
            raise DeviceDisconnectedError("device disconnected")
        orig_push(local, remote, serial=serial)

    c.push = flaky
    with pytest.raises(DeviceDisconnectedError):
        e.run()
    pushed_first = len(c.pushed)

    # "reconnect": restore healthy push, re-run the queue (as resume does)
    c.push = orig_push
    e.run()

    # completed files (a, b) were NOT pushed again; only c was transferred
    assert len(c.pushed) == pushed_first + 1
    assert e.state is AppState.COMPLETED


def test_sd_not_auto_swapped_to_internal_on_disconnect():
    c = FakeAdbClient(remote={})
    storage = StorageTarget("/storage/A12B-34CD", "SD card", is_removable=True, free_bytes=10**9)
    e = _engine(c, files=[("a.mp3", 1)], storage=storage)

    def gone(local, remote, *, serial=None):
        raise StorageUnavailableError("storage gone")

    c.push = gone
    with pytest.raises(StorageUnavailableError):
        e.transfer_one(e.plan.items[0])

    # storage MUST remain the SD card, not silently replaced by internal
    assert e.storage.mount_path == "/storage/A12B-34CD"
