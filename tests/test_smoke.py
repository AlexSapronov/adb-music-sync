"""Smoke import tests — verify all core modules import without a display."""

from __future__ import annotations


def test_import_entry_modules():
    import adb_music_sync
    import adb_music_sync.adb
    import adb_music_sync.config
    import adb_music_sync.controller
    import adb_music_sync.errors
    import adb_music_sync.logging_setup
    import adb_music_sync.models
    import adb_music_sync.paths
    import adb_music_sync.scanner
    import adb_music_sync.storage
    import adb_music_sync.transfer

    assert adb_music_sync.__version__ == "0.1.1"


def test_entry_point_importable():
    # __main__ must not start the GUI on import
    import adb_music_sync.__main__  # noqa: F401


def test_gui_module_imports_no_instantiation():
    # Importing gui must not create a QApplication (only define classes).
    import adb_music_sync.gui  # noqa: F401


def test_fake_adb_adb_matches_interface():
    from fake_adb import FakeAdbClient

    c = FakeAdbClient(devices=[])
    assert hasattr(c, "list_devices")
    assert hasattr(c, "push")
    assert hasattr(c, "shell_stat_size")
    assert hasattr(c, "shell_mkdir")
    assert hasattr(c, "shell_mv")
    assert hasattr(c, "shell_rm")
