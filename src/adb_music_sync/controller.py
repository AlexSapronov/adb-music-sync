"""GUI application controller.

Separates app logic from Qt widgets: the controller owns the AdbClient,
StorageManager, scanner and transfer engine, and drives a QThread-based
worker for long operations so the UI never blocks. Widgets talk to this
controller via signals/slots.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .adb import AdbClient
from .config import load_config, save_config
from .logging_setup import get_logger
from .models import AppState, Device, StorageTarget
from .scanner import ScanResult, scan_library
from .storage import StorageManager
from .transfer import TransferEngine, TransferPlan

log = get_logger()


class _WorkerThread(QThread):
    """Run a callable on a background thread, emitting finished/error signals."""

    finished_ok = Signal(object)
    failed = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._result = None
        self._error = None

    def run(self):  # noqa: D102
        try:
            self._result = self._fn()
        except Exception as exc:  # noqa: BLE001
            self._error = exc
            self.failed.emit(exc)
        else:
            self.finished_ok.emit(self._result)


class Controller(QObject):
    """Coordinates all backend operations and exposes them to the UI."""

    devices_changed = Signal(list)
    storages_changed = Signal(list)
    scan_changed = Signal(object)  # ScanResult
    plan_changed = Signal(object)  # TransferPlan
    state_changed = Signal(str)  # AppState value
    log_message = Signal(str)
    progress_changed = Signal(object)  # TransferProgress
    storages_status = Signal(str)  # "discovering" | "ready" | "error" + msg

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = load_config()
        self.client: AdbClient | None = None
        self.storage_manager: StorageManager | None = None
        self.devices: list[Device] = []
        self.storages: list[StorageTarget] = []
        self.selected_device: Device | None = None
        self.selected_storage: StorageTarget | None = None
        self.scan_result: ScanResult | None = None
        self.plan: TransferPlan | None = None
        self.engine: TransferEngine | None = None
        self.state = AppState.IDLE
        self._worker: _WorkerThread | None = None
        self._discovery_worker: _WorkerThread | None = None
        self._engine_lock = threading.Lock()
        self._pending_destination: str | None = None
        self._pending_continuation: callable | None = None
        self._retry_timer: QTimer | None = None

    # -- wiring ------------------------------------------------------------
    def _connect(self) -> None:
        try:
            self.client = AdbClient()
            self.storage_manager = StorageManager(self.client)
        except SystemExit:
            raise
        except Exception as exc:  # e.g. AdbNotFoundError
            self.log_message.emit(str(exc))
            self.client = None
            self.storage_manager = None

    def set_state(self, s: AppState) -> None:
        self.state = s
        self.state_changed.emit(s.value)

    # -- device actions ----------------------------------------------------
    def refresh_devices(self) -> None:
        """Discover devices asynchronously (never blocks the GUI thread).

        ``adb devices -l`` plus stable-identity resolution (``ro.serialno``)
        run on a background worker. If no device shows up (FiiO enumerates
        slowly right after plug-in), a short QTimer auto-retries until one
        appears — no busy-loop, no UI freeze.
        """
        if self.client is None:
            self._connect()
        if self.client is None:
            self.devices = []
            self.devices_changed.emit([])
            return
        self._run_discovery(self._discover_devices_work, self._on_devices_discovered)

    def _discover_devices_work(self):
        client = self.client
        devices = client.list_devices()
        # Resolve a stable identity (ro.serialno) for each ready device
        # without a usable host serial, using its *live* transport selector.
        for d in devices:
            if d.is_ready:
                d.stable_id = client.resolve_stable_id(d)
        return devices

    def _on_devices_discovered(self, devices: list[Device]) -> None:
        self.devices = devices
        self.devices_changed.emit(devices)
        if not devices:
            self._schedule_retry()
            return
        self._cancel_retry()
        # Keep the previous selection when it still exists, by stable identity
        # first, then host serial, then transport — reconnect may change the
        # transport id, but the stable id pins the same physical device.
        prev = self.selected_device
        if prev is not None:
            self.selected_device = self._rematch_device(prev, devices)
        if self.selected_device is None and devices:
            self.selected_device = devices[0]
        if self.selected_device is not None:
            self._refresh_storages()

    def _rematch_device(self, prev: Device, devices: list[Device]) -> Device | None:
        """Find ``prev`` among freshly discovered devices after a reconnect."""
        prev_stable = getattr(prev, "stable_id", None)
        if prev_stable:
            for d in devices:
                if d.stable_id == prev_stable:
                    return d
        for d in devices:
            if d.serial == prev.serial and not prev._serial_unreliable():
                return d
        return None

    def _schedule_retry(self) -> None:
        log.info("No devices yet — will retry discovery shortly")
        if self._retry_timer is None:
            self._retry_timer = QTimer(self)
            self._retry_timer.setSingleShot(False)
            self._retry_timer.timeout.connect(self.refresh_devices)
            self._retry_timer.setInterval(1500)
        if not self._retry_timer.isActive():
            self._retry_timer.start()

    def _cancel_retry(self) -> None:
        if self._retry_timer is not None and self._retry_timer.isActive():
            self._retry_timer.stop()

    def select_device(self, serial: str) -> None:
        for d in self.devices:
            if d.serial == serial:
                self.selected_device = d
                self.config["selected_serial"] = d.stable_id or d.serial or serial
                self._save()
                self._refresh_storages()
                return

    # -- storage discovery -------------------------------------------------
    def _refresh_storages(self) -> None:
        """Discover storages for the selected device asynchronously."""
        if self.storage_manager is None or self.selected_device is None:
            self.storages = []
            self.storages_changed.emit([])
            return
        if not self.selected_device.is_ready:
            self.storages = []
            self.storages_changed.emit([])
            return
        self.storages_status.emit("discovering")
        self._run_discovery(self._discover_storages_work, self._on_storages_discovered)

    def _discover_storages_work(self):
        device = self.selected_device
        mgr = self.storage_manager
        return mgr.list_storages(target=device.selector)

    def _on_storages_discovered(self, storages: list[StorageTarget]) -> None:
        self.storages = storages
        self.storages_changed.emit(storages)
        self.storages_status.emit("ready")
        for s in storages:
            log.info(
                "%s: %s (free=%d, total=%d)",
                "Internal" if not s.is_removable else "Removable",
                s.mount_path,
                s.free_bytes,
                s.total_bytes,
            )
        # Re-select the remembered storage (by stable device id) if present.
        if self.selected_device is not None:
            key = self.config.get("storage_by_serial", {}).get(
                self.selected_device.stable_id or self.selected_device.serial
            )
            if key:
                for s in storages:
                    if s.mount_path == key:
                        self.selected_storage = s
                        break

    def _on_discovery_error(self, exc: Exception) -> None:
        self.storages_status.emit("error")
        self.log_message.emit(f"Discovery failed: {exc}")
        log.error("Discovery failed: %s", exc)

    def select_storage(self, mount_path: str) -> None:
        for s in self.storages:
            if s.mount_path == mount_path:
                self.selected_storage = s
                if self.selected_device is not None:
                    dev_key = self.selected_device.stable_id or self.selected_device.serial
                    self.config["storage_by_serial"][dev_key] = mount_path
                self._save()
                return

    # -- scanning ----------------------------------------------------------
    def scan_library_async(self, folder: str, destination: str | None = None) -> None:
        """Scan the local library, then (optionally) auto-build the plan.

        When ``destination`` is given, the plan is built automatically as
        soon as the scan finishes — this is the "Проверить" happy path:
        scan -> callback -> build plan -> callback -> READY. The two steps
        are chained sequentially on purpose (never two QThreads at once).
        """
        self.set_state(AppState.SCANNING)
        self._pending_destination = destination
        self._run_background(lambda: scan_library(folder), self._on_scan_done)

    def _on_scan_done(self, result: ScanResult) -> None:
        self.scan_result = result
        self.scan_changed.emit(result)
        log.info("Scanned %d files, %d bytes", len(result.files), result.total_bytes)
        if self._pending_destination is not None:
            dest = self._pending_destination
            self._pending_destination = None
            # Chain the next phase as a *deferred continuation*. At this point
            # ```finished_ok`` has fired but the QThread may still report
            # ``isRunning()``, so calling build_plan_async directly would hit
            # the single-worker guard and silently drop the plan build. Deferring
            # until `QThread.finished` guarantees a strictly sequential chain
            # with no lost phase and no second concurrent worker.
            self._pending_continuation = lambda: self.build_plan_async(dest)
        else:
            self.set_state(AppState.READY)

    # -- plan (pre-check) --------------------------------------------------
    def build_plan_async(self, destination: str) -> None:
        if self.client is None or self.selected_device is None or self.selected_storage is None:
            return
        dest = destination or "Music"

        def _work():
            from .models import TransferItem
            from .models import TransferPlan as P
            from .paths import validate_destination
            from .transfer import TransferEngine as TE

            root = self.selected_storage.mount_path
            dest_norm = validate_destination(root, dest)
            engine = TE(
                client=self.client,
                storage=self.selected_storage,
                destination=dest_norm,
                plan=P(items=[]),
                serial=self.selected_device.serial,
            )
            # build full plan from scan
            items = [TransferItem(source=f, remote_rel=f.rel_path) for f in self.scan_result.files]
            engine.plan = P(items=items)
            # Order matters: probe remote sizes, drop already-present files
            # from the queue, THEN check space. check_space() must only count
            # files that will actually transfer, not the whole local library.
            sizes = engine.remote_sizes()
            engine.build_queue(sizes)
            engine.check_space()
            # Live write-probe before we promise the UI a transfer is possible.
            # Never report a storage as writable without actually testing it.
            self.storage_manager.probe_writable(dest_norm, serial=self.selected_device.serial)
            return engine

        self._run_background(_work, self._on_plan_done)

    def _on_plan_done(self, engine: TransferEngine) -> None:
        self.engine = engine
        self.plan = engine.plan
        self.plan_changed.emit(engine.plan)
        self.set_state(AppState.READY)

    # -- transfer ----------------------------------------------------------
    def start_transfer(self, destination: str) -> None:
        if self.state in (AppState.TRANSFERRING, AppState.PAUSED):
            return  # guard against double-start
        if self.engine is None:
            self.build_plan_async(destination)
            return
        self.set_state(AppState.TRANSFERRING)
        self._run_background(
            lambda: self.engine.run(
                on_item_done=lambda _: self.progress_changed.emit(self.engine.progress)
            ),
            self._on_transfer_done,
        )

    def _on_transfer_done(self, result) -> None:
        if self.engine is not None:
            self.progress_changed.emit(self.engine.progress)
        err_count = self.engine.summary()["errors"] if self.engine else 0
        self.set_state(AppState.COMPLETED if err_count == 0 else AppState.FAILED)

    def pause_transfer(self) -> None:
        if self.engine is not None:
            self.engine.pause()
            self.set_state(AppState.PAUSED)

    def resume_transfer(self) -> None:
        if self.engine is not None:
            self.engine.resume()
            self.set_state(AppState.TRANSFERRING)

    def cancel_transfer(self) -> None:
        if self.engine is not None:
            self.engine.cancel()
            self.set_state(AppState.CANCELLING)

    def retry_errors(self) -> None:
        if self.engine is None:
            return
        for item in self.engine.plan.items:
            if item.status.value == "error":
                from .models import TransferStatus

                item.status = TransferStatus.PENDING
                item.error = None
        self.start_transfer(self.engine.destination)

    # -- helpers -----------------------------------------------------------
    def _run_discovery(self, fn, on_done) -> None:
        """Run a device/storage discovery on its own lightweight worker.

        Discovery is deliberately independent from the scan/transfer single
        worker: it must never be serialized behind a long transfer, and it
        must never block the UI. Its own guard prevents overlapping discovery
        passes (e.g. a burst of refresh button clicks).
        """
        if self._discovery_worker is not None:
            return  # a discovery pass is already in flight — drop duplicates
        worker = _WorkerThread(fn, parent=self)
        worker.finished_ok.connect(on_done)
        worker.failed.connect(self._on_discovery_error)
        worker.finished.connect(lambda _w=worker: self._on_discovery_finished(_w))
        self._discovery_worker = worker
        worker.start()

    def _on_discovery_finished(self, worker: _WorkerThread) -> None:
        if self._discovery_worker is worker:
            self._discovery_worker = None

    def _run_background(self, fn, on_done) -> None:
        if self._worker is not None:
            return  # strictly one worker at a time — never overlap
        worker = _WorkerThread(fn, parent=self)
        worker.finished_ok.connect(on_done)
        worker.failed.connect(self._on_background_error)
        # `finished` fires only after the thread object has actually exited,
        # sitting after `finished_ok`/`failed` in the event queue. This is the
        # single place the worker clears itself, so the next phase can start.
        worker.finished.connect(lambda _w=worker: self._on_worker_finished(_w))
        self._worker = worker
        worker.start()

    def _on_worker_finished(self, worker: _WorkerThread) -> None:
        if self._worker is worker:
            self._worker = None
        cont = self._pending_continuation
        self._pending_continuation = None
        if cont is not None:
            cont()

    def _on_background_error(self, exc: Exception) -> None:
        from .errors import (
            DeviceDisconnectedError,
            DeviceOfflineError,
            StorageUnavailableError,
        )

        if isinstance(exc, (DeviceDisconnectedError, DeviceOfflineError)):
            self.set_state(AppState.DISCONNECTED)
        elif isinstance(exc, StorageUnavailableError):
            self.set_state(AppState.DISCONNECTED)
        else:
            self.set_state(AppState.FAILED)
        self.log_message.emit(str(exc))

    def _save(self) -> None:
        try:
            save_config(self.config)
        except Exception:
            pass

    def save_window_geometry(self, width: int, height: int, x, y) -> None:
        self.config["window"] = {"width": width, "height": height, "x": x, "y": y}
        self._save()

    def shutdown(self) -> None:
        """Gracefully stop background work on app close.

        Cancels any active transfer and blocks until the running worker
        finishes its current file. The active `adb push` is allowed to
        complete before exit — we never destroy a running QThread
        (would raise ``QThread: Destroyed while thread is still running``).
        """
        if self.engine is not None:
            self.engine.cancel()
        self._cancel_retry()
        if self._worker is not None and self._worker.isRunning():
            # Wait for the worker to finish the in-flight file and observe
            # the cancel flag. Worker exits *between* files, so this is
            # bounded by one push duration, not the whole queue.
            self._worker.wait()
            self._worker.quit()
