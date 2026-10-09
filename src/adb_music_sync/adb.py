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

import base64
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
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
                text=args[0] != "exec-out",
                encoding="utf-8" if args[0] != "exec-out" else None,
                errors="replace" if args[0] != "exec-out" else None,
                timeout=timeout,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except FileNotFoundError as exc:
            raise AdbNotFoundError(f"adb not found: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbCommandError(f"adb timed out: {' '.join(args)}") from exc
        # decode may contain nulls from shell byte dumps; keep as-is
        stdout = proc.stdout.decode("utf-8") if isinstance(proc.stdout, bytes) else proc.stdout
        stderr = (
            proc.stderr.decode("utf-8", errors="replace")
            if isinstance(proc.stderr, bytes)
            else proc.stderr
        )
        return CommandResult(proc.returncode, stdout, stderr)

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
        self._raise_for(r, args, serial=serial, target=target)
        sel = self._selector_desc(serial=serial, target=target)
        raise AdbCommandError(
            f"adb {' '.join(args)} failed{sel}: "
            f"{(r.stderr or '').strip() or f'exit code {r.returncode}'}"
        )

    def _raise_for(
        self,
        r: CommandResult,
        args: list[str] | None = None,
        *,
        serial: str | None = None,
        target: AdbTarget | None = None,
    ) -> None:
        if args is None:
            args = []
        blob = (r.stderr + " " + r.stdout).lower()
        sel = self._selector_desc(serial=serial, target=target)
        ctx = f"adb {' '.join(args)} failed{sel}"
        # Specific states first, so "device offline" -> OfflineError and
        # "unauthorized" -> UnauthorizedError, not the broad Disconnected.
        if any(m in blob for m in _UNAUTHORIZED_MARKERS):
            raise DeviceUnauthorizedError(f"{ctx}: {blob.strip() or 'device unauthorized'}")
        if any(m in blob for m in _OFFLINE_MARKERS):
            raise DeviceOfflineError(f"{ctx}: {blob.strip() or 'device offline'}")
        if any(m in blob for m in _DISCONNECT_MARKERS):
            raise DeviceDisconnectedError(f"{ctx}: {blob.strip() or 'device disconnected'}")
        stderr = (r.stderr or "").strip()
        detail = stderr or blob.strip() or f"exit code {r.returncode}"
        raise AdbCommandError(f"{ctx}: {detail}")

    @staticmethod
    def _selector_desc(*, serial: str | None, target: AdbTarget | None) -> str:
        if target is not None:
            return f" [target: transport_id={target.transport_id or '-'} serial={target.serial or '-'}]"
        if serial is not None:
            return f" [serial: {serial}]"
        return ""

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

    def getprop(
        self, key: str, *, serial: str | None = None, target: AdbTarget | None = None
    ) -> str:
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

    def wait_for_device(
        self, serial: str | None = None, target: AdbTarget | None = None, timeout: float = 30.0
    ) -> None:
        self._run_checked(["wait-for-device"], serial=serial, target=target, timeout=timeout)

    # -- shell / filesystem ------------------------------------------------
    def shell(
        self, command: str, *, serial: str | None = None, target: AdbTarget | None = None
    ) -> str:
        """Run a raw shell command on the device, returning stdout.

        `command` is passed to ``adb shell`` as a SINGLE argument so the
        device-side shell receives it verbatim — POSIX quoting (from
        :func:`_posix_quote`), shell operators (``&&``, ``2>/dev/null``) and
        redirections survive. We must NOT shlex.split() here: Python's
        host-side splitter strips the single quotes that are meant for the
        Android shell.
        """
        r = self._run_checked(["shell", command], serial=serial, target=target, timeout=60.0)
        return r.stdout.rstrip("\n")

    def list_files(self, root: str, *, target: AdbTarget | None = None) -> list[str]:
        """Strict recursive read; NUL delimiters preserve every path."""
        result = self._run_checked(
            ["exec-out", f"find {_posix_quote(root)} -type f -print0"],
            target=target,
            timeout=300.0,
        )
        return sorted({path for path in result.stdout.split("\0") if path})

    def list_audio_files(self, root: str, *, target: AdbTarget | None = None) -> list[str]:
        from .scanner import is_supported

        return [path for path in self.list_files(root, target=target) if is_supported(path)]

    def path_exists(self, path: str, *, target: AdbTarget) -> bool:
        # Include dangling symlinks: they must not be overwritten either.
        out = self.shell(
            f"if [ -e {_posix_quote(path)} ] || [ -L {_posix_quote(path)} ]; "
            "then printf yes; else printf no; fi",
            target=target,
        )
        if out not in ("yes", "no"):
            raise AdbCommandError("Invalid existence response")
        return out == "yes"

    def canonical_path(self, path: str, *, target: AdbTarget) -> str:
        result = self._run_checked(
            ["exec-out", f"readlink -f {_posix_quote(path)}"],
            target=target,
        )
        return result.stdout.removesuffix("\n")

    def sha256(self, path: str, *, target: AdbTarget) -> str:
        # Hash stdin so shell tools cannot escape or decorate the filename.
        result = self._run_checked(
            ["shell", f"sha256sum < {_posix_quote(path)}"],
            target=target,
            timeout=3600.0,
        )
        digest = result.stdout.split()[0] if result.stdout.split() else ""
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise AdbCommandError(f"Invalid SHA-256 response: {path}")
        return digest.lower()

    def audio_durations(self, *, target: AdbTarget) -> dict[str, int]:
        """Optional MediaStore index; unavailable permissions return no durations."""
        result = self._run(
            [
                "shell",
                "content query --uri content://media/external/audio/media "
                "--projection duration:_data",
            ],
            target=target,
            timeout=60.0,
        )
        if not result.ok:
            blob = (result.stderr + result.stdout).lower()
            if any(
                marker in blob
                for marker in (
                    *_OFFLINE_MARKERS,
                    *_UNAUTHORIZED_MARKERS,
                    "no devices/emulators found",
                    "device disconnected",
                    "device not found",
                    "connection reset",
                    "broken pipe",
                    "remote closed",
                )
            ):
                self._raise_for(result, target=target)
            return {}
        durations = {}
        for line in result.stdout.splitlines():
            match = re.fullmatch(r"Row: \d+ duration=(\d+), _data=(/.+)", line)
            if match and int(match[1]) > 0:
                # A conflicting/stale duplicate row is not usable evidence.
                path, duration = match[2], int(match[1])
                if path in durations and durations[path] != duration:
                    durations[path] = 0
                else:
                    durations.setdefault(path, duration)
        return {path: value for path, value in durations.items() if value > 0}

    def read_bytes(self, path: str, *, target: AdbTarget, limit: int = 16 * 1024 * 1024) -> bytes:
        size = self.shell_stat_size(path, target=target)
        if size is None or size > limit:
            raise AdbCommandError(f"Cannot read missing/oversized text file: {path}")
        result = self._run_checked(["exec-out", f"base64 < {_posix_quote(path)}"], target=target)
        try:
            data = base64.b64decode("".join(result.stdout.split()), validate=True)
        except ValueError as exc:
            raise AdbCommandError(f"Invalid text-file response: {path}") from exc
        if len(data) != size:
            raise AdbCommandError(f"Text file changed while reading: {path}")
        return data

    def write_bytes(self, path: str, data: bytes, *, target: AdbTarget) -> None:
        """Push a small control file, verify bytes, then rename it atomically."""
        remote_temp = path + "." + uuid.uuid4().hex + ".part"
        local = None
        try:
            with tempfile.NamedTemporaryFile(delete=False) as stream:
                stream.write(data)
                local = Path(stream.name)
            self.push(str(local), remote_temp, target=target)
            if self.sha256(remote_temp, target=target) != hashlib.sha256(data).hexdigest():
                raise AdbCommandError(f"Written file verification failed: {path}")
            self.shell_mv(remote_temp, path, target=target)
        finally:
            if local is not None:
                local.unlink(missing_ok=True)
        # A failed push may leave a .part, never overwrite the original file.

    def move_no_replace(self, source: str, destination: str, *, target: AdbTarget) -> None:
        # mv -n is available in Android toybox; verify it actually moved the
        # source, since mv -n can return success when a destination exists.
        if self.path_exists(destination, target=target):
            raise AdbCommandError(f"Refusing to overwrite: {destination}")
        self._run_checked(
            ["shell", f"mv -n {_posix_quote(source)} {_posix_quote(destination)}"],
            target=target,
        )
        if self.path_exists(source, target=target) or not self.path_exists(
            destination, target=target
        ):
            raise AdbCommandError(f"Move did not complete: {source}")

    def shell_list(
        self, command: str, *, serial: str | None = None, target: AdbTarget | None = None
    ) -> str:
        """Tolerant variant: returns empty string on ORDINARY remote-shell
        non-zero status, still raising real device errors (offline /
        disconnected / unauthorized / transport failure).

        A non-zero exit from `command` itself (e.g. `stat` on a missing file)
        is NOT a device error; only ADB-level / transport-level failures raise
        here. The command is passed to ``adb shell`` as a single argument so
        device-side quoting survives (no host-side shlex split)."""
        r = self._run(["shell", command], serial=serial, target=target, timeout=60.0)
        if r.ok:
            return r.stdout.rstrip("\n")
        self._raise_for(r, ["shell", command], serial=serial, target=target)
        return ""

    # -- file transfer -----------------------------------------------------
    def push(
        self, local: str, remote: str, *, serial: str | None = None, target: AdbTarget | None = None
    ) -> None:
        self._run_checked(["push", local, remote], serial=serial, target=target, timeout=3600.0)

    def shell_mkdir(
        self, path: str, *, serial: str | None = None, target: AdbTarget | None = None
    ) -> None:
        self._run_checked(
            ["shell", "mkdir", "-p", _posix_quote(path)], serial=serial, target=target
        )

    def shell_mv(
        self, src: str, dst: str, *, serial: str | None = None, target: AdbTarget | None = None
    ) -> None:
        self._run_checked(
            ["shell", "mv", _posix_quote(src), _posix_quote(dst)], serial=serial, target=target
        )

    def shell_rm(
        self, path: str, *, serial: str | None = None, target: AdbTarget | None = None
    ) -> None:
        """Remove a single file (used only for our own .part files)."""
        self._run_checked(["shell", "rm", "-f", _posix_quote(path)], serial=serial, target=target)

    def shell_stat_size(
        self, remote: str, *, serial: str | None = None, target: AdbTarget | None = None
    ) -> int | None:
        """Return remote file size in bytes, or None if it does not exist.

        A missing remote file is a *normal* probe outcome (used to decide
        whether a track must be transferred), NOT an ADB error. So we run the
        ``stat`` via the raw command layer and inspect the result directly:

        * success + integer stdout  -> size;
        * ordinary ``stat`` failure (file absent, exit != 0) -> None;
        * genuine device/transport failure -> the matching typed exception.

        The path is POSIX-quoted via :func:`_posix_quote` so `'`, `"`, `&`,
        `#`, `$`, backticks, parens and Unicode survive the device shell.
        """
        script = f"stat -c %s {_posix_quote(remote)} 2>/dev/null"
        r = self._run(["shell", script], serial=serial, target=target, timeout=60.0)
        if r.ok:
            out = r.stdout.strip()
            if out:
                try:
                    return int(out.splitlines()[0])
                except ValueError:
                    return None
            return None
        # Non-zero: distinguish "file simply doesn't exist" from a real
        # device/transport failure. 2>/dev/null suppresses the stat error for
        # a missing file, so empty output here => missing file => None.
        blob = (r.stderr + " " + r.stdout).strip().lower()
        if not blob:
            return None
        # Anything adb actually printed is a genuine failure — classify it.
        self._raise_for(r, ["shell", script], serial=serial, target=target)
        return None

    def shell_stat_sizes(
        self, remotes: list[str], *, serial: str | None = None, target: AdbTarget | None = None
    ) -> dict[str, int | None]:
        """Probe up to 100 paths per invocation, with a bounded Windows command line.

        Numeric row IDs keep filenames (even embedded newlines) out of the
        response protocol. A failed remote stat produces '-', while ADB
        failures and incomplete/malformed responses still stop plan building.
        """
        sizes: dict[str, int | None] = {}
        batch: list[str] = []
        fragments: list[str] = []

        def flush() -> None:
            if not batch:
                return
            script = "".join(fragments)
            r = self._run_checked(["shell", script], serial=serial, target=target)
            rows = r.stdout.splitlines()
            if len(rows) != len(batch):
                raise AdbCommandError("incomplete batched stat response")
            for index, (path, row) in enumerate(zip(batch, rows, strict=True)):
                key, sep, value = row.partition(":")
                if key != str(index) or not sep or (value != "-" and not value.isdecimal()):
                    raise AdbCommandError("invalid batched stat response")
                sizes[path] = None if value == "-" else int(value)
            batch.clear()
            fragments.clear()

        for remote in remotes:
            if "\x00" in remote:
                raise AdbCommandError("remote path contains a NUL byte")
            if len(batch) == 100:
                flush()
            fragment = (
                f"s=$(stat -c %s {_posix_quote(remote)} 2>/dev/null) || s=-; "
                f"printf '{len(batch)}:%s\\n' \"$s\";"
            )
            # Include Python's Windows argv escaping and UTF-16 code units.
            selector = target.args() if target is not None else (["-s", serial] if serial else [])
            cmdline = subprocess.list2cmdline(
                [self.adb_path, *selector, "shell", "".join(fragments) + fragment]
            )
            if len(cmdline.encode("utf-16-le")) // 2 > 24000:
                flush()
                fragment = (
                    f"s=$(stat -c %s {_posix_quote(remote)} 2>/dev/null) || s=-; "
                    "printf '0:%s\\n' \"$s\";"
                )
                cmdline = subprocess.list2cmdline([self.adb_path, *selector, "shell", fragment])
                if len(cmdline.encode("utf-16-le")) // 2 > 24000:
                    raise AdbCommandError("remote path exceeds batched stat command limit")
            batch.append(remote)
            fragments.append(fragment)
        flush()
        return sizes

    def shell_touch(
        self, path: str, *, serial: str | None = None, target: AdbTarget | None = None
    ) -> None:
        """Create an empty file at `path` (used only for write probes)."""
        self._run_checked(
            ["shell", "touch", _posix_quote(path)], serial=serial, target=target, timeout=30.0
        )
