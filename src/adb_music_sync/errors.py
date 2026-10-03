"""Application error hierarchy.

All ADB failures are normalized into these structured exceptions inside
AdbClient. Higher-level layers (scanner, queue, storage manager) raise and
catch these, never inspect raw stderr text.
"""

from __future__ import annotations


class AdbMusicSyncError(Exception):
    """Base class for all application-specific errors."""


class AdbError(AdbMusicSyncError):
    """Base for ADB-level failures."""


class AdbNotFoundError(AdbError):
    """The adb executable could not be located."""


class AdbCommandError(AdbError):
    """An adb command failed with a non-zero exit / structured error."""


class DeviceDisconnectedError(AdbError):
    """The device is no longer reachable (transport lost mid-operation)."""


class DeviceOfflineError(AdbError):
    """The device is offline."""


class DeviceUnauthorizedError(AdbError):
    """The device is connected but has not authorized USB debugging."""


class NoDeviceError(AdbError):
    """No device is currently selected / connected."""


class StorageUnavailableError(AdbError):
    """The selected storage (e.g. SD card) is gone or not writable."""


class InsufficientSpaceError(AdbError):
    """The destination storage does not have enough free space."""


class TransferError(AdbMusicSyncError):
    """A file transfer failed for a non-fatal, per-file reason."""


class InvalidDestinationError(AdbMusicSyncError):
    """The destination path is unsafe / escapes the storage root."""
