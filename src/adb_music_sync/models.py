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
    serial: str
    state: DeviceState

    @property
    def is_ready(self) -> bool:
        return self.state is DeviceState.DEVICE


@dataclass
class StorageTarget:
    """A writable storage volume on the Android device."""

    mount_path: str  # e.g. /storage/emulated/0 or /storage/A12B-34CD
    label: str  # human-readable: "Internal storage" / "SD card"
    is_removable: bool = False
    free_bytes: int = 0
    total_bytes: int = 0

    @property
    def writable(self) -> bool:
        return self.free_bytes > 0 or True  # determined at check time, not here


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
