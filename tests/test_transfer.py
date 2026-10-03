"""Integration tests for the transfer engine with FakeAdbClient."""

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
from adb_music_sync.transfer import TransferEngine


def _engine(client, files=None, storage=None, dest="/storage/emulated/0/Music", serial="DEVICE1"):
    storage = storage or StorageTarget(
        mount_path="/storage/emulated/0", label="Internal", free_bytes=10**9
    )
    items = [
        TransferItem(source=LocalFileRef(local_path=f"/pc/{r}", rel_path=r, size=sz))
        for r, sz in (files or [])
    ]
    return TransferEngine(
        client=client,
        storage=storage,
        destination=dest,
        plan=TransferPlan(items=items),
        serial=serial,
    )


def test_build_queue_skips_same_size():
    c = FakeAdbClient(remote={"/storage/emulated/0/Music/a.mp3": 100})
    e = _engine(c, files=[("a.mp3", 100), ("b.mp3", 200)])
    queued = e.build_queue(e.remote_sizes())
    assert len(queued) == 1
    assert queued[0].remote_rel == "b.mp3"
    # 'a' marked skipped
    a = next(i for i in e.plan.items if i.remote_rel == "a.mp3")
    assert a.status is TransferStatus.SKIPPED


def test_build_queue_requeues_wrong_size():
    c = FakeAdbClient(remote={"/storage/emulated/0/Music/a.mp3": 99})
    e = _engine(c, files=[("a.mp3", 100)])
    queued = e.build_queue(e.remote_sizes())
    assert len(queued) == 1
    assert queued[0].status is TransferStatus.PENDING


def test_insufficient_space():
    c = FakeAdbClient(remote={})
    storage = StorageTarget("/storage/emulated/0", "Internal", free_bytes=10)
    e = _engine(c, files=[("a.mp3", 1000)], storage=storage)
    with pytest.raises(InsufficientSpaceError):
        e.check_space()


def test_transfer_uses_part_and_rename():
    c = FakeAdbClient(remote={})
    e = _engine(c, files=[("Artist/a.mp3", 5)])
    e.transfer_one(e.plan.items[0])
    # pushed to .part then moved
    assert c.pushed == [("/pc/Artist/a.mp3", "/storage/emulated/0/Music/Artist/a.mp3.part")]
    assert (
        "/storage/emulated/0/Music/Artist/a.mp3.part",
        "/storage/emulated/0/Music/Artist/a.mp3",
    ) in c.moves
    assert e.plan.items[0].status is TransferStatus.OK


def test_per_file_error_queue_continues():
    c = FakeAdbClient(remote={})
    c.fail_next_push = True
    e = _engine(c, files=[("a.mp3", 1), ("b.mp3", 2), ("c.mp3", 3)])
    # manually process both via run()
    e.run()
    # a failed first, b and c transferred
    assert e.plan.items[0].status is TransferStatus.ERROR
    assert e.plan.items[1].status is TransferStatus.OK
    assert e.plan.items[2].status is TransferStatus.OK
    assert e.state in (AppState.FAILED,)


def test_completed_files_not_retransferred():
    c = FakeAdbClient(remote={})
    c.fail_next_push = False
    e = _engine(c, files=[("a.mp3", 1)])
    e.run()
    assert e.state is AppState.COMPLETED
    done = c.pushed[:]
    # a manual "retry errors" with no errors should not push again
    e.retry_errors() if hasattr(e, "retry_errors") else None
    # re-run should skip since statuses are OK (no PENDING)
    e.run()
    assert len(c.pushed) == len(done)


def test_disconnect_during_transfer():
    c = FakeAdbClient(remote={})
    e = _engine(c, files=[("a.mp3", 1), ("b.mp3", 1)])
    # disconnect on second push
    orig_push = c.push

    def flaky(local, remote, *, serial=None):
        if len(c.pushed) >= 1:
            raise DeviceDisconnectedError("device offline")
        orig_push(local, remote, serial=serial)

    c.push = flaky
    with pytest.raises(DeviceDisconnectedError):
        e.run()
    assert e.state is AppState.DISCONNECTED


def test_sd_disappears_no_fallback():
    c = FakeAdbClient(remote={})
    storage = StorageTarget("/storage/A12B-34CD", "SD card", is_removable=True, free_bytes=10**9)
    e = _engine(c, files=[("a.mp3", 1)], storage=storage)

    # simulate SD vanishing: stat/push raise StorageUnavailableError
    def gone(local, remote, *, serial=None):
        raise StorageUnavailableError(f"storage {remote} gone")

    c.push = gone
    with pytest.raises(StorageUnavailableError):
        e.transfer_one(e.plan.items[0])
    # must NOT have switched destination to internal
    assert e.storage.mount_path == "/storage/A12B-34CD"


def test_state_machine_transitions():
    c = FakeAdbClient(remote={})
    e = _engine(c, files=[("a.mp3", 1)])
    assert e.state is AppState.READY
    e.run()
    assert e.state is AppState.COMPLETED


def test_pause_resume():
    c = FakeAdbClient(remote={})
    e = _engine(c, files=[("a.mp3", 1)])
    e.pause()
    assert e._pause_event.is_set()
    e.resume()
    assert not e._pause_event.is_set()
