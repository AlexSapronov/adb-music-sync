"""Unit tests for adb device parsing and error normalization."""

from __future__ import annotations

import pytest
from fake_adb import FakeAdbClient

from adb_music_sync.errors import DeviceDisconnectedError
from adb_music_sync.models import Device, DeviceState
from adb_music_sync.storage import _parse_df_kb


def test_list_devices_parses_states():
    c = FakeAdbClient(
        devices=[
            Device("AAAA", DeviceState.DEVICE),
            Device("BBBB", DeviceState.UNAUTHORIZED),
            Device("CCCC", DeviceState.OFFLINE),
        ]
    )
    devs = c.list_devices()
    assert [d.serial for d in devs] == ["AAAA", "BBBB", "CCCC"]
    assert devs[0].state is DeviceState.DEVICE
    assert devs[1].state is DeviceState.UNAUTHORIZED
    assert devs[2].state is DeviceState.OFFLINE


def test_no_devices_empty():
    c = FakeAdbClient(devices=[])
    assert c.list_devices() == []


def test_parse_df_kb_gnu():
    out = (
        "Filesystem 1K-blocks Used Available Use% Mounted on\n"
        "/dev/sda1 1953000 100 1853000 1% /storage/A12B-34CD\n"
    )
    free, total = _parse_df_kb(out)
    assert free == 1853000 * 1024
    assert total == 1953000 * 1024


def test_parse_df_kb_toybox_k_suffix():
    out = (
        "Filesystem 1K-blocks Used Available Use% Mounted on\n"
        "/dev/block/mmcblk0 128K 0 128K 0% /storage/emulated/0\n"
    )
    free, total = _parse_df_kb(out)
    assert free == 128 * 1024
    assert total == 128 * 1024


def test_parse_df_kb_empty():
    assert _parse_df_kb("") is None


def test_disconnect_raises_structured():
    c = FakeAdbClient(devices=[])
    c.disconnect = True
    with pytest.raises(DeviceDisconnectedError):
        c.push("a", "b")
