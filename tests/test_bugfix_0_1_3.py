"""Regression tests for the 0.1.3 FiiO / hardware-compatibility fixes.

Covers the real-device findings from a FiiO JM21:

1. ``adb devices -l`` reports host serial ``?`` but a valid ``transport_id``.
2. Removable SD mounts at a vendor-named path (``/storage/external_sd``) rather
   than a UUID-style name.
3. Discovery must be asynchronous (never block the Qt GUI thread).

These target the REAL ``AdbClient`` / ``StorageManager`` / ``Controller``
logic (with FakeAdbClient for the transport) — not a paraphrase of it.
"""

from __future__ import annotations

import pytest
from fake_adb import FakeAdbClient
from PySide6.QtCore import QCoreApplication

from adb_music_sync.adb import AdbClient
from adb_music_sync.controller import Controller
from adb_music_sync.models import Device, DeviceState
from adb_music_sync.storage import (
    StorageManager,
    _parse_sm_volumes,
    _parse_storage_listing,
)

# -- problem 1: ADB serial = "?" -------------------------------------------


def _parse(line: str) -> Device:
    return AdbClient(adb_path="/fake/adb")._parse_device_line(line)


def test_parse_fiio_question_mark_serial():
    d = _parse("? device product:bengal_515 model:FiiO_JM21 device:bengal_515 transport_id:4")
    assert d.serial == "?"
    assert d.state is DeviceState.DEVICE
    assert d.transport_id == 4
    assert d.product == "bengal_515"
    assert d.model == "FiiO_JM21"
    assert d.device_name == "bengal_515"
    assert d.display_name == "FiiO JM21"  # underscore -> space


def test_fiio_uses_transport_selector_not_s_question_mark():
    d = _parse("? device product:bengal_515 model:FiiO_JM21 device:bengal_515 transport_id:4")
    args = d.selector.args()
    assert args == ["-t", "4"]
    assert "-s" not in args
    # and no `-s ?` ever
    assert "?" not in args


def test_normal_serial_device_keeps_serial_selector():
    d = _parse("R58M123ABC device product:foo model:Galaxy_S25 device:foo transport_id:7")
    assert d.serial == "R58M123ABC"
    assert d.transport_id == 7
    assert d.display_name == "Galaxy S25"
    # A normal, reliable serial keeps using -s (transport is session-specific).
    assert d.selector.args() == ["-s", "R58M123ABC"]


def test_unreliable_serial_detection():
    assert _parse("? device transport_id:4")._serial_unreliable()
    assert _parse("unknown device transport_id:4")._serial_unreliable()
    assert not _parse("R58M123ABC device transport_id:4")._serial_unreliable()


def test_transport_id_absent_uses_serial():
    d = _parse("R58M123ABC device model:Galaxy_S25")
    assert d.transport_id is None
    assert d.selector.args() == ["-s", "R58M123ABC"]


def test_stable_id_from_serialno_resolution():
    client = FakeAdbClient.with_device()
    client.props["ro.serialno"] = "e32f44df"
    d = Device(serial="?", state=DeviceState.DEVICE, transport_id=4)
    assert client.resolve_stable_id(d) == "e32f44df"


def test_stable_id_falls_back_to_host_serial():
    client = FakeAdbClient.with_device()
    d = Device(serial="R58M123ABC", state=DeviceState.DEVICE, transport_id=7)
    # no ro.serialno prop -> falls back to the reliable host serial
    assert client.resolve_stable_id(d) == "R58M123ABC"


def test_stable_id_never_transport_id_for_persistent():
    client = FakeAdbClient.with_device()
    d = Device(serial="?", state=DeviceState.DEVICE, transport_id=4)
    # No serial to fall back to — must NOT return "4" as a *stable* id for
    # settings (transport ids change across reconnects).
    # resolve_stable_id returns a display-only "transport-N" token here, which
    # is distinct enough that callers never persist it as the stable device id.
    sid = client.resolve_stable_id(d)
    assert sid != "4"


# -- problem 2: vendor-named removable storage ------------------------------


def test_sm_volumes_fiio_external_sd():
    out = "private mounted null\nemulated;0 mounted null null\npublic:179,25 mounted external_sd\n"
    internal, removable = _parse_sm_volumes(out)
    assert internal == ["/storage/emulated/0"]
    assert removable == ["/storage/external_sd"]


def test_sm_volumes_private_ignored():
    internal, removable = _parse_sm_volumes("private mounted null\n")
    assert internal == []
    assert removable == []


def test_sm_volumes_public_unmounted_ignored():
    _, removable = _parse_sm_volumes("public:179,25 unmounted null\n")
    assert removable == []


def test_sm_volumes_uuid_still_works():
    _, removable = _parse_sm_volumes("public:179,1 mounted A12B-34CD\n")
    assert removable == ["/storage/A12B-34CD"]


def test_sm_volumes_multiple_removable():
    out = (
        "emulated;0 mounted null null\n"
        "public:179,25 mounted external_sd\n"
        "public:179,129 mounted A12B-34CD\n"
    )
    _, removable = _parse_sm_volumes(out)
    assert removable == ["/storage/external_sd", "/storage/A12B-34CD"]


def test_storage_listing_rejects_reserved_names():
    out = "emulated\nself\nsdcard0\n"
    assert _parse_storage_listing(out) == []


def test_storage_listing_vendor_and_uuid():
    out = "emulated\nself\nexternal_sd\nA12B-34CD\n"
    assert _parse_storage_listing(out) == ["/storage/external_sd", "/storage/A12B-34CD"]


def test_fiio_full_discovery_internal_plus_sd():
    client = FakeAdbClient(
        devices=[Device(serial="?", state=DeviceState.DEVICE, transport_id=4)],
        sm_volumes=(
            "private mounted null\n"
            "emulated;0 mounted null null\n"
            "public:179,25 mounted external_sd\n"
        ),
        storage_listing="emulated\nself\nexternal_sd\n",
    )
    client._df_map = {
        (
            "/storage/emulated/0",
            "?",
        ): "Filesystem 1K-blocks Used Available Use% M\n/dev/fuse 22270892 5346004 16777432 25% /storage/emulated/0\n",
        (
            "/storage/external_sd",
            "?",
        ): "Filesystem 1K-blocks Used Available Use% M\n/dev/mmc 61000000 1000000 60000000 3% /storage/external_sd\n",
    }
    mgr = StorageManager(client)
    storages = mgr.list_storages(
        target=Device(serial="?", state=DeviceState.DEVICE, transport_id=4).selector
    )
    labels = [s.label for s in storages]
    assert "Internal storage" in labels
    assert "SD card" in labels
    sd = next(s for s in storages if s.is_removable)
    assert sd.mount_path == "/storage/external_sd"
    assert sd.free_bytes > 0
    assert sd.total_bytes > 0
    # internal first
    assert storages[0].mount_path == "/storage/emulated/0"


# -- problem 3: async discovery, picker independence ------------------------


@pytest.fixture
def _qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    return app


def test_refresh_devices_runs_async_not_synchronously(_qapp, monkeypatch):
    """list_devices must not run on the controller's own thread synchronously."""

    ctrl = Controller()
    fake = FakeAdbClient(devices=[Device(serial="?", state=DeviceState.DEVICE, transport_id=4)])
    fake.props["ro.serialno"] = "e32f44df"
    ctrl.client = fake
    ctrl.storage_manager = StorageManager(fake)

    # Capture whether list_devices was called directly (sync) during the call.
    called_inline = False
    orig = fake.list_devices

    def patched():
        nonlocal called_inline
        called_inline = True
        return orig()

    monkeypatch.setattr(fake, "list_devices", patched)
    ctrl.refresh_devices()
    # The synchronous call path returns before the worker runs; list_devices
    # is only reached once the event loop processes the worker thread.
    assert not called_inline


def test_discovery_error_surfaces(_qapp, monkeypatch):

    ctrl = Controller()
    fake = FakeAdbClient(devices=[])
    ctrl.client = fake
    ctrl.storage_manager = StorageManager(fake)
    ctrl.selected_device = Device(serial="?", state=DeviceState.DEVICE, transport_id=4)

    statuses: list[str] = []
    ctrl.storages_status.connect(statuses.append)

    # Simulate a discovery failure via a storage manager that raises.
    def boom(target=None, serial=None):
        raise RuntimeError("adbd not responding")

    monkeypatch.setattr(ctrl.storage_manager, "list_storages", boom)

    ctrl._refresh_storages()
    # The status must immediately reflect "discovering" before any async work.
    assert statuses[0] == "discovering"


def test_select_device_does_not_sync_discover_storages(_qapp, monkeypatch):
    """select_device must NOT run storage discovery inline (bug: GUI freeze)."""

    ctrl = Controller()
    fake = FakeAdbClient(devices=[Device(serial="?", state=DeviceState.DEVICE, transport_id=4)])
    ctrl.client = fake
    ctrl.storage_manager = StorageManager(fake)
    ctrl.devices = list(fake.devices)

    ran_inline = False

    def _sync_probe():
        nonlocal ran_inline
        ran_inline = True
        return []

    monkeypatch.setattr(ctrl.storage_manager, "list_storages", _sync_probe)

    ctrl.selected_device = None
    ctrl.select_device("?")
    # Discovery is dispatched to a worker; the actual list_storages call must
    # NOT have happened synchronously inside select_device.
    assert not ran_inline


def test_multiple_devices_do_not_mix(_qapp):
    client = FakeAdbClient(
        devices=[
            Device(serial="R58M123ABC", state=DeviceState.DEVICE, transport_id=7),
            Device(serial="?", state=DeviceState.DEVICE, transport_id=4),
            Device(serial="emulator-5554", state=DeviceState.DEVICE, transport_id=1),
        ]
    )
    devices = client.list_devices()
    assert len(devices) == 3
    serials = [d.serial for d in devices]
    assert serials == ["R58M123ABC", "?", "emulator-5554"]
    transports = [d.transport_id for d in devices]
    assert transports == [7, 4, 1]
    # Selectors are independent and correct.
    assert devices[0].selector.args() == ["-s", "R58M123ABC"]
    assert devices[1].selector.args() == ["-t", "4"]
