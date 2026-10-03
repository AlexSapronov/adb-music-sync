"""Tests for the local library scanner."""

from __future__ import annotations

from adb_music_sync.scanner import is_supported, scan_library


def _mk(tmp_path, *parts: str, content=b"x"):
    p = tmp_path.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_is_supported():
    assert is_supported("a.mp3")
    assert is_supported("b.FLAC")
    assert is_supported("c.opus")
    assert not is_supported("a.mp4")
    assert not is_supported("cover.jpg")


def test_scan_preserves_structure_and_posix(tmp_path):
    _mk(tmp_path, "Massive Attack", "Mezzanine", "01 - Angel.flac", content=b"1" * 10)
    _mk(tmp_path, "Massive Attack", "Mezzanine", "02 - Risingson.mp3", content=b"2" * 5)
    _mk(tmp_path, "Skip", "cover.jpg", content=b"3" * 3)  # not audio
    result = scan_library(str(tmp_path))
    rels = sorted(f.rel_path for f in result.files)
    assert rels == [
        "Massive Attack/Mezzanine/01 - Angel.flac",
        "Massive Attack/Mezzanine/02 - Risingson.mp3",
    ]
    assert result.total_bytes == 15


def test_scan_unicode(tmp_path):
    _mk(tmp_path, "Кино", "Группа крови", "03 - Группа крови.flac", content=b"a" * 7)
    _mk(tmp_path, "Японская", "曲", "歌.mp3", content=b"b" * 3)
    result = scan_library(str(tmp_path))
    rels = sorted(f.rel_path for f in result.files)
    assert "Кино/Группа крови/03 - Группа крови.flac" in rels
    assert "Японская/曲/歌.mp3" in rels


def test_scan_empty(tmp_path):
    result = scan_library(str(tmp_path))
    assert result.files == []
    assert result.total_bytes == 0
