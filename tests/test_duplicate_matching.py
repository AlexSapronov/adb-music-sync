"""Aggressiveness levels never turn uncertain matches into automatic selections."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from test_duplicates import MUSIC, PLAYLIST, A, B, ByteDevice, manager

from adb_music_sync.adb import AdbClient
from adb_music_sync.duplicates import names_match, normalized_name, possible_groups
from adb_music_sync.errors import DeviceDisconnectedError
from adb_music_sync.models import AdbTarget

P = MUSIC + "/Artist/Album/01 Burning Desires.flac"
Q = MUSIC + "/Artist/Compilation/02 Burning Desires.mp3"
R = MUSIC + "/Artist/Live/03 Burning Desire.flac"


def tracks():
    device = ByteDevice({P: b"lossless audio bytes", Q: b"mp3 audio", R: b"live edit different"})
    device.audio_durations = lambda **kwargs: {P: 220000, Q: 221500, R: 228000}
    return device


def test_level_zero_remains_exact_only():
    device = tracks()
    device.audio_durations = lambda **kwargs: pytest.fail("Exact mode does not query MediaStore")
    assert manager(device).scan("Music").groups == ()


@pytest.mark.parametrize(
    "level, expected", [(1, "name_duration"), (2, "name"), (3, "similar_name")]
)
def test_levels_group_reencoded_and_similar_titles(level, expected):
    scan = manager(tracks()).scan("Music", level=level)
    assert len(scan.groups) == 1
    assert scan.groups[0].mode == expected
    assert set(scan.groups[0].paths) == ({P, Q, R} if level == 3 else {P, Q})
    assert scan.duration_count == 3
    assert len(scan.groups[0].hashes) == len(scan.groups[0].paths)
    assert scan.redundant_bytes == sum(scan.groups[0].size_for(p) for p in scan.groups[0].paths[1:])


def test_missing_duration_never_falls_back_to_name_only():
    device = tracks()
    device.audio_durations = lambda **kwargs: {P: 220000}
    assert manager(device).scan("Music", level=1).groups == ()
    assert len(manager(device).scan("Music", level=2).groups) == 1


def test_duration_tolerance_is_two_seconds_and_does_not_chain():
    paths = [MUSIC + f"/{i}/01 Song.flac" for i in range(3)]
    result = possible_groups(paths, dict(zip(paths, [100000, 101900, 103800], strict=True)), 1)
    assert result == [tuple(paths[:2])]


def test_fuzzy_groups_compare_all_members():
    paths = [MUSIC + "/" + t + ".flac" for t in ["abcdefghijk", "abcdefghijx", "abcdefghixx"]]
    assert names_match("abcdefghijk", "abcdefghijx")
    assert names_match("abcdefghijx", "abcdefghixx")
    assert not names_match("abcdefghijk", "abcdefghixx")
    assert possible_groups(paths, {}, 3) == [tuple(paths[:2])]


def test_normalization_preserves_version_words_and_handles_unicode():
    assert normalized_name("/Music/01 - ＨＯＴ SAUCE.flac") == "hot sauce"
    assert normalized_name("/Music/02_Hot-Sauce.mp3") == "hot sauce"
    assert normalized_name("/Music/01 Song (Live).flac") != normalized_name("/Music/02 Song.flac")
    assert normalized_name("/Music/02 日本語.flac") == "日本語"
    assert not names_match("intro", "outro")


def test_exact_groups_always_present_and_never_overlap_possible_groups():
    device = tracks()
    device.files[A] = device.files[B] = b"exact bytes"
    for level in [1, 2, 3]:
        scan = manager(device).scan("Music", level=level)
        assert any(g.mode == "exact" and set(g.paths) == {A, B} for g in scan.groups)
        all_paths = [p for g in scan.groups for p in g.paths]
        assert len(all_paths) == len(set(all_paths))


def test_possible_quarantine_restore_preserves_different_original_audio():
    device = tracks()
    playlist = ("#EXTM3U\n" + Q + "\n").encode()
    device.files[PLAYLIST] = playlist
    device._dirs(PLAYLIST)
    before = dict(device.files)
    m = manager(device)
    scan = m.scan("Music", level=2)
    session = m.quarantine(scan, {scan.groups[0].digest: P})
    journal = json.loads(device.files[session + "/manifest.json"])
    assert journal["entries"][0]["sha256"] != journal["entries"][0]["kept_sha256"]
    assert journal["entries"][0]["size"] == len(before[Q])
    assert device.files[PLAYLIST] == playlist.replace(Q.encode(), P.encode())
    m.restore(session)
    assert {p: device.files[p] for p in before} == before


def test_possible_purge_checks_kept_fingerprint_not_duplicate_hash():
    device = tracks()
    m = manager(device)
    scan = m.scan("Music", level=2)
    session = m.quarantine(scan, {scan.groups[0].digest: P})
    m.purge(session)
    assert device.files[P] == b"lossless audio bytes"
    assert Q not in device.files
    assert len(device.removals) == 1


def test_possible_files_changed_after_scan_are_not_moved():
    device = tracks()
    m = manager(device)
    scan = m.scan("Music", level=2)
    device.files[Q] = b"edited"
    with pytest.raises(ValueError):
        m.quarantine(scan, {scan.groups[0].digest: P})
    assert not device.moves


def test_legacy_journal_without_kept_hash_still_restores_and_purges():
    device = ByteDevice()
    m = manager(device)
    scan = m.scan("Music")
    session = m.quarantine(scan, {scan.groups[0].digest: A})
    manifest = session + "/manifest.json"
    journal = json.loads(device.files[manifest])
    for e in journal["entries"]:
        e.pop("kept_sha256")
        e.pop("match_mode")
    device.files[manifest] = json.dumps(journal).encode()
    m.restore(session)


def test_mediastore_parser_keeps_commas_unicode_and_rejects_missing_values(monkeypatch):
    text = "\n".join(
        [
            "Row: 0 duration=123000, _data=/storage/external_sd/Music/日本, title.flac",
            "Row: 1 duration=NULL, _data=/storage/external_sd/Music/missing.flac",
            "Row: 2 duration=0, _data=/storage/external_sd/Music/zero.flac",
            "Row: 3 duration=123000, _data=/storage/external_sd/Music/stale.flac",
            "Row: 4 duration=126000, _data=/storage/external_sd/Music/stale.flac",
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=text, stderr=""),
    )
    result = AdbClient("adb").audio_durations(target=AdbTarget(transport_id=4))
    assert result == {"/storage/external_sd/Music/日本, title.flac": 123000}


def test_mediastore_permission_failure_is_optional_but_disconnect_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="Permission Denial: READ_EXTERNAL_STORAGE"
        ),
    )
    client = AdbClient("adb")
    target = AdbTarget(transport_id=4)
    assert client.audio_durations(target=target) == {}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="no devices/emulators found"
        ),
    )
    with pytest.raises(DeviceDisconnectedError):
        client.audio_durations(target=target)
