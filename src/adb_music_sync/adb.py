"""ADB abstraction layer.

This is the ONLY module that shells out to `adb`. Every other component
(GUI, scanner, storage manager, transfer queue) talks to ADB exclusively
through :class:`AdbClient`, which lets tests substitute a
``FakeAdbClient`` with no real device attached.

Design notes:
- subprocess is always invoked with an argument list (never ``shell=True``).
- ADB output/errors are mapped to structured exceptions in
  :mod:`adb_music_sync.errors`.
- Device storage discovery analyses ``sm list-volumes`` / ``/storage``
  rather than trusting a single hard-coded path.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    AdbCommandError,
    AdbNotFoundError,
    DeviceDisconnectedError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
)
from .models import AdbTarget, Device, DeviceState

# Keyword fragments used to classify a failed adb invocation. These are
# normalized only here, once — not scattered across the project.
# Order matters: more specific (unauthorized, offline) come BEFORE the broad
# disconnect markers, so a genuine "device offline" maps to Offline rather
# than Disconnected.
_UNAUTHORIZED_MARKERS = ("unauthorized", "adb_vendor_keys", "adb_keys")
_OFFLINE_MARKERS = ("device offline", "offline")
_DISCONNECT_MARKERS = (
    "no devices/emulators found",
    "device not found",
    "not found",
    "failed to get feature set",
    "not a device",
    "device disconnected",
    "connection reset",
    "broken pipe",
    "remote closed the connection",
    "closed",
)


def _posix_quote(path: str) -> str:
    """Single-quote a string for an Android (POSIX) shell command.

    This is the single, canonical quoting mechanism for every path/argument
    the ADB layer hands to ``adb shell``. It escapes embedded single quotes
    using the standard ``'\\''`` idiom, so names containing ``'``, spaces,
    ``&``, ``#``, ``$``, backticks, parens, quotes and Unicode survive the
    device-side shell intact.
    """
    return "'" + path.replace("'", "'\\''") + "'"


def _find_adb() -> str:
    """Locate the adb executable: bundled platform-tools first, then PATH."""
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    # Bundled: project_root/platform-tools/adb(.exe)
    for root in (
        here.parent.parent,  # src/adb_music_sync -> adb-music-sync
        here.parent.parent.parent,  # handle deeper layouts
    ):
        candidates.append(root / "platform-tools" / "adb.exe")
        candidates.append(root / "platform-tools" / "adb")

    for c in candidates:
        if c.is_file():
            return str(c)

    in_path = shutil.which("adb")
    if in_path:
        return in_path
    raise AdbNotFoundError(
        "adb not found. Put platform-tools/adb next to the app or add adb to PATH."
    )


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class AdbClient:
    """Thin, test-friendly wrapper around the adb CLI."""

    def __init__(self, adb_path: str | None = None) -> None:
        self.adb_path = adb_path or _find_adb()

    # -- low-level ---------------------------------------------------------
    def _run(
        self,
        args: list[str],
        *,
        serial: str | None = None,
        target: AdbTarget | None = None,
        timeout: float = 60.0,
    ) -> CommandResult:
        cmd = [self.adb_path]
        if target is not None:
            cmd += target.args()
        elif serial is not None:
            cmd += ["-s", serial]
        cmd += list(args)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbNotFoundError(f"adb not found: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbCommandError(f"adb timed out: {' '.join(args)}") from exc
        # decode may contain nulls from shell byte dumps; keep as-is
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def _run_checked(
        self,
        args: list[str],
        *,
        serial: str | None = None,
        target: AdbTarget | None = None,
        timeout: float = 60.0,
    ) -> CommandResult:
        r = self._run(args, serial=serial, target=target, timeout=timeout)
        if r.ok:
            return r
        self._raise_for(r)
        raise AdbCommandError(f"adb {' '.join(args)} failed")

    def _raise_for(self, r: CommandResult) -> None:
        blob = (r.stderr + " " + r.stdout).lower()
        # Specific states first, so "device offline" -> OfflineError and
        # "unauthorized" -> UnauthorizedError, not the broad Disconnected.
        if any(m in blob for m in _UNAUTHORIZED_MARKERS):
            raise DeviceUnauthorizedError(blob.strip() or "device unauthorized")
        if any(m in blob for m in _OFFLINE_MARKERS):
            raise DeviceOfflineError(blob.strip() or "device offline")
        if any(m in blob for m in _DISCONNECT_MARKERS):
            raise DeviceDisconnectedError(blob.strip() or "device disconnected")
        raise AdbCommandError(blob.strip() or f"adb exited {r.returncode}")

    # -- device discovery --------------------------------------------------
    def list_devices(self) -> list[Device]:
        r = self._run(["devices", "-l"], timeout=20.0)
        devices: list[Device] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            devices.append(self._parse_device_line(line))
        return devices

    def _parse_device_line(self, line: str) -> Device:
        """Parse one ``adb devices -l`` entry into a :class:`Device`.

        Robust against the ``?`` host serial and optional extra key/value
        tokens (``product:``, ``model:``, ``device:``, ``transport_id:``,
        and an unexpected ``usb:``/``emulator-...:`` pair).
        """
        parts = line.split()
        serial, state_str = parts[0], parts[1]
        try:
            state = DeviceState(state_str)
        except ValueError:
            state = DeviceState.UNKNOWN
        transport_id: int | None = None
        product: str | None = None
        model: str | None = None
        device_name: str | None = None
        # Remaining tokens are `key:value`; the transport is often a bare
        # integer token (rarely) or attached to `transport_id:`.
        tokens = parts[2:]
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if ":" in tok:
                key, _, val = tok.partition(":")
                if key == "transport_id":
                    transport_id = self._parse_int(val)
                elif key == "product":
                    product = val
                elif key == "model":
                    model = val
                elif key == "device":
                    device_name = val
                # ignore other keys (usb:, emulator-...:)
            elif i + 1 < len(tokens) and tokens[i + 1].startswith("transport_id:"):
                # bare integer immediately before transport_id:
                transport_id = self._parse_int(tok)
            i += 1
        return Device(
            serial=serial,
            state=state,
            transport_id=transport_id,
            product=product or None,
            model=model or None,
            device_name=device_name or None,
        )

    @staticmethod
    def _parse_int(value: str) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def getprop(self, key: str, *, serial: str | None = None, target: AdbTarget | None = None) -> str:
        """Return ``getprop <key>`` (empty string if unset). Used for
        ``ro.serialno`` / ``ro.product.model`` to build a stable identity."""
        out = self.shell_list(f"getprop {key}", serial=serial, target=target)
        return out.strip()

    def resolve_stable_id(self, device: Device) -> str | None:
        """Best-effort persistent identity for a device.

        Reads ``ro.serialno`` via the live selector. Falls back (in order) to
        a normal host serial, the transport id, or ``None`` when all are
        unusable. A transport id is never a persistent id — this only reads
        it as a last resort for *display*, not storage."""
        raw = None
        try:
            raw = self.getprop("ro.serialno", target=device.selector)
        except Exception:  # noqa: BLE001 — discovery must never hard-fail
            raw = None
        val = (raw or "").strip()
        if val and val not in ("unknown", "null", ""):
            return val
        if device.serial and not device._serial_unreliable():
            return device.serial
        if device.transport_id is not None:
            return f"transport-{device.transport_id}"
        return None

    def wait_for_device(self, serial: str | None = None, target: AdbTarget | None = None, timeout: float = 30.0) -> None:
        self._run_checked(["wait-for-device"], serial=serial, target=target, timeout=timeout)

    # -- shell / filesystem ------------------------------------------------
    def shell(self, command: str, *, serial: str | None = None, target: AdbTarget | None = None) -> str:
        """Run a raw shell command on the device, returning stdout.

        `command` is split with shlex (single string) so quoting/Unicode is
        preserved; adb receives the command as separate argv tokens it joins.
        """
        r = self._run_checked(
            ["shell", *shlex.split(command)], serial=serial, target=target, timeout=60.0
        )
        return r.stdout.rstrip("\n")

    def shell_list(self, command: str, *, serial: str | None = None, target: AdbTarget | None = None) -> str:
        """Tolerant variant: returns empty string on shell errors it can't
        normalize, still raising real device errors."""
        r = self._run(["shell", *shlex.split(command)], serial=serial, target=target, timeout=60.0)
        if r.ok:
            return r.stdout.rstrip("\n")
        self._raise_for(r)
        return ""

    # -- file transfer -----------------------------------------------------
    def push(self, local: str, remote: str, *, serial: str | None = None, target: AdbTarget | None = None) -> None:
        self._run_checked(["push", local, remote], serial=serial, target=target, timeout=3600.0)

    def shell_mkdir(self, path: str, *, serial: str | None = None, target: AdbTarget | None = None) -> None:
        self._run_checked(["shell", "mkdir", "-p", _posix_quote(path)], serial=serial, target=target)

    def shell_mv(self, src: str, dst: str, *, serial: str | None = None, target: AdbTarget | None = None) -> None:
        self._run_checked(["shell", "mv", _posix_quote(src), _posix_quote(dst)], serial=serial, target=target)

    def shell_rm(self, path: str, *, serial: str | None = None, target: AdbTarget | None = None) -> None:
        """Remove a single file (used only for our own .part files)."""
        self._run_checked(["shell", "rm", "-f", _posix_quote(path)], serial=serial, target=target)

    def shell_stat_size(self, remote: str, *, serial: str | None = None, target: AdbTarget | None = None) -> int | None:
        """Return remote file size in bytes, or None if it does not exist.

        Uses a portable `stat`-based probe that avoids relying on `ls -l`
        column splitting (which breaks on spaces/Unicode). The path is
        POSIX-quoted via :func:`_posix_quote` so `'`, `"`, `&`, `#`, `$`,
        backticks, parens and Unicode survive the device shell intact.
        """
        script = f"stat -c %s {_posix_quote(remote)} 2>/dev/null"
        out = self.shell_list(script, serial=serial, target=target).strip()
        if not out:
            return None
        try:
            return int(out.splitlines()[0])
        except ValueError:
            return None

    def shell_touch(self, path: str, *, serial: str | None = None, target: AdbTarget | None = None) -> None:
        """Create an empty file at `path` (used only for write probes)."""
        self._run_checked(["shell", "touch", _posix_quote(path)], serial=serial, target=target, timeout=30.0)
