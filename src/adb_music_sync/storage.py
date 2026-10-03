"""Android storage discovery.

Detects the internal user storage and any physical/removable SD cards on a
device, without trusting a single hard-coded path. ``/sdcard`` is treated as
an alias of internal storage, NOT a physical SD card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .adb import AdbClient
from .errors import StorageUnavailableError
from .models import StorageTarget

# Physical SD cards on modern Android mount at /storage/<UUID> (e.g. A12B-34CD).
_SDCARD_RE = re.compile(r"^[0-9A-F]{4,}-[0-9A-F]{4,}$")
_EMULATED_INTERNAL = "/storage/emulated/0"


@dataclass
class StorageManager:
    client: AdbClient

    def list_storages(self, serial: str) -> list[StorageTarget]:
        """Return writable storage targets for a device (internal first)."""
        mounts = self._discover_mounts(serial)
        storages: list[StorageTarget] = []

        internal = self._internal_storage(serial, mounts)
        if internal is not None:
            storages.append(internal)

        for mp in mounts:
            if self._is_removable(mp):
                storages.append(self._removable_storage(serial, mp))

        # De-duplicate on mount path (defensive).
        seen: set[str] = set()
        unique: list[StorageTarget] = []
        for s in storages:
            if s.mount_path in seen:
                continue
            seen.add(s.mount_path)
            unique.append(s)
        return unique

    # -- discovery helpers -------------------------------------------------
    def _discover_mounts(self, serial: str) -> list[str]:
        """Collect candidate storage mount points from `sm list-volumes`."""
        mounts: list[str] = []
        try:
            out = self.client.shell_list("sm list-volumes", serial=serial)
        except Exception:  # some ROMs lack `sm`; fall back to /storage scan
            out = ""
        mounts.extend(_parse_sm_volumes(out))

        try:
            listing = self.client.shell_list("ls /storage", serial=serial)
        except Exception:
            listing = ""
        mounts.extend(_parse_storage_listing(listing))

        # Always include the canonical internal path if we found nothing.
        if not mounts:
            mounts.append(_EMULATED_INTERNAL)
        return _dedupe(mounts)

    def _internal_storage(self, serial: str, mounts: list[str]) -> StorageTarget | None:
        # Prefer the standard emulated path if present, else fall back.
        candidates = [m for m in mounts if m.startswith("/storage/emulated")]
        if not candidates and any(
            m == "/sdcard" or m.startswith("/storage/sdcard") for m in mounts
        ):
            candidates = ["/sdcard"]
        if not candidates:
            candidates = [_EMULATED_INTERNAL]
        mp = candidates[0]
        free, total = self._free_space(serial, mp)
        return StorageTarget(
            mount_path=mp,
            label="Internal storage",
            is_removable=False,
            free_bytes=free,
            total_bytes=total,
        )

    def _removable_storage(self, serial: str, mp: str) -> StorageTarget:
        free, total = self._free_space(serial, mp)
        return StorageTarget(
            mount_path=mp,
            label="SD card",
            is_removable=True,
            free_bytes=free,
            total_bytes=total,
        )

    def _free_space(self, serial: str, mp: str) -> tuple[int, int]:
        out = self.client.shell_list(f"df -k {_sh_single(mp)}", serial=serial)
        parsed = _parse_df_kb(out)
        if parsed is None:
            # fall back to statvfs-like via toybox `df` long form
            return 0, 0
        return parsed

    def _is_removable(self, mp: str) -> bool:
        if mp.startswith("/storage/emulated") or mp == "/sdcard":
            return False
        if mp.startswith("/storage/sdcard"):
            return False
        base = mp.rstrip("/").split("/")[-1]
        return bool(_SDCARD_RE.match(base))

    def ensure_still_available(self, storage: StorageTarget, serial: str) -> StorageTarget:
        """Re-check a selected storage is still present.

        Raises :class:`StorageUnavailableError` if it vanished (e.g. SD
        card removed) — the caller must NOT silently fall back to another
        storage.
        """
        current = self.list_storages(serial)
        for s in current:
            if s.mount_path == storage.mount_path:
                return s
        raise StorageUnavailableError(f"storage {storage.mount_path} is no longer available")


# -- parsing helpers (pure functions, unit-testable) ------------------------


def _parse_sm_volumes(out: str) -> list[str]:
    """Parse `sm list-volumes` output, returning mount paths when visible.

    Typical lines:
      emulated;0 mounted null null
      public:179,0 mounted A12B-34CD
      private;null unmountable null
    """
    mounts: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        if fields[0].startswith("emulated"):
            continue
        if "mounted" not in fields:
            continue
        # public:<disk>,<part> mounted <uuid>
        ident = fields[-1]
        if not _SDCARD_RE.match(ident):
            continue
        mounts.append(f"/storage/{ident}")
    return mounts


def _parse_storage_listing(out: str) -> list[str]:
    """Parse `ls /storage` output into /storage/<name> mount candidates."""
    mounts: list[str] = []
    for line in out.splitlines():
        name = line.strip()
        if not name:
            continue
        if name in ("emulated", "self", "sdcard0", "sdcard1"):
            continue
        if _SDCARD_RE.match(name):
            mounts.append(f"/storage/{name}")
    return mounts


def _parse_df_kb(out: str) -> tuple[int, int] | None:
    """Parse `df -k <path>` output -> (free_bytes, total_bytes).

    ``df -k`` prints: ``Filesystem 1K-blocks Used Available Use% Mounted``.
    Both GNU coreutils and toybox follow this column order; the last token is
    the mount point, and ``Use%`` is percent. We read column 1 (total 1K-blocks)
    and column 3 (available 1K-blocks).
    """
    lines = [line for line in out.splitlines() if line.strip() and "Filesystem" not in line]
    if not lines:
        return None
    nums = lines[-1].split()
    numbers: list[int] = []
    for tok in nums[1:]:
        m = re.match(r"^(\d+)[KkMm]?%?$", tok)
        if m:
            numbers.append(int(m.group(1)))
    if len(numbers) < 4:
        return None
    total_1k = numbers[0]
    avail_1k = numbers[2]
    return avail_1k * 1024, total_1k * 1024


def _sh_single(path: str) -> str:
    """Single-quote a path for use inside an already-array-bound shell command.

    The command string is passed as ONE argv element to `adb shell`, so we
    only need to protect against the device shell expanding the path. We use
    single quotes and escape embedded single quotes.
    """
    return "'" + path.replace("'", "'\\''") + "'"


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        if it not in out:
            out.append(it)
    return out
