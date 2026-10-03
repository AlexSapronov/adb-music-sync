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
from .models import Device, DeviceState

# Keyword fragments used to classify a failed adb invocation. These are
# normalized only here, once — not scattered across the project.
_OFFLINE_MARKERS = ("offline", "device offline")
_UNAUTHORIZED_MARKERS = ("unauthorized",)
_DISCONNECT_MARKERS = (
    "no devices/emulators found",
    "device offline",
    "device not found",
    "failed to get feature set",
    "not a device",
)


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
        timeout: float = 60.0,
    ) -> CommandResult:
        cmd = [self.adb_path]
        if serial is not None:
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
        timeout: float = 60.0,
    ) -> CommandResult:
        r = self._run(args, serial=serial, timeout=timeout)
        if r.ok:
            return r
        self._raise_for(r)
        raise AdbCommandError(f"adb {' '.join(args)} failed")

    def _raise_for(self, r: CommandResult) -> None:
        blob = (r.stderr + " " + r.stdout).lower()
        if any(m in blob for m in _DISCONNECT_MARKERS):
            raise DeviceDisconnectedError(blob.strip() or "device disconnected")
        if any(m in blob for m in _OFFLINE_MARKERS):
            raise DeviceOfflineError(blob.strip() or "device offline")
        if any(m in blob for m in _UNAUTHORIZED_MARKERS):
            raise DeviceUnauthorizedError(blob.strip() or "device unauthorized")
        raise AdbCommandError(blob.strip() or f"adb exited {r.returncode}")

    # -- device discovery --------------------------------------------------
    def list_devices(self) -> list[Device]:
        r = self._run(["devices"], timeout=20.0)
        devices: list[Device] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state_str = parts[0], parts[1]
            try:
                state = DeviceState(state_str)
            except ValueError:
                state = DeviceState.UNKNOWN
            devices.append(Device(serial=serial, state=state))
        return devices

    def wait_for_device(self, serial: str, timeout: float = 30.0) -> None:
        self._run_checked(["wait-for-device"], serial=serial, timeout=timeout)

    # -- shell / filesystem ------------------------------------------------
    def shell(self, command: str, *, serial: str | None = None) -> str:
        """Run a raw shell command on the device, returning stdout.

        `command` is split with shlex (single string) so quoting/Unicode is
        preserved; adb receives the command as separate argv tokens it joins.
        """
        r = self._run_checked(["shell", *shlex.split(command)], serial=serial, timeout=60.0)
        return r.stdout.rstrip("\n")

    def shell_list(self, command: str, *, serial: str | None = None) -> str:
        """Tolerant variant: returns empty string on shell errors it can't
        normalize, still raising real device errors."""
        r = self._run(["shell", *shlex.split(command)], serial=serial, timeout=60.0)
        if r.ok:
            return r.stdout.rstrip("\n")
        self._raise_for(r)
        return ""

    # -- file transfer -----------------------------------------------------
    def push(self, local: str, remote: str, *, serial: str | None = None) -> None:
        self._run_checked(["push", local, remote], serial=serial, timeout=3600.0)

    def shell_mkdir(self, path: str, *, serial: str | None = None) -> None:
        self._run_checked(["shell", "mkdir", "-p", path], serial=serial)

    def shell_mv(self, src: str, dst: str, *, serial: str | None = None) -> None:
        self._run_checked(["shell", "mv", src, dst], serial=serial)

    def shell_rm(self, path: str, *, serial: str | None = None) -> None:
        """Remove a single file (used only for our own .part files)."""
        self._run_checked(["shell", "rm", "-f", path], serial=serial)

    def shell_stat_size(self, remote: str, *, serial: str | None = None) -> int | None:
        """Return remote file size in bytes, or None if it does not exist.

        Uses a portable `stat`-based probe that avoids relying on `ls -l`
        column splitting (which breaks on spaces/Unicode).
        """
        script = f'stat -c "%s" "{remote}" 2>/dev/null'
        out = self.shell_list(script, serial=serial).strip()
        if not out:
            return None
        try:
            return int(out.splitlines()[0])
        except ValueError:
            return None
