"""Core data models: devices, storages, scan results, transfer items."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DeviceState(str, Enum):
    DEVICE = "device"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"


@dataclass
class Device:
    """A connected Android device.

    Splits the two distinct notions the ADB layer needs:

    * ``serial`` / ``transport_id`` — how ADB addresses the device *right now*
      (session-specific; a host serial can literally be ``?`` and a transport
      id changes after every reconnect);
    * ``stable_id`` — a persistent identity (``ro.serialno``) for settings.

    ``model`` / ``product`` / ``device_name`` come straight from
    ``adb devices -l`` and are human-facing metadata.
    """

    serial: str  # host-side ADB serial (may be "?" or empty)
    state: DeviceState
    transport_id: int | None = None
    product: str | None = None
    model: str | None = None
    device_name: str | None = None
    stable_id: str | None = None  # ro.serialno if resolvable (persistent)

    @property
    def is_ready(self) -> bool:
        return self.state is DeviceState.DEVICE

    @property
    def display_name(self) -> str:
        """Human-readable device label; falls back to the raw serial."""
        if self.model:
            return self.model.replace("_", " ")
        return self.serial or "(unknown)"

    @property
    def selector(self) -> AdbTarget:
        """The runtime ADB selector for this device right now.

        Prefers ``-t <transport_id>`` when the host serial is unreliable
        (``?`` / empty), else ``-s <serial>``. Never emits ``-s ?``.
        """
        if self.transport_id is not None and self._serial_unreliable():
            return AdbTarget(transport_id=self.transport_id)
        return AdbTarget(serial=self.serial)

    def _serial_unreliable(self) -> bool:
        return not self.serial or self.serial in ("?", "unknown", "null")


@dataclass(frozen=True)
class AdbTarget:
    """How to reach one device in the current session — serial or transport.

    Exactly one field is set. Every ADB command funnels through this so the
    ``-s ?`` / ``-t N`` choice is centralized in one place (AdbClient).
    """

    serial: str | None = None
    transport_id: int | None = None

    def args(self) -> list[str]:
        if self.transport_id is not None:
            return ["-t", str(self.transport_id)]
        if self.serial is not None:
            return ["-s", self.serial]
        return []


@dataclass
class StorageTarget:
    """A storage volume on the Android device.

    ``writable`` is intentionally NOT a property of this model: writability is
    a device state that can change mid-session and must be probed live via the
    AdbClient (see ``StorageManager.probe_writable``). Reporting a storage as
    writable without probing would be a lie.
    """

    mount_path: str  # e.g. /storage/emulated/0 or /storage/A12B-34CD
    label: str  # human-readable: "Internal storage" / "SD card"
    is_removable: bool = False
    free_bytes: int = 0
    total_bytes: int = 0


class AppState(str, Enum):
    IDLE = "IDLE"
    SCANNING = "SCANNING"
    READY = "READY"
    TRANSFERRING = "TRANSFERRING"
    PAUSED = "PAUSED"
    DISCONNECTED = "DISCONNECTED"
    CANCELLING = "CANCELLING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TransferStatus(str, Enum):
    PENDING = "pending"
    TRANSFERRING = "transferring"
    OK = "ok"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class LocalFileRef:
    """A source file on the PC plus its relative path."""

    local_path: str
    rel_path: str  # POSIX-style relative path, e.g. Artist/Album/track.flac
    size: int


@dataclass
class TransferItem:
    source: LocalFileRef
    remote_rel: str | None = None  # defaults to source.rel_path
    status: TransferStatus = TransferStatus.PENDING
    error: str | None = None

    def __post_init__(self) -> None:
        if self.remote_rel is None:
            self.remote_rel = self.source.rel_path

    @property
    def remote_path_prefix(self) -> str:
        return self.remote_rel


@dataclass
class ScanResult:
    files: list[LocalFileRef] = field(default_factory=list)
    total_bytes: int = 0


@dataclass
class TransferPlan:
    """Outcome of comparing local library against device contents."""

    items: list[TransferItem] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return len(self.items)

    @property
    def to_transfer(self) -> list[TransferItem]:
        return [i for i in self.items if i.status is TransferStatus.PENDING]

    @property
    def to_transfer_bytes(self) -> int:
        return sum(i.source.size for i in self.to_transfer)
