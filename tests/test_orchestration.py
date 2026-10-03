"""Regression tests for controller orchestration (bug 1): scan -> build plan -> READY.

Tests the callback logic directly (no GUI, no QThread) to prove the fixed
sequential flow: after a scan completes with a pending destination, the
plan-build is chained automatically and the state does not get stuck in
SCANNING.
"""

from __future__ import annotations

import pytest

try:
    from PySide6.QtCore import QCoreApplication
except Exception:  # pragma: no cover - headless-friendly
    QCoreApplication = None

from adb_music_sync.controller import Controller
from adb_music_sync.models import AppState
from adb_music_sync.scanner import ScanResult

needs_qt = pytest.mark.skipif(QCoreApplication is None, reason="PySide6 not importable")


def _make_controller():
    app = QCoreApplication.instance() or QCoreApplication([])
    c = Controller()
    return app, c


@needs_qt
def test_scan_done_without_destination_goes_ready(monkeypatch):
    _, c = _make_controller()
    c.set_state(AppState.SCANNING)
    calls = []
    monkeypatch.setattr(c, "build_plan_async", lambda dest: calls.append(dest))

    c._pending_destination = None
    c._on_scan_done(ScanResult(files=[], total_bytes=0))

    # No destination pending -> must NOT call build_plan, must reach READY.
    assert calls == []
    assert c.state is AppState.READY


@needs_qt
def test_scan_done_chains_build_plan(monkeypatch):
    _, c = _make_controller()
    c.set_state(AppState.SCANNING)
    calls = []
    monkeypatch.setattr(c, "build_plan_async", lambda dest: calls.append(dest))

    c._pending_destination = "/storage/emulated/0/Music"
    c._on_scan_done(ScanResult(files=[], total_bytes=0))

    # Destination was pending -> build_plan is chained automatically.
    assert calls == ["/storage/emulated/0/Music"]
    # _pending_destination consumed exactly once
    assert c._pending_destination is None


@needs_qt
def test_scan_library_async_stores_destination(monkeypatch):
    app, c = _make_controller()
    # Prevent the background worker from actually running.
    monkeypatch.setattr(c, "_run_background", lambda fn, cb: None)

    c.scan_library_async("/music", destination="/storage/emulated/0/Music")
    assert c._pending_destination == "/storage/emulated/0/Music"
    assert c.state is AppState.SCANNING
    app.quit()
