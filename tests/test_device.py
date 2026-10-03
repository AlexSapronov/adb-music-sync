"""Optional tests requiring a real Android device over ADB.

Excluded from CI with ``pytest -m "not device"``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.device


@pytest.mark.device
def test_real_device_listed():
    from adb_music_sync.adb import AdbClient

    client = AdbClient()
    devices = client.list_devices()
    assert isinstance(devices, list)


@pytest.mark.device
def test_real_storage_discovery():
    from adb_music_sync.adb import AdbClient
    from adb_music_sync.storage import StorageManager

    client = AdbClient()
    devices = client.list_devices()
    if not devices:
        pytest.skip("no device connected")
    serial = devices[0].serial
    mgr = StorageManager(client)
    storages = mgr.list_storages(serial)
    assert len(storages) >= 1
    assert any(s.label == "Internal storage" for s in storages)
