"""Android storage discovery.

Detects the internal user storage and any physical/removable SD cards on a
device, without trusting a single hard-coded path. ``/sdcard`` is treated as
an alias of internal storage, NOT a physical SD card.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from .adb import AdbClient, _posix_quote
from .errors import (
    AdbError,
    DeviceDisconnectedError,
    DeviceOfflineError,
    StorageUnavailableError,
)
from .models import AdbTarget, StorageTarget

# Physical SD cards mount at /storage/<NAME> where NAME is a UUID-style token
# (A12B-34CD) OR a vendor-named mount (external_sd, sdcard1, extSdCard, ...).
# ``sm list-volumes`` is the authoritative source (public + mounted); this
# regex is only a *fallback* heuristic for the `ls /storage` path.
_SDCARD_RE = re.compile(r"^[0-9A-F]{4,}-[0-9A-F]{4,}$")
_EMULATED_INTERNAL = "/storage/emulated/0"
# Mount tokens we never treat as removable SD cards even in a fallback scan.
_RESERVED_STORAGE_NAMES = frozenset({"emulated", "self", "sdcard0", "sdcard1", "sdcard"})
# Additional clearly-vendor removable names accepted in the *fallback* only
# (sm list-volumes already covers these authoritatively at runtime).
_VENDOR_REMOVABLE_RE = re.compile(
    r"^(external_sd|extsdcard|sd ?card|microsd|sdcard\d+|storage/sdcard\d+)$",
    re.IGNORECASE,
)


@dataclass
class StorageManager:
    client: AdbClient

    def list_storages(
        self, serial: str | None = None, target: AdbTarget | None = None
    ) -> list[StorageTarget]:
        """Return writable storage targets for a device (internal first).

        ``target`` (an :class:`AdbTarget`) selects the device for every
        command; ``serial`` is kept only as a legacy convenience for the old
        string-based callers and maps to ``-s <serial>``.
        """
        volumes = self._discover_mounts(serial=serial, target=target)
        storages: list[StorageTarget] = []

        internal = self._internal_storage(serial=serial, target=target, mounts=volumes.internal)
        if internal is not None:
            storages.append(internal)

        for mp in volumes.removable:
            storages.append(self._removable_storage(serial=serial, target=target, mp=mp))

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
    def _discover_mounts(self, serial: str | None, target: AdbTarget | None) -> _VolumeSet:
        """Collect storage mount points.

        ``sm list-volumes`` is authoritative: a ``public ... mounted <name>``
        volume is a physical removable card regardless of the name's shape
        (UUID or vendor-named). It also authoritatively reports ``private``
        and ``emulated`` volumes, which we exclude. On ROMs without `sm`,
        fall back to scanning ``ls /storage`` with safe heuristics.
        """
        sm_out = ""
        try:
            sm_out = self.client.shell_list("sm list-volumes", serial=serial, target=target)
        except Exception:  # some ROMs lack `sm`; fall back to /storage scan
            sm_out = ""
        sm_internal, sm_removable = _parse_sm_volumes(sm_out)

        listing_out = ""
        try:
            listing_out = self.client.shell_list("ls /storage", serial=serial, target=target)
        except Exception:
            listing_out = ""
        fallback = _parse_storage_listing(listing_out)

        internal = (
            list(sm_internal) if sm_internal else ([_EMULATED_INTERNAL] if not fallback else [])
        )
        removable = list(sm_removable)
        if not removable and not sm_removable and fallback:
            # sm gave nothing useful — use the /storage fallback as SD candidates.
            removable = list(fallback)

        return _VolumeSet(internal=_dedupe(internal), removable=_dedupe(removable))

    def _internal_storage(
        self, serial: str | None, target: AdbTarget | None, mounts: list[str]
    ) -> StorageTarget | None:
        candidates = [m for m in mounts if m.startswith("/storage/emulated")]
        if not candidates:
            candidates = [_EMULATED_INTERNAL]
        mp = candidates[0]
        free, total = self._free_space(serial, target, mp)
        return StorageTarget(
            mount_path=mp,
            label="Internal storage",
            is_removable=False,
            free_bytes=free,
            total_bytes=total,
        )

    def _removable_storage(
        self, serial: str | None, target: AdbTarget | None, mp: str
    ) -> StorageTarget:
        free, total = self._free_space(serial, target, mp)
        return StorageTarget(
            mount_path=mp,
            label="SD card",
            is_removable=True,
            free_bytes=free,
            total_bytes=total,
        )

    def _free_space(self, serial: str | None, target: AdbTarget | None, mp: str) -> tuple[int, int]:
        out = self.client.shell_list(f"df -k {_posix_quote(mp)}", serial=serial, target=target)
        parsed = _parse_df_kb(out)
        if parsed is None:
            return 0, 0
        return parsed

    def ensure_still_available(
        self, storage: StorageTarget, serial: str | None = None, target: AdbTarget | None = None
    ) -> StorageTarget:
        """Re-check a selected storage is still present.

        Raises :class:`StorageUnavailableError` if it vanished (e.g. SD
        card removed) — the caller must NOT silently fall back to another
        storage.
        """
        current = self.list_storages(serial=serial, target=target)
        for s in current:
            if s.mount_path == storage.mount_path:
                return s
        raise StorageUnavailableError(f"storage {storage.mount_path} is no longer available")

    def probe_writable(
        self, destination: str, serial: str | None = None, target: AdbTarget | None = None
    ) -> None:
        """Verify the destination directory actually accepts writes.

        Ensures `destination` exists (creating it if needed via the ADB layer),
        then creates a uniquely-named empty file inside it and removes it.
        Raises :class:`StorageUnavailableError` on a writability failure
        (read-only mount, vanished SD card, permission denied, ...); transport
        errors propagate as-is. Only ever touches paths this application owns
        (the destination directory it may create, and the probe file).
        """
        probe_name = f".adb-music-sync-write-test-{uuid.uuid4().hex}"
        probe_path = f"{destination.rstrip('/')}/{probe_name}"
        try:
            # The destination may not exist yet (a brand-new target folder the
            # transfer itself would create). Create it first so the probe does
            # not fail with "No such file or directory" on a healthy card.
            self.client.shell_mkdir(destination, serial=serial, target=target)
            self.client.shell_touch(probe_path, serial=serial, target=target)
        except (DeviceDisconnectedError, DeviceOfflineError):
            # transport/device state, not a writability verdict — propagate as-is
            raise
        except AdbError as exc:
            raise StorageUnavailableError(
                f"destination {destination} is not writable: {exc}"
            ) from exc
        finally:
            # Always attempt cleanup; the probe file is ours alone. Do NOT
            # remove the destination directory we may have just created.
            try:
                self.client.shell_rm(probe_path, serial=serial, target=target)
            except Exception:
                pass


@dataclass(frozen=True)
class _VolumeSet:
    internal: list[str]
    removable: list[str]


# -- parsing helpers (pure functions, unit-testable) ------------------------


def _is_valid_mount_token(name: str) -> bool:
    """A storage mount name is safe to turn into ``/storage/<name>``.

    Rejects empty/whitespace, dot/relative segments, path separators and the
    reserved internal names, so a stray directory can never be trusted as an
    SD card.
    """
    if not name or name != name.strip():
        return False
    if name in _RESERVED_STORAGE_NAMES:
        return False
    if any(c in name for c in "/\\\0"):
        return False
    if name.startswith(".") or ".." in name:
        return False
    return True


def _parse_sm_volumes(out: str) -> tuple[list[str], list[str]]:
    """Parse `sm list-volumes` -> (internal mounts, removable mounts).

    ``sm list-volumes`` is authoritative about which volumes are
    public/removable vs private/emulated, so it is the primary source and does
    NOT require a UUID-shaped name. Typical lines:

      emulated;0 mounted null null
      private mounted null
      public:179,25 mounted external_sd        <- vendor-named removable
      public:179,1 mounted A12B-34CD           <- UUID removable
      public:179,2 unmounted null              <- NOT mounted, ignore
    """
    internal: list[str] = []
    removable: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        kind = fields[0]
        if "mounted" not in fields:
            continue
        name = fields[-1]
        if kind.startswith("emulated"):
            if name in ("null", "unknown") or not name:
                internal.append(_EMULATED_INTERNAL)
            continue
        if kind.startswith("private"):
            # private/adopted storage is NOT removable external storage
            continue
        if kind.startswith("public"):
            # public + mounted => a real physical (removable) volume.
            if name in ("null", "unknown") or not name or name == "null":
                continue
            if not _is_valid_mount_token(name):
                continue
            removable.append(f"/storage/{name}")
    return _dedupe(internal), _dedupe(removable)


def _parse_storage_listing(out: str) -> list[str]:
    """Parse `ls /storage` output into removable mount candidates (fallback).

    Only used when `sm list-volumes` is unavailable. Excludes internal names
    and applies conservative heuristics (UUID-style or a small set of clearly
    vendor-named removable tokens) so a random directory is never blindly
    treated as an SD card.
    """
    mounts: list[str] = []
    for line in out.splitlines():
        name = line.strip()
        if not name:
            continue
        if name in _RESERVED_STORAGE_NAMES:
            continue
        if _SDCARD_RE.match(name) or _VENDOR_REMOVABLE_RE.match(name):
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


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        if it not in out:
            out.append(it)
    return out
