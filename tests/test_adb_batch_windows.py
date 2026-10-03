"""Exercise actual subprocess arguments and run batch scripts in a POSIX shell."""

import subprocess
from types import SimpleNamespace

import pytest

import adb_music_sync.adb as adb_module
from adb_music_sync.adb import AdbClient
from adb_music_sync.errors import (
    AdbCommandError,
    DeviceDisconnectedError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
)
from adb_music_sync.models import AdbTarget


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_all_adb_subprocesses_hide_windows_console(monkeypatch, platform):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adb_module.sys, "platform", platform)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    client = AdbClient("adb.exe")
    client._run(["devices", "-l"])
    client.push("local", "/Music/remote", target=AdbTarget(transport_id=1))
    assert calls[1][0][:3] == ["adb.exe", "-t", "1"]
    for _, kwargs in calls:
        assert kwargs["creationflags"] == (0x08000000 if platform == "win32" else 0)
        assert kwargs.get("shell", False) is False
        assert kwargs["capture_output"] is True


@pytest.mark.skipif(adb_module.sys.platform == "win32", reason="uses POSIX sh/stat")
def test_batch_scripts_execute_with_special_filenames(tmp_path, monkeypatch):
    names = [
        "Come Alive.flac",
        "It's My Life.flac",
        'Some "Quoted" Track.flac',
        "AC&DC #1 $track.flac",
        "Океан Ельзи.flac",
        "日本語.flac",
        "line\nbreak.flac",
        "$(touch INJECTED).flac",
        "`touch INJECTED`.flac",
    ]
    expected = {}
    for i, name in enumerate(names):
        path = tmp_path / name
        path.write_bytes(b"a" * i)
        expected[str(path)] = i
    expected[str(tmp_path / "missing.flac")] = None
    original_run = subprocess.run
    calls = []

    def run(cmd, **kwargs):
        assert cmd[:4] == ["adb", "-t", "1", "shell"]
        assert len(cmd) == 5
        calls.append(cmd)
        kwargs.pop("creationflags")
        return original_run(["sh", "-c", cmd[-1]], cwd=tmp_path, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    client = AdbClient("adb")
    assert client.shell_stat_sizes(list(expected), target=AdbTarget(transport_id=1)) == expected
    assert len(calls) == 1
    assert not (tmp_path / "INJECTED").exists()


@pytest.mark.parametrize(
    "stderr,error",
    [
        ("error: device offline", DeviceOfflineError),
        ("no devices/emulators found", DeviceDisconnectedError),
        ("error: device unauthorized", DeviceUnauthorizedError),
        ("error: closed", DeviceDisconnectedError),
    ],
)
def test_batch_keeps_transport_errors(monkeypatch, stderr, error):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=stderr,
        ),
    )
    with pytest.raises(error):
        AdbClient("adb").shell_stat_sizes(["/Music/track.flac"])


@pytest.mark.parametrize("output", ["", "0:-\n", "1:5\n0:6\n", "0:abc\n1:-\n"])
def test_partial_or_invalid_batch_does_not_mark_files_missing(monkeypatch, output):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=output,
            stderr="",
        ),
    )
    with pytest.raises(AdbCommandError):
        AdbClient("adb").shell_stat_sizes(["/a", "/b"])


def test_command_length_splits_long_unicode_paths(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        length = len(subprocess.list2cmdline(cmd).encode("utf-16-le")) // 2
        assert length <= 24000
        count = cmd[-1].count("s=$(stat")
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0, stdout="".join(f"{i}:-\n" for i in range(count)), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", run)
    paths = ["/Music/" + "🎵曲" * 300 + str(i) for i in range(120)]
    sizes = AdbClient("adb.exe").shell_stat_sizes(paths)
    assert sizes == dict.fromkeys(paths)
    assert 1 < len(calls) < len(paths)


def test_empty_batch_spawns_nothing(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("empty plan must not launch adb")

    monkeypatch.setattr(subprocess, "run", unexpected)
    assert AdbClient("adb").shell_stat_sizes([]) == {}


def test_plan_450_missing_files_uses_five_subprocesses(monkeypatch):
    from adb_music_sync.models import (
        LocalFileRef,
        StorageTarget,
        TransferItem,
        TransferPlan,
        TransferStatus,
    )
    from adb_music_sync.transfer import TransferEngine

    calls = []

    def run(cmd, **kwargs):
        assert cmd[:4] == ["adb.exe", "-t", "1", "shell"]
        count = cmd[-1].count("s=$(stat")
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0, stdout="".join(f"{i}:-\n" for i in range(count)), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", run)
    items = [
        TransferItem(LocalFileRef(f"/local/{i}.flac", f"Album/Track {i}.flac", 1234))
        for i in range(450)
    ]
    engine = TransferEngine(
        client=AdbClient("adb.exe"),
        storage=StorageTarget("/storage/external_sd", "SD", free_bytes=10**12),
        destination="/storage/external_sd/Music",
        plan=TransferPlan(items=items),
        target=AdbTarget(transport_id=1),
    )
    queue = engine.build_queue(engine.remote_sizes())
    assert len(queue) == 450
    assert all(item.status is TransferStatus.PENDING for item in queue)
    assert len(calls) == 5
