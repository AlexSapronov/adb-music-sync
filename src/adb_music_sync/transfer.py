"""Transfer engine.

Moves files one-by-one via ``adb push`` into a temporary ``.part`` name, then
renames on success. Handles per-file errors (queue continues), device
disconnect (queue pauses, state preserved), and missing storage (stops, never
falls back to another volume). Completed files are never re-transferred.
"""

from __future__ import annotations

import posixpath
import threading
from dataclasses import dataclass, field

from .adb import AdbClient
from .errors import (
    DeviceDisconnectedError,
    DeviceOfflineError,
    InsufficientSpaceError,
    StorageUnavailableError,
    TransferError,
)
from .models import AppState, StorageTarget, TransferItem, TransferPlan, TransferStatus
from .paths import join_rel


@dataclass
class TransferProgress:
    transferred_files: int = 0
    transferred_bytes: int = 0
    current_file: str = ""
    current_index: int = 0


@dataclass
class TransferEngine:
    client: AdbClient
    storage: StorageTarget
    destination: str
    plan: TransferPlan
    serial: str

    # mutable runtime state
    state: AppState = AppState.READY
    _pause_event: threading.Event = field(default_factory=threading.Event)
    _cancel_event: threading.Event = field(default_factory=threading.Event)
    _progress: TransferProgress = field(default_factory=TransferProgress)
    _current_item: TransferItem | None = None
    _raise_on_done: Exception | None = None

    # -- plan building (pure-ish, unit-testable) ---------------------------
    def build_queue(self, remote_sizes: dict[str, int | None]) -> list[TransferItem]:
        """Decide what to transfer given a map of remote_rel -> device size."""
        queued: list[TransferItem] = []
        for item in self.plan.items:
            remote = remote_sizes.get(item.remote_rel)
            if remote is None:
                item.status = TransferStatus.PENDING
                queued.append(item)
            elif remote == item.source.size:
                item.status = TransferStatus.SKIPPED
            else:
                # wrong size -> re-transfer
                item.status = TransferStatus.PENDING
                queued.append(item)
        self.plan.items = self.plan.items  # no-op; items mutated in place
        return queued

    def remote_sizes(self) -> dict[str, int | None]:
        """Probe device sizes for every expected remote relative path."""
        sizes: dict[str, int | None] = {}
        for item in self.plan.items:
            remote = join_rel(self.destination, item.remote_rel)
            sizes[item.remote_rel] = self.client.shell_stat_size(remote, serial=self.serial)
        return sizes

    def check_space(self) -> None:
        need = self.plan.to_transfer_bytes
        available = self.storage.free_bytes
        if available < need:
            raise InsufficientSpaceError(f"not enough space: need {need} bytes, have {available}")

    # -- execution ---------------------------------------------------------
    def transfer_one(self, item: TransferItem) -> None:
        remote = join_rel(self.destination, item.remote_rel)
        part = remote + ".part"
        parent = posixpath.dirname(remote)

        self.client.shell_mkdir(parent, serial=self.serial)
        item.status = TransferStatus.TRANSFERRING
        self._progress.current_file = item.remote_rel

        try:
            self.client.push(item.source.local_path, part, serial=self.serial)
            self.client.shell_mv(part, remote, serial=self.serial)
            item.status = TransferStatus.OK
            self._progress.transferred_files += 1
            self._progress.transferred_bytes += item.source.size
        except (DeviceDisconnectedError, DeviceOfflineError, StorageUnavailableError):
            # Queue-stopping conditions: propagate unchanged.
            item.status = TransferStatus.ERROR
            item.error = "device or storage unavailable"
            raise
        except Exception as exc:  # per-file failure: wrap, let queue continue
            item.status = TransferStatus.ERROR
            item.error = str(exc)
            self._cleanup_part(part)
            raise TransferError(f"transfer failed for {item.remote_rel}: {exc}") from exc

    def _cleanup_part(self, part: str) -> None:
        try:
            self.client.shell_rm(part, serial=self.serial)
        except Exception:
            pass

    # -- full run loop -----------------------------------------------------
    def run(self, on_item_done=None) -> None:
        """Run the queue. May raise DeviceDisconnectedError/StorageUnavailableError
        to signal a stop-the-queue condition; per-file errors are tolerated."""
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self.state = AppState.TRANSFERRING
        queued = [i for i in self.plan.items if i.status is TransferStatus.PENDING]
        self.check_space()

        try:
            for idx, item in enumerate(queued, start=1):
                self._progress.current_index = idx
                self._ensure_not_disconnected()

                # pause gate
                while self._pause_event.is_set() and not self._cancel_event.is_set():
                    self.state = AppState.PAUSED
                    self._pause_event.wait(0.2)
                if self._cancel_event.is_set():
                    self.state = AppState.CANCELLING
                    break

                self.state = AppState.TRANSFERRING
                try:
                    self.transfer_one(item)
                except TransferError:
                    # per-file error: record and continue with the next file
                    if on_item_done is not None:
                        on_item_done(item)
                    continue
                if on_item_done is not None:
                    on_item_done(item)

            if self._cancel_event.is_set():
                self.state = AppState.CANCELLING
                return
            self.state = AppState.COMPLETED if self._error_count() == 0 else AppState.FAILED
        except DeviceDisconnectedError:
            self.state = AppState.DISCONNECTED
            raise
        except StorageUnavailableError:
            self.state = AppState.DISCONNECTED
            raise
        finally:
            pass

    def _error_count(self) -> int:
        return sum(1 for i in self.plan.items if i.status is TransferStatus.ERROR)

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _ensure_not_disconnected(self) -> None:
        # Fast-path: rely on `push`/`shell` raising on the next call.
        return

    @property
    def progress(self) -> TransferProgress:
        return self._progress

    def summary(self) -> dict[str, int]:
        ok = sum(1 for i in self.plan.items if i.status is TransferStatus.OK)
        skipped = sum(1 for i in self.plan.items if i.status is TransferStatus.SKIPPED)
        errors = sum(1 for i in self.plan.items if i.status is TransferStatus.ERROR)
        return {"ok": ok, "skipped": skipped, "errors": errors}
