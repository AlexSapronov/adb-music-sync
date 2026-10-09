"""Portable, UTF-8 device catalog for music curation and Poweramp playlists."""

from __future__ import annotations

import json
import os
import posixpath
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import __version__
from .adb import AdbClient
from .models import AdbTarget
from .paths import is_within, validate_destination


def export_catalog(
    client: AdbClient,
    storage_root: str,
    music_folder: str,
    output: str | Path,
    *,
    target: AdbTarget,
) -> dict:
    """Scan only the selected device folder; atomically save a complete catalog.

    Folder/file names are clues for curation, not invented audio tags. No
    audio downloads, writes on Android, or MediaStore permissions are needed.
    """
    music_root = validate_destination(storage_root, music_folder or "Music")
    playlist_root = posixpath.join(music_root, "ChatGPT_Playlists")
    tracks = []
    for path in client.list_audio_files(music_root, target=target):
        if not is_within(music_root, path) or path == music_root:
            raise ValueError(f"Unexpected path outside music folder: {path}")
        rel = posixpath.relpath(path, music_root)
        tracks.append(
            {
                "path": path,
                "relative_path": rel,
                "playlist_path": posixpath.relpath(path, playlist_root),
                "filename": PurePosixPath(path).name,
                "folders": list(PurePosixPath(rel).parts[:-1]),
                "extension": PurePosixPath(path).suffix.lower(),
                "m3u_compatible": not any(c in path for c in "\r\n"),
            }
        )
    catalog = {
        "schema": "adb-music-sync.catalog",
        "schema_version": 1,
        "app_version": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "music_root": music_root,
        "playlist_directory": playlist_root,
        "playlist_format": "m3u8",
        "path_base": "playlist_directory",
        "metadata_source": "file_and_folder_names_only",
        "instructions": (
            "Create curated UTF-8 M3U8 playlists using only tracks in this catalog. "
            "Use playlist_path unchanged for playlists saved in playlist_directory. "
            "Do not invent paths or treat filenames as verified audio tags. "
            "Exclude tracks with m3u_compatible=false from M3U8 playlists."
        ),
        "track_count": len(tracks),
        "tracks": tracks,
    }
    output = Path(output)
    # Replace only after the entire scan and serialization succeed. An old
    # export must survive device disconnects and failed scans/writes.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output.parent,
            prefix=output.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(catalog, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return catalog
