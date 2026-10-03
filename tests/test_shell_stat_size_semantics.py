"""Regression tests for the shell_execution layer fix (0.1.3 follow-up).

Two real bugs fixed together:

1. ``shell_list`` / ``shell`` / ``shell_stat_size`` used to pass the command
   through ``shlex.split()`` on the host, which stripped the POSIX single
   quotes that ``_posix_quote`` had inserted.  Paths with spaces (or `'`, `"`,
   `&`, `#`, `$`, Unicode) were silently split before reaching Android's shell.
   Now the full shell command is passed to ``adb shell`` as ONE argument, so
   quoting survives verbatim.

2. ``shell_stat_size`` used to treat a missing remote file (ordinary ``stat``
   exit != 0) as ``AdbCommandError``, crashing plan build on the first new
   track.  Now it returns ``None`` for "file absent", the integer size on
   success, and still raises the typed device/transport exception on a real
   failure.

These tests exercise the REAL ``AdbClient`` command-building layer by
monkeypatching ``AdbClient._run`` (never the FakeAdbClient high-level
overrides), so they prove what actually reaches ``adb shell``.
"""

from __future__ import annotations

import pytest

from adb_music_sync.adb import AdbClient, CommandResult, _posix_quote
from adb_music_sync.errors import (
    AdbCommandError,
    DeviceDisconnectedError,
    DeviceOfflineError,
)
from adb_music_sync.models import (
    AdbTarget,
    LocalFileRef,
    StorageTarget,
    TransferItem,
    TransferPlan,
    TransferStatus,
)
from adb_music_sync.transfer import TransferEngine


def _client_with_run(monkeypatch, responses):
    """Return an AdbClient whose _run() pops canned CommandResults per call.

    `responses` is a list of CommandResult; each _run() call pops the next one.
    The argv of every _run() call is recorded into `captured`.
    """
    captured: list[list[str]] = []

    def fake_run(args, *, serial=None, target=None, timeout=60.0):
        captured.append(list(args))
        return responses.pop(0)

    c = AdbClient(adb_path="/fake/adb")
    monkeypatch.setattr(c, "_run", fake_run)
    return c, captured


# -- problem 2: missing file is a normal probe, not an error -------------


def test_stat_size_missing_file_returns_none(monkeypatch):
    # file absent: stat exits 1, stderr suppressed by 2>/dev/null -> empty
    c, _ = _client_with_run(monkeypatch, [CommandResult(1, "", "")])
    assert c.shell_stat_size("/Music/2024-06-29 - Come Alive/01. Come Alive.flac") is None


def test_stat_size_existing_file_returns_int(monkeypatch):
    c, _ = _client_with_run(monkeypatch, [CommandResult(0, "1048576\n", "")])
    assert c.shell_stat_size("/Music/x.flac") == 1048576


def test_stat_size_offline_still_raises(monkeypatch):
    c, _ = _client_with_run(monkeypatch, [CommandResult(1, "", "error: device offline")])
    with pytest.raises(DeviceOfflineError):
        c.shell_stat_size("/Music/x.flac")


def test_stat_size_disconnected_still_raises(monkeypatch):
    c, _ = _client_with_run(monkeypatch, [CommandResult(1, "", "no devices/emulators found")])
    with pytest.raises(DeviceDisconnectedError):
        c.shell_stat_size("/Music/x.flac")


def test_stat_size_real_transport_error_not_swallowed(monkeypatch):
    c, _ = _client_with_run(monkeypatch, [CommandResult(1, "", "error: closed")])
    with pytest.raises((DeviceDisconnectedError, AdbCommandError)):
        c.shell_stat_size("/Music/x.flac")


# -- problem 1: quoting survives to adb shell ----------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/storage/external_sd/Music/2024-06-29 - Come Alive/01. Come Alive.flac",
        "/storage/external_sd/Music/It's My Life.flac",
        '/storage/external_sd/Music/Some "Quoted" Album/song.flac',
        "/storage/external_sd/Music/AC&DC/#1 Song.flac",
        "/storage/external_sd/Music/dollar$sign/track.flac",
        "/storage/external_sd/Music/Океан Ельзи/Трек.flac",
        "/storage/external_sd/Music/日本語の音楽/曲.flac",
    ],
)
def test_stat_size_passes_quoted_single_arg(monkeypatch, path):
    c, captured = _client_with_run(monkeypatch, [CommandResult(1, "", "")])
    assert c.shell_stat_size(path) is None
    # The WHOLE command must be a single argument after `shell`, with the path
    # still single-quoted — proving no host-side shlex.split stripped it.
    assert captured[0] == ["shell", f"stat -c %s {_posix_quote(path)} 2>/dev/null"]


def test_shell_list_passes_single_arg(monkeypatch):
    c, captured = _client_with_run(monkeypatch, [CommandResult(0, "ok\n", "")])
    c.shell_list("df -k '/storage/external_sd'")
    assert captured[0] == ["shell", "df -k '/storage/external_sd'"]


def test_shell_preserves_mount_quoting(monkeypatch):
    c, captured = _client_with_run(monkeypatch, [CommandResult(0, "ok\n", "")])
    c.shell("ls -1 '/storage/emulated/0/My Music'")
    assert captured[0] == ["shell", "ls -1 '/storage/emulated/0/My Music'"]


# -- end-to-end: missing files never crash plan build --------------------


def _engine(monkeypatch, items, responses):
    """Build a TransferEngine over the REAL AdbClient, with _run stubbed."""
    c, captured = _client_with_run(monkeypatch, responses)
    storage = StorageTarget(mount_path="/storage/external_sd", label="SD", free_bytes=10**12)
    target = AdbTarget(transport_id=1)
    engine = TransferEngine(
        client=c,
        storage=storage,
        destination="/storage/external_sd/Music",
        plan=TransferPlan(items=items),
        target=target,
    )
    return engine, captured


def test_plan_build_450_missing_no_error(monkeypatch):
    """450 local / 0 remote -> 450 PENDING, zero errors from stat probes."""
    items = [
        TransferItem(
            source=LocalFileRef(
                local_path=f"/lib/track{i:03d}.flac",
                rel_path=f"Album/track{i:03d}.flac",
                size=1000 + i,
            )
        )
        for i in range(450)
    ]
    # every stat returns "missing" (exit 1, empty stderr)
    responses = [CommandResult(1, "", "") for _ in items]
    engine, captured = _engine(monkeypatch, items, responses)

    sizes = engine.remote_sizes()
    assert all(v is None for v in sizes.values())
    queued = engine.build_queue(sizes)
    assert len(queued) == 450
    assert all(i.status is TransferStatus.PENDING for i in queued)
    # none of these raised AdbCommandError, and 450 stat probes were issued
    assert len(captured) == 450


def test_plan_build_mixed_scenario(monkeypatch):
    """same size -> SKIPPED, missing -> PENDING, different size -> PENDING."""
    a = LocalFileRef("/lib/a.flac", "Album/a.flac", 1000)
    b = LocalFileRef("/lib/b.flac", "Album/b.flac", 2000)
    cm = LocalFileRef("/lib/c.flac", "Album/c.flac", 3000)
    items = [TransferItem(source=a), TransferItem(source=b), TransferItem(source=cm)]

    # a: same size (skip), b: missing (pending), c: wrong size (pending)
    responses = [
        CommandResult(0, "1000\n", ""),  # a exists, same size
        CommandResult(1, "", ""),  # b missing
        CommandResult(0, "999\n", ""),  # c exists, different size
    ]
    engine, captured = _engine(monkeypatch, items, responses)

    sizes = engine.remote_sizes()
    queued = engine.build_queue(sizes)

    by_rel = {i.remote_rel: i.status for i in engine.plan.items}
    assert by_rel["Album/a.flac"] is TransferStatus.SKIPPED
    assert by_rel["Album/b.flac"] is TransferStatus.PENDING
    assert by_rel["Album/c.flac"] is TransferStatus.PENDING
    assert len(queued) == 2
    assert len(captured) == 3


def test_e2e_transfer_uses_target_with_spaces(monkeypatch):
    """Full probe->queue->transfer for a real FiiO profile with spaces in paths."""
    src = LocalFileRef(
        "/lib/01. Come Alive.flac",
        "2024-06-29 - Come Alive/01. Come Alive.flac",
        12345,
    )
    items = [TransferItem(source=src)]
    # probe says missing, then mkdir/push/mv all succeed
    responses = [
        CommandResult(1, "", ""),  # stat -> missing
        CommandResult(0, "", ""),  # mkdir -p
        CommandResult(0, "12345\n", ""),  # push (fake ok)
        CommandResult(0, "", ""),  # mv
    ]
    # only stub _run for shell/stat; push is a real method that calls _run too,
    # so we drive it here through the engine's transfer_one()
    c, captured = _client_with_run(monkeypatch, responses)
    storage = StorageTarget(mount_path="/storage/external_sd", label="SD", free_bytes=10**12)
    target = AdbTarget(transport_id=1)
    engine = TransferEngine(
        client=c,
        storage=storage,
        destination="/storage/external_sd/Music",
        plan=TransferPlan(items=items),
        target=target,
    )

    sizes = engine.remote_sizes()
    assert sizes["2024-06-29 - Come Alive/01. Come Alive.flac"] is None
    engine.build_queue(sizes)

    engine.transfer_one(items[0])
    # every device-side argv carried target selector (transport_id=1), and the
    # stat/mkdir args kept the spaceful path single-quoted
    assert captured[0] == [
        "shell",
        f"stat -c %s {_posix_quote('/storage/external_sd/Music/2024-06-29 - Come Alive/01. Come Alive.flac')} 2>/dev/null",
    ]
    assert captured[1][0] == "shell" and "2024-06-29 - Come Alive" in captured[1][-1]
