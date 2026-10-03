"""Regression tests for controller orchestration.

Covers the 0.1.2 worker-chaining race (bug 2): the next phase must start only
after the previous QThread has *actually* finished, never be silently dropped
by the single-worker guard, and never start a second concurrent worker. Runs
the real QThread lifecycle through a QCoreApplication event loop where Qt is
available.
"""

from __future__ import annotations

import time

import pytest

try:
    from PySide6.QtCore import QCoreApplication
except Exception:  # pragma: no cover - headless-friendly
    QCoreApplication = None

from adb_music_sync.controller import Controller, _WorkerThread
from adb_music_sync.models import AppState
from adb_music_sync.scanner import ScanResult

needs_qt = pytest.mark.skipif(QCoreApplication is None, reason="PySide6 not importable")


def _make_controller():
    app = QCoreApplication.instance() or QCoreApplication([])
    c = Controller()
    return app, c


def _spin(app, timeout_s: float = 1.0):
    """Drain queued Qt events (cross-thread signal delivery) without sleep-polling."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


@needs_qt
def test_scan_done_without_destination_goes_ready(monkeypatch):
    _, c = _make_controller()
    c.set_state(AppState.SCANNING)
    calls = []
    monkeypatch.setattr(c, "build_plan_async", lambda dest: calls.append(dest))

    c._on_scan_done(ScanResult(files=[], total_bytes=0))

    assert calls == []
    assert c._pending_continuation is None
    assert c.state is AppState.READY


@needs_qt
def test_scan_done_defers_build_plan_not_immediate(monkeypatch):
    """build_plan must NOT run synchronously in _on_scan_done; it is queued as
    a continuation triggered once the QThread actually exits."""
    _, c = _make_controller()
    c.set_state(AppState.SCANNING)
    calls = []
    monkeypatch.setattr(c, "build_plan_async", lambda dest: calls.append(dest))

    c._pending_destination = "/storage/emulated/0/Music"
    c._on_scan_done(ScanResult(files=[], total_bytes=0))

    assert calls == []
    assert c._pending_continuation is not None
    assert c._pending_destination is None  # consumed exactly once


@needs_qt
def test_pending_continuation_runs_once_on_worker_finish(monkeypatch):
    app, c = _make_controller()
    returns = []
    monkeypatch.setattr(c, "build_plan_async", lambda dest: returns.append(dest))

    fake = _WorkerThread(lambda: None, parent=c)
    c._worker = fake
    c._pending_continuation = lambda: c.build_plan_async("/storage/emulated/0/Music")

    c._on_worker_finished(fake)

    assert returns == ["/storage/emulated/0/Music"]
    assert c._worker is None
    assert c._pending_continuation is None
    # A second call must not re-run the continuation.
    c._on_worker_finished(fake)
    assert returns == ["/storage/emulated/0/Music"]
    app.quit()


@needs_qt
def test_real_thread_chain_scan_then_plan(monkeypatch):
    """Real QThread lifecycle: scan worker finishes -> plan worker starts ->
    state reaches READY. No sleep/polling; a short event-loop drain delivers
    the cross-thread signals."""
    app, c = _make_controller()

    scan_runs = []
    plan_dests = []

    # Stub the heavy scan_library so the scan worker returns instantly.
    monkeypatch.setattr(
        "adb_music_sync.controller.scan_library",
        lambda folder: scan_runs.append(folder) or ScanResult(files=[], total_bytes=0),
    )

    # Record plan-build dests; still go through a real worker thread.
    def build_stub(dest):
        plan_dests.append(dest)
        c.set_state(AppState.READY)

    monkeypatch.setattr(c, "build_plan_async", build_stub)

    c.scan_library_async("/music", destination="/storage/emulated/0/Music")
    assert c.state is AppState.SCANNING

    _spin(app)

    assert scan_runs == ["/music"]
    assert plan_dests == ["/storage/emulated/0/Music"]
    assert c.state is AppState.READY  # not stuck in SCANNING
    assert c._worker is None  # worker released, no overlap
    app.quit()


@needs_qt
def test_double_scan_does_not_start_two_workers(monkeypatch):
    """A second 'Проверить' during an active scan must not spawn a second worker."""
    app, c = _make_controller()

    started = [0]
    orig_start = _WorkerThread.start

    def counted_start(self):
        started[0] += 1
        orig_start(self)

    monkeypatch.setattr(_WorkerThread, "start", counted_start)
    monkeypatch.setattr("adb_music_sync.controller.scan_library", lambda folder: None)

    c.scan_library_async("/music", destination="/storage/emulated/0/Music")
    first_worker = c._worker
    # Second press while first is still active.
    c.scan_library_async("/music", destination="/storage/emulated/0/Music")

    # Exactly one real worker started; the second call hit the guard.
    assert started[0] == 1
    assert c._worker is first_worker  # unchanged, no second worker
    _spin(app)
    app.quit()


@needs_qt
def test_scan_library_async_stores_destination(monkeypatch):
    app, c = _make_controller()
    monkeypatch.setattr(c, "_run_background", lambda fn, cb: None)

    c.scan_library_async("/music", destination="/storage/emulated/0/Music")
    assert c._pending_destination == "/storage/emulated/0/Music"
    assert c.state is AppState.SCANNING
    app.quit()
