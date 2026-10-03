"""Tests for destination path safety and Android path construction."""

from __future__ import annotations

import pytest

from adb_music_sync.errors import InvalidDestinationError
from adb_music_sync.paths import (
    is_within,
    join_rel,
    normalize_posix,
    validate_destination,
)


def test_validate_relative_under_root():
    assert validate_destination("/storage/emulated/0", "Music") == "/storage/emulated/0/Music"


def test_validate_absolute_under_root():
    assert (
        validate_destination("/storage/A12B-34CD", "/storage/A12B-34CD/Music")
        == "/storage/A12B-34CD/Music"
    )


def test_validate_trailing_slash_normalized():
    assert validate_destination("/storage/emulated/0", "Music/") == "/storage/emulated/0/Music"


def test_reject_parent_escape():
    with pytest.raises(InvalidDestinationError):
        validate_destination("/storage/emulated/0", "../etc")


def test_reject_escape_via_absolute():
    with pytest.raises(InvalidDestinationError):
        validate_destination("/storage/A12B-34CD", "/storage/emulated/0")


def test_reject_system_paths():
    for bad in ("/", "/system", "/data", "/system/app"):
        with pytest.raises(InvalidDestinationError):
            validate_destination("/storage/emulated/0", bad)


def test_reject_empty():
    with pytest.raises(InvalidDestinationError):
        validate_destination("/storage/emulated/0", "")


def test_is_within_unicode_and_spaces():
    root = "/storage/A12B-34CD"
    assert is_within(root, r"/storage/A12B-34CD/Massive Attack/Меланина")
    assert not is_within(root, "/storage/emulated/0")


def test_join_rel_rejects_parent():
    with pytest.raises(InvalidDestinationError):
        join_rel("/storage/emulated/0/Music", "../../etc")


def test_join_rel_unicode():
    joined = join_rel("/storage/A12B-34CD/Music", "Arist/Aлиса/Track (1) 'x' & #.flac")
    assert joined.startswith("/storage/A12B-34CD/Music/")
    assert "Aлиса" in joined


def test_normalize_posix_root():
    assert normalize_posix("/") == "/"
    assert normalize_posix("//") == "/"


def test_join_rel_strips_leading_slash():
    joined = join_rel("/storage/emulated/0/Music", "/Artist/x.mp3")
    assert joined == "/storage/emulated/0/Music/Artist/x.mp3"
