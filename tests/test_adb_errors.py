"""Regression tests: ADB error classification and shell quoting.

These test the REAL ``AdbClient`` (not FakeAdbClient) so we exercise the
actual ``_raise_for`` mapping and the canonical ``_posix_quote`` helper that
every ``adb shell`` path goes through.
"""

from __future__ import annotations

import pytest

from adb_music_sync.adb import AdbClient, CommandResult, _posix_quote
from adb_music_sync.errors import (
    AdbCommandError,
    DeviceDisconnectedError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
)

# -- error classification ---------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "exc_type"),
    [
        ("device unauthorized", DeviceUnauthorizedError),
        ("This adbd's $ADB_VENDOR_KEYS is not set", DeviceUnauthorizedError),
        ("device offline", DeviceOfflineError),
        ("error: device offline", DeviceOfflineError),
        ("no devices/emulators found", DeviceDisconnectedError),
        ("error: device 'X' not found", DeviceDisconnectedError),
        ("failed to get feature set: no devices", DeviceDisconnectedError),
        ("remote closed the connection", DeviceDisconnectedError),
        ("adb: error: unknown command", AdbCommandError),
    ],
)
def test_raise_for_classification(stderr, exc_type):
    c = AdbClient(adb_path="/fake/adb")
    r = CommandResult(returncode=1, stdout="", stderr=stderr)
    with pytest.raises(exc_type):
        c._raise_for(r)


def test_offline_not_misclassified_as_disconnect():
    """'device offline' must map to OfflineError, never DisconnectedError."""
    c = AdbClient(adb_path="/fake/adb")
    r = CommandResult(returncode=1, stdout="", stderr="error: device offline")
    with pytest.raises(DeviceOfflineError):
        c._raise_for(r)


def test_disconnect_is_separate_from_offline():
    c = AdbClient(adb_path="/fake/adb")
    r = CommandResult(returncode=1, stdout="", stderr="no devices/emulators found")
    with pytest.raises(DeviceDisconnectedError):
        c._raise_for(r)


# -- shell quoting ----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/storage/A12B-34CD/Music/Океан Ельзи/track.flac",
        "/storage/A12B-34CD/Music/It's My Life.flac",
        "/storage/A12B-34CD/Music/AC&DC/#1 Song.flac",
        "/storage/A12B-34CD/Music/[Album] (2026)/track.flac",
        "/storage/A12B-34CD/Music/日本語/音楽.flac",
        '/storage/A12B-34CD/Music/Some "Quoted" Album/song.flac',
        "/storage/A12B-34CD/Music/dollar$sign/tick`tock.flac",
    ],
)
def test_posix_quote_wraps_single_quotes(path):
    q = _posix_quote(path)
    # Always wrapped in single quotes so the android shell sees ONE argument.
    assert q.startswith("'") and q.endswith("'")
    # Embedded single quotes must be escaped with the POSIX '\\'' idiom.
    if "'" in path:
        assert "'\\''" in q


def test_posix_quote_embedded_single_quote():
    q = _posix_quote("It's My Life.flac")
    assert q == "'It'\\''s My Life.flac'"


def test_shell_command_shapes_use_quote_helper(monkeypatch):
    """shell_mkdir/mv/rm/touch/stat_size must route the path through
    _posix_quote so quotes/special chars survive ``adb shell``."""
    captured = {}

    def fake_run_checked(args, *, serial=None, target=None, timeout=60.0):
        captured["args"] = list(args)

    c = AdbClient(adb_path="/fake/adb")
    monkeypatch.setattr(c, "_run_checked", fake_run_checked)

    tricky = "/Music/It's & #1 (2026).flac"

    c.shell_mkdir(tricky)
    assert captured["args"] == ["shell", "mkdir", "-p", _posix_quote(tricky)]

    c.shell_mv(tricky, "/Music/done.flac")
    assert captured["args"] == [
        "shell",
        "mv",
        _posix_quote(tricky),
        _posix_quote("/Music/done.flac"),
    ]

    c.shell_rm(tricky)
    assert captured["args"] == ["shell", "rm", "-f", _posix_quote(tricky)]

    c.shell_touch(tricky)
    assert captured["args"] == ["shell", "touch", _posix_quote(tricky)]


def test_shell_stat_size_quotes_path(monkeypatch):
    captured = {}

    def fake_run(args, *, serial=None, target=None, timeout=60.0):
        captured["args"] = list(args)
        return CommandResult(0, "123\n", "")

    c = AdbClient(adb_path="/fake/adb")
    monkeypatch.setattr(c, "_run", fake_run)

    tricky = '/Music/It\'s "Title" & #1.flac'
    assert c.shell_stat_size(tricky) == 123
    # the whole script must go as a SINGLE arg after `shell`, with the path
    # single-quoted so Android's shell keeps it intact
    assert captured["args"] == ["shell", f"stat -c %s {_posix_quote(tricky)} 2>/dev/null"]
