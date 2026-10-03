"""Destination path validation and Android path construction.

All path joining uses POSIX semantics regardless of host OS, because device
paths are always POSIX. This module guarantees a destination can never escape
its storage root or point at a system directory.
"""

from __future__ import annotations

import posixpath

from .errors import InvalidDestinationError

# Directories that are never a valid user destination.
_BLOCKED_ROOTS = ("/", "/system", "/system/", "/data", "/data/", "/dev", "/proc", "/sys")


def normalize_posix(path: str) -> str:
    """Normalize a device-side path: collapse '.', resolve '..', strip trailing
    slash (except root). Collapses duplicate slashes including a leading "//"."""
    if not path or path == "/" or path == "//":
        return "/"
    p = posixpath.normpath(path)
    return "/" if p in ("/", "") else p


def is_within(storage_root: str, path: str) -> bool:
    """True if `path` is inside `storage_root` (both normalized)."""
    root = normalize_posix(storage_root).rstrip("/") + "/"
    p = normalize_posix(path)
    rel = posixpath.relpath(p, posixpath.normpath(storage_root))
    if rel == ".." or rel.startswith("../"):
        return False
    if p == root.rstrip("/"):
        return True  # exactly the root is allowed
    return p.startswith(root) or p == posixpath.normpath(storage_root)


def validate_destination(storage_root: str, destination: str) -> str:
    """Validate and normalize a destination directory under a storage root.

    Returns the normalized absolute destination path. Raises
    :class:`InvalidDestinationError` for anything unsafe.
    """
    dest = destination.strip()
    if not dest:
        raise InvalidDestinationError("destination is empty")

    # Build absolute path: relative paths resolve under the storage root.
    if dest.startswith("/"):
        absolute = dest
    else:
        absolute = posixpath.join(storage_root, dest)

    normalized = normalize_posix(absolute)

    if normalized in _BLOCKED_ROOTS or normalized.rstrip("/") in _BLOCKED_ROOTS:
        raise InvalidDestinationError(f"destination {normalized} is a system path")

    if not is_within(storage_root, normalized):
        raise InvalidDestinationError(
            f"destination {normalized} escapes storage root {storage_root}"
        )

    return normalized


def join_rel(destination: str, rel_path: str) -> str:
    """Join a (validated) destination root with a relative file path.

    `rel_path` must stay within destination — '..' is rejected.
    """
    rel = normalize_posix(rel_path)
    if rel.startswith("/"):
        rel = rel.lstrip("/")
    if rel == ".." or rel.startswith("../"):
        raise InvalidDestinationError(f"relative path escapes destination: {rel_path}")
    joined = posixpath.join(destination, rel)
    if not is_within(destination, joined) and not joined.startswith(destination.rstrip("/") + "/"):
        raise InvalidDestinationError(f"joined path escapes destination: {joined}")
    return joined
