"""Fake ADB client for tests — no real device required.

Mirrors the AdbClient interface in-memory. Scripts control device list,
storage mounts, and a fake remote filesystem mapping for push/stat/mkdir/mv.
"""

from __future__ import annotations

from adb_music_sync.adb import AdbClient, CommandResult
from adb_music_sync.errors import (
    AdbCommandError,
    DeviceDisconnectedError,
    DeviceOfflineError,
    TransferError,
)
from adb_music_sync.models import Device, DeviceState


class FakeAdbClient(AdbClient):
    """In-memory ADB implementation.

    Configure with:
    - ``devices``: list[(serial, state)]
    - ``sm_volumes``: string mimicking `sm list-volumes`
    - ``storage_listing``: string mimicking `ls /storage`
    - ``df``: callable or dict serial->df text for free/space reporting
    - ``remote``: dict remote_path -> size (bytes) for stat
    - ``fail_next_push`` / ``disconnect_on_push``: fault injection
    """

    def __init__(self, devices=None, sm_volumes="", storage_listing="", df="", remote=None):
        # bypass real adb lookup entirely
        self.adb_path = "/fake/adb"
        self.devices = list(devices) if devices else []
        self.sm_volumes = sm_volumes
        self.storage_listing = storage_listing
        self._df = df
        self._df_map: dict = {}
        self._free_override = None
        self.remote = dict(remote) if remote else {}
        self.pushed: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []
        self.mkdirs: list[str] = []
        self.removed: list[str] = []
        self.fail_next_push = False
        self.disconnect = False  # when True, every op raises DeviceDisconnectedError
        self.offline = False  # when True, every op raises DeviceOfflineError
        self.calls: list[list[str]] = []
        self.read_only = False  # when True, touch/write ops fail (permission denied)
        self.props: dict[str, str] = {}  # getprop key -> value (e.g. ro.serialno)

    # -- helpers for tests ------------------------------------------------
    @classmethod
    def with_device(cls, serial="DEVICE1", state=DeviceState.DEVICE):
        return cls(devices=[Device(serial=serial, state=state)])

    @classmethod
    def with_storage(
        cls,
        internal_size=None,
        sd_uuid=None,
        sd_size=None,
        serial="DEVICE1",
        free=None,
    ):
        """Build a client with internal storage and optionally an SD card.

        `internal_size` / `sd_size` are total bytes; free is defaulted to
        total if not given.
        """
        sm = "emulated;0 mounted null null\n"
        if internal_size is not None:
            sm += "public:179,0 unmounted null\n"
        if sd_uuid:
            sm += f"public:179,1 mounted {sd_uuid}\n"
        listing = "emulated\nself\n"
        if sd_uuid:
            listing += f"{sd_uuid}\n"
        client = cls(
            devices=[Device(serial=serial, state=DeviceState.DEVICE)],
            sm_volumes=sm,
            storage_listing=listing,
        )
        client._set_free(
            serial, internal_size if internal_size is not None else 0, sd_uuid, sd_size
        )
        if free is not None:
            client._free_override = free
        return client

    def _set_free(self, serial, internal, sd_uuid, sd_size):
        # df text: Filesystem 1K-blocks Used Available Use% Mounted
        def make(total_kb):
            free_kb = total_kb  # free == total for simplicity unless overridden
            return (
                "Filesystem 1K-blocks Used Available Use% Mounted on\n"
                f"/dev/block  {total_kb} 0 {free_kb} 0% /storage/emulated/0\n"
            )

        self._df_map = {}
        self._df_map[("/storage/emulated/0", serial)] = make(internal // 1024 if internal else 0)
        if sd_uuid:
            self._df_map[(f"/storage/{sd_uuid}", serial)] = make(sd_size // 1024 if sd_size else 0)

    # -- AdbClient interface overrides ------------------------------------
    def _run(self, args, *, serial=None, target=None, timeout=60.0) -> CommandResult:
        self.calls.append(list(args))
        if self.disconnect:
            raise DeviceDisconnectedError("device disconnected")
        if self.offline:
            raise DeviceOfflineError("device offline")

        cmd = args[0] if args else ""
        if cmd == "devices":
            lines = ["List of devices attached"]
            for d in self.devices:
                extra = ""
                if d.transport_id is not None:
                    extra += f" transport_id:{d.transport_id}"
                if d.product is not None:
                    extra += f" product:{d.product}"
                if d.model is not None:
                    extra += f" model:{d.model}"
                if d.device_name is not None:
                    extra += f" device:{d.device_name}"
                lines.append(f"{d.serial}\t{d.state.value}{extra}")
            return CommandResult(0, "\n".join(lines), "")

        if cmd == "shell":
            return self._fake_shell(args[1:], serial, timeout)

        if cmd == "push":
            if self.fail_next_push:
                self.fail_next_push = False
                return CommandResult(1, "", "remote closed the connection")
            local, remote = args[1], args[2]
            self.pushed.append((local, remote))
            return CommandResult(0, "", "")

        if cmd == "wait-for-device":
            return CommandResult(0, "", "")

        return CommandResult(0, "", "")

    def _fake_shell(self, args, serial, timeout) -> CommandResult:
        if not args:
            return CommandResult(0, "", "")
        sub = args[0]
        if sub == "sm":
            return CommandResult(0, self.sm_volumes, "")
        if sub == "ls":
            target = args[1] if len(args) > 1 else ""
            if target == "/storage":
                return CommandResult(0, self.storage_listing, "")
            return CommandResult(0, "", "")
        if sub == "df":
            mp = args[-1].strip("'") if args else ""
            text = self._df_map.get((mp, serial), None)
            if text is None and serial is not None:
                text = self._df_map.get((mp, None), None)
            if text is None:
                # fall back to any entry with the same mount path (target dispatch)
                for (m, _s), t in self._df_map.items():
                    if m == mp:
                        text = t
                        break
            if self._free_override is not None:
                return CommandResult(0, self._free_override, "")
            if text is None:
                # derive from remote/dflt
                return CommandResult(
                    0, "Filesystem 1K-blocks Used Available Use% M\n/dev/x 0 0 0 0% /\n", ""
                )
            return CommandResult(0, text, "")
        if sub == "getprop":
            key = args[1] if len(args) > 1 else ""
            return CommandResult(0, self.props.get(key, ""), "")
        if sub == "stat":
            # stat -c "%s" "path"
            raw = " ".join(args[1:])
            path = self._extract_quoted(raw)
            size = self.remote.get(path)
            if size is None:
                return CommandResult(1, "", "No such file")
            return CommandResult(0, str(size), "")
        if sub == "mkdir":
            path = args[-1].strip("'") if args else ""
            self.mkdirs.append(path)
            return CommandResult(0, "", "")
        if sub == "mv":
            src = args[1].strip("'")
            dst = args[2].strip("'")
            self.moves.append((src, dst))
            if src in self.remote:
                self.remote[dst] = self.remote.pop(src)
            return CommandResult(0, "", "")
        if sub == "rm":
            path = args[-1].strip("'")
            self.removed.append(path)
            self.remote.pop(path, None)
            return CommandResult(0, "", "")
        if sub == "touch":
            if self.read_only:
                return CommandResult(1, "", "Permission denied")
            path = args[-1].strip("'")
            self.remote[path] = 0  # empty probe file
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    @staticmethod
    def _extract_quoted(raw: str) -> str:
        import re

        m = re.search(r'"([^"]*)"', raw)
        if m:
            return m.group(1)
        m = re.search(r"'([^']*)'", raw)
        if m:
            return m.group(1)
        parts = raw.split()
        # stat -c %s <path> -> last non-flag token
        toks = [t for t in parts if not t.startswith("-") and t != "%s"]
        return toks[-1] if toks else raw

    # -- override high-level to respect disconnect/offline/read_only ----------
    def _check_faults(self):
        if self.disconnect:
            raise DeviceDisconnectedError("device disconnected")
        if self.offline:
            raise DeviceOfflineError("device offline")

    def push(self, local, remote, *, serial=None, target=None):
        self._check_faults()
        if self.fail_next_push:
            self.fail_next_push = False
            raise TransferError(f"push failed for {remote}")
        self.pushed.append((local, remote))
        self.remote[remote] = 0

    def shell_stat_size(self, remote, *, serial=None, target=None):
        self._check_faults()
        return self.remote.get(remote)

    def shell_mkdir(self, path, *, serial=None, target=None):
        self._check_faults()
        if self.read_only:
            raise AdbCommandError(f"mkdir failed for {path}: Permission denied")
        self.mkdirs.append(path)
        self.remote.setdefault(path, None)

    def shell_mv(self, src, dst, *, serial=None, target=None):
        self._check_faults()
        self.moves.append((src, dst))
        if src in self.remote:
            self.remote[dst] = self.remote.pop(src)

    def shell_rm(self, path, *, serial=None, target=None):
        self._check_faults()
        self.removed.append(path)
        self.remote.pop(path, None)

    def shell_touch(self, path, *, serial=None, target=None):
        self._check_faults()
        if self.read_only:
            raise AdbCommandError(f"touch failed for {path}: Permission denied")
        self.remote[path] = 0

    def getprop(self, key, *, serial=None, target=None):
        self._check_faults()
        return self.props.get(key, "")

    def list_devices(self):
        self._check_faults()
        return list(self.devices)
