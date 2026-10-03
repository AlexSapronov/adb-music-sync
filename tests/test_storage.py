"""Tests for storage discovery (internal + SD, free space parsing)."""

from __future__ import annotations

from fake_adb import FakeAdbClient

from adb_music_sync.models import Device, DeviceState
from adb_music_sync.storage import (
    StorageManager,
    _parse_sm_volumes,
    _parse_storage_listing,
)


def test_parse_sm_volumes_sd_only():
    out = "emulated;0 mounted null null\npublic:179,1 mounted A12B-34CD\n"
    mounts = _parse_sm_volumes(out)
    assert mounts == ["/storage/A12B-34CD"]


def test_parse_sm_volumes_different_uuid():
    out = "public:179,65 mounted 5EFE-A1B2\n"
    assert _parse_sm_volumes(out) == ["/storage/5EFE-A1B2"]


def test_parse_sm_volumes_unmounted_ignored():
    out = "public:179,1 unmounted null\n"
    assert _parse_sm_volumes(out) == []


def test_parse_storage_listing():
    out = "emulated\nself\nA12B-34CD\n"
    assert _parse_storage_listing(out) == ["/storage/A12B-34CD"]


def test_internal_only():
    c = FakeAdbClient(
        devices=[Device("D1", DeviceState.DEVICE)],
        sm_volumes="emulated;0 mounted null null\n",
        storage_listing="emulated\nself\n",
    )
    c._df_map = {
        (
            "/storage/emulated/0",
            "D1",
        ): "Filesystem 1K-blocks Used Available Use% M\n/dev/x 45000000 0 41000000 0% /storage/emulated/0\n"
    }
    mgr = StorageManager(c)
    storages = mgr.list_storages("D1")
    assert len(storages) == 1
    assert storages[0].label == "Internal storage"
    assert not storages[0].is_removable
    assert storages[0].free_bytes == 41000000 * 1024


def test_internal_plus_sd():
    c = FakeAdbClient.with_storage(
        internal_size=64 * 1024**3, sd_uuid="A12B-34CD", sd_size=200 * 1024**3
    )
    mgr = StorageManager(c)
    storages = mgr.list_storages("DEVICE1")
    labels = [s.label for s in storages]
    assert "Internal storage" in labels
    assert "SD card" in labels
    sd = next(s for s in storages if s.is_removable)
    assert sd.mount_path == "/storage/A12B-34CD"


def test_two_sd_cards_listed_separately():
    sm = "public:179,1 mounted A12B-34CD\npublic:179,129 mounted 5EFE-A1B2\n"
    listing = "A12B-34CD\n5EFE-A1B2\n"
    c = FakeAdbClient(
        devices=[Device("D1", DeviceState.DEVICE)],
        sm_volumes=sm,
        storage_listing=listing,
    )
    c._df_map = {}
    mgr = StorageManager(c)
    storages = mgr.list_storages("D1")
    sd_paths = [s.mount_path for s in storages if s.is_removable]
    assert "/storage/A12B-34CD" in sd_paths
    assert "/storage/5EFE-A1B2" in sd_paths


def test_sdcard_alias_not_treated_as_removable():
    c = FakeAdbClient(
        devices=[Device("D1", DeviceState.DEVICE)],
        sm_volumes="",
        storage_listing="",
    )
    c._df_map = {
        (
            "/storage/emulated/0",
            "D1",
        ): "Filesystem 1K-blocks Used Available Use% M\n/dev/x 1000 0 1000 0% /storage/emulated/0\n"
    }
    mgr = StorageManager(c)
    storages = mgr.list_storages("D1")
    # should only produce internal storage, never treat /sdcard as removable
    assert all(s.mount_path == "/storage/emulated/0" for s in storages)
