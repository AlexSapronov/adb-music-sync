"""Local music library scanner.

Recursively walks a PC folder, collecting supported audio files while
preserving relative directory structure (POSIX separators) for the device
side.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from .models import LocalFileRef, ScanResult

SUPPORTED_EXTENSIONS = frozenset(
    {
        ".mp3",
        ".flac",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".wav",
        ".ape",
    }
)


def is_supported(path: str | Path) -> bool:
    return PurePosixPath(str(path)).suffix.lower() in SUPPORTED_EXTENSIONS


def scan_library(root: str | Path, on_progress=None) -> ScanResult:
    """Walk `root` recursively, returning all supported audio files.

    Relative paths use POSIX separators so they map 1:1 onto Android paths.
    """
    root_path = Path(root)
    files: list[LocalFileRef] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Stable ordering for reproducibility.
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if not is_supported(name):
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            rel = PurePosixPath(full.relative_to(root_path)).as_posix()
            files.append(LocalFileRef(local_path=str(full), rel_path=rel, size=size))
            total += size
            if on_progress is not None:
                on_progress(len(files), total)
        # Do not descend into hidden dirs that are clearly not music.
    return ScanResult(files=files, total_bytes=total)
