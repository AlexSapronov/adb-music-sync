"""Persistent user settings with atomic (temp + rename) writes.

Stored under %APPDATA%/ADB Music Sync/ on Windows, with a HOME/.config
fallback elsewhere. Written atomically so a crash mid-write cannot corrupt
the file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import APP_NAME, APP_SLUG


def config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:  # Windows
        base = Path(appdata) / APP_NAME
    else:
        base = Path.home() / ".config" / APP_SLUG
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return config_dir() / "config.json"


_DEFAULTS: dict = {
    "last_local_folder": "",
    "selected_serial": "",
    "storage_by_serial": {},
    "destination_path": "Music",
    "window": {"width": 860, "height": 640, "x": None, "y": None},
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(_DEFAULTS))  # deep copy
    p = config_path()
    if not p.is_file():
        return cfg
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return cfg
    for key, value in _DEFAULTS.items():
        cfg[key] = data.get(key, value)
    # merge dict sub-fields defensively
    for key in ("window", "storage_by_serial"):
        if isinstance(cfg[key], dict) and isinstance(_DEFAULTS[key], dict):
            merged = dict(_DEFAULTS[key])
            merged.update(cfg[key])
            cfg[key] = merged
    return cfg


def save_config(cfg: dict) -> None:
    """Write atomically: temp file -> fsync -> rename over the real file."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmpname, p)
    except Exception:
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise
