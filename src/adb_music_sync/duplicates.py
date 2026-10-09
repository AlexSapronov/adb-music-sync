"""Exact duplicate detection and recoverable, journaled device-side cleanup."""

from __future__ import annotations

import base64
import json
import posixpath
import re
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .adb import AdbClient
from .models import AdbTarget
from .paths import is_within, validate_destination
from .scanner import is_supported

QUARANTINE = ".adb-music-sync-quarantine"


class OperationCancelled(Exception):
    pass


@dataclass(frozen=True)
class DuplicateGroup:
    digest: str
    size: int
    paths: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateScan:
    music_root: str
    groups: tuple[DuplicateGroup, ...]
    file_count: int
    hashed_count: int

    @property
    def redundant_bytes(self) -> int:
        return sum(g.size * (len(g.paths) - 1) for g in self.groups)


def rewrite_playlist(data: bytes, playlist_path: str, replacements: dict[str, str]) -> bytes:
    """Replace exact path references, retaining comments, order, BOM and EOLs."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = data.decode(encoding)
    except UnicodeError as exc:
        raise ValueError(
            f"Плейлист не в UTF-8/UTF-16: {playlist_path}. Конвертируйте его перед очисткой."
        ) from exc
    lines = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        if body and not body.startswith("#"):
            normalized = body.replace("\\", "/")
            absolute = posixpath.normpath(
                posixpath.join(posixpath.dirname(playlist_path), normalized)
            )
            if absolute in replacements:
                kept = replacements[absolute]
                body = (
                    kept
                    if normalized.startswith("/")
                    else posixpath.relpath(kept, posixpath.dirname(playlist_path))
                )
        lines.append(body + ending)
    result = "".join(lines)
    if result == text:
        return data
    if data.startswith(b"\xfe\xff"):
        return b"\xfe\xff" + result.encode("utf-16-be")
    return result.encode(encoding)


class DuplicateManager:
    def __init__(self, client: AdbClient, storage_root: str, target: AdbTarget):
        self.client = client
        self.storage_root = posixpath.normpath(storage_root)
        self.target = target
        self.quarantine_root = posixpath.join(self.storage_root, QUARANTINE)

    def _check_cancel(self, cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise OperationCancelled(
                "Операция остановлена. Обновите список карантина для восстановления или продолжения удаления."
            )

    def _safe_existing(self, path: str, root: str) -> None:
        if (
            "\x00" in path
            or posixpath.normpath(path) != path
            or not is_within(root, path)
            or path == root
        ):
            raise ValueError(f"Недопустимый путь: {path}")
        canonical_root = self.client.canonical_path(root, target=self.target)
        canonical_path = self.client.canonical_path(path, target=self.target)
        if (
            not canonical_root
            or not canonical_path
            or not is_within(canonical_root, canonical_path)
            or canonical_root == canonical_path
        ):
            raise ValueError(f"Ссылка выходит за пределы папки: {path}")

    def _hash(self, path: str, root: str) -> str:
        self._safe_existing(path, root)
        return self.client.sha256(path, target=self.target)

    def scan(self, folder: str, *, progress=None, cancel=None) -> DuplicateScan:
        root = validate_destination(self.storage_root, folder or "Music")
        if root != self.storage_root:
            self._safe_existing(root, self.storage_root)
        paths = [
            p
            for p in self.client.list_audio_files(root, target=self.target)
            if not is_within(self.quarantine_root, p)
        ]
        if progress:
            progress(f"Проверка размеров: {len(paths)} файлов")
        self._check_cancel(cancel)
        sizes = self.client.shell_stat_sizes(paths, target=self.target)
        by_size = defaultdict(list)
        for path in paths:
            if sizes[path] is None:
                raise ValueError(f"Файл исчез во время поиска: {path}. Повторите сканирование.")
            by_size[sizes[path]].append(path)
        candidates = [p for group in by_size.values() if len(group) > 1 for p in group]
        hashes = defaultdict(list)
        for index, path in enumerate(candidates, 1):
            self._check_cancel(cancel)
            if progress:
                progress(f"SHA-256: {index}/{len(candidates)} — {posixpath.basename(path)}")
            self._check_cancel(cancel)
            hashes[(sizes[path], self._hash(path, root))].append(path)
        groups = tuple(
            DuplicateGroup(digest, size, tuple(sorted(group)))
            for (size, digest), group in sorted(hashes.items())
            if len(group) > 1
        )
        return DuplicateScan(root, groups, len(paths), len(candidates))

    def _save(self, session: str, journal: dict) -> None:
        payload = json.dumps(journal, ensure_ascii=False, indent=2).encode("utf-8")
        if len(payload) > 16 * 1024 * 1024 - 1024:
            raise ValueError(
                "Слишком большой журнал карантина. Очистите меньше групп за одну операцию."
            )
        self.client.write_bytes(
            posixpath.join(session, "manifest.json"), payload, target=self.target
        )

    def _make_parent(self, path: str, root: str) -> None:
        # Check every existing ancestor before mkdir, then resolve again to
        # reject symlinks outside the expected root (also on restoration).
        parent = posixpath.dirname(path)
        ancestor = parent
        while not self.client.path_exists(ancestor, target=self.target):
            ancestor = posixpath.dirname(ancestor)
            if not is_within(root, ancestor):
                raise ValueError(f"Родитель вне хранилища: {path}")
        if ancestor != root:
            self._safe_existing(ancestor, root)
        self.client.shell_mkdir(parent, target=self.target)
        if parent != root:
            self._safe_existing(parent, root)

    def quarantine(
        self, scan: DuplicateScan, keep: dict[str, str], *, progress=None, cancel=None
    ) -> str:
        root = validate_destination(self.storage_root, scan.music_root)
        if is_within(root, self.quarantine_root) or is_within(self.quarantine_root, root):
            raise ValueError(
                "Для очистки выберите папку музыки, а не корень хранилища или карантин."
            )
        self._safe_existing(root, self.storage_root)
        replacements = {}
        entries = []
        for group in scan.groups:
            if group.digest not in keep:
                continue  # group unchecked by user
            kept = keep[group.digest]
            if kept not in group.paths:
                raise ValueError("Сохраняемая копия отсутствует в группе.")
            for path in group.paths:
                self._check_cancel(cancel)
                if progress:
                    progress(f"Повторная проверка: {posixpath.basename(path)}")
                if self._hash(path, root) != group.digest:
                    raise ValueError(f"Файл изменился после поиска: {path}. Повторите поиск.")
                if path != kept:
                    if any(c in path + kept for c in "\r\n"):
                        raise ValueError(
                            "Очистка имён с переводами строк не поддерживается: нельзя обновить M3U."
                        )
                    replacements[path] = kept
                    entries.append(
                        {"original": path, "kept": kept, "sha256": group.digest, "size": group.size}
                    )
        if not entries:
            raise ValueError("Не выбраны группы дубликатов.")
        playlists = []
        for path in self.client.list_files(root, target=self.target):
            self._check_cancel(cancel)
            if PurePosixPath(path).suffix.lower() not in (".m3u", ".m3u8"):
                continue
            self._safe_existing(path, root)
            before = self.client.read_bytes(path, target=self.target)
            after = rewrite_playlist(before, path, replacements)
            if before != after:
                playlists.append(
                    {
                        "path": path,
                        "before": base64.b64encode(before).decode(),
                        "after": base64.b64encode(after).decode(),
                    }
                )
        session = posixpath.join(self.quarantine_root, uuid.uuid4().hex)
        self._make_parent(posixpath.join(session, "manifest.json"), self.storage_root)
        nomedia = posixpath.join(self.quarantine_root, ".nomedia")
        if not self.client.path_exists(nomedia, target=self.target):
            self.client.write_bytes(nomedia, b"", target=self.target)
        for entry in entries:
            entry["quarantined"] = posixpath.join(
                session, "files", posixpath.relpath(entry["original"], root)
            )
        journal = {
            "schema_version": 1,
            "state": "prepared",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "music_root": root,
            "entries": entries,
            "playlists": playlists,
        }
        self._save(session, journal)  # durable BEFORE the first music move
        for index, entry in enumerate(entries, 1):
            self._check_cancel(cancel)
            if progress:
                progress(
                    f"В карантин: {index}/{len(entries)} — {posixpath.basename(entry['original'])}"
                )
            if (
                self._hash(entry["original"], root) != entry["sha256"]
                or self._hash(entry["kept"], root) != entry["sha256"]
            ):
                raise ValueError(
                    "Файл изменился во время очистки. Восстановите незавершённый карантин."
                )
            self._make_parent(entry["quarantined"], session)
            self.client.move_no_replace(entry["original"], entry["quarantined"], target=self.target)
        for playlist in playlists:
            self._check_cancel(cancel)
            self._safe_existing(playlist["path"], root)
            before = base64.b64decode(playlist["before"])
            if self.client.read_bytes(playlist["path"], target=self.target) != before:
                raise ValueError(
                    "Плейлист изменился во время очистки. Восстановите незавершённый карантин."
                )
            self.client.write_bytes(
                playlist["path"], base64.b64decode(playlist["after"]), target=self.target
            )
        journal["state"] = "quarantined"
        self._save(session, journal)
        return session

    def _load(self, session: str) -> dict:
        parent, name = posixpath.split(session)
        if parent != self.quarantine_root or not re.fullmatch(r"[0-9a-f]{32}", name):
            raise ValueError("Недопустимая папка карантина.")
        self._safe_existing(session, self.storage_root)
        manifest = posixpath.join(session, "manifest.json")
        self._safe_existing(manifest, session)
        journal = json.loads(self.client.read_bytes(manifest, target=self.target).decode("utf-8"))
        if journal.get("schema_version") != 1:
            raise ValueError("Неизвестный формат журнала карантина.")
        root = validate_destination(self.storage_root, journal["music_root"])
        if is_within(root, self.quarantine_root) or is_within(self.quarantine_root, root):
            raise ValueError("Недопустимая папка музыки в журнале.")
        self._safe_existing(root, self.storage_root)
        originals = set()
        for entry in journal["entries"]:
            original = entry["original"]
            if original in originals or not is_supported(original):
                raise ValueError("Повторный/неаудио путь в журнале.")
            originals.add(original)
            for path in (original, entry["kept"]):
                if (
                    posixpath.normpath(path) != path
                    or path == root
                    or not is_within(root, path)
                    or "\x00" in path
                ):
                    raise ValueError("Путь в журнале выходит за папку музыки.")
            expected = posixpath.join(session, "files", posixpath.relpath(original, root))
            if entry["quarantined"] != expected or not re.fullmatch(
                r"[0-9a-f]{64}", entry["sha256"]
            ):
                raise ValueError("Повреждённая запись карантина.")
        for playlist in journal["playlists"]:
            path = playlist["path"]
            if (
                not is_within(root, path)
                or path == root
                or posixpath.normpath(path) != path
                or PurePosixPath(path).suffix.lower() not in (".m3u", ".m3u8")
            ):
                raise ValueError("Недопустимый путь плейлиста в журнале.")
            base64.b64decode(playlist["before"], validate=True)
            base64.b64decode(playlist["after"], validate=True)
        return journal

    def sessions(self) -> list[tuple[str, str, str, int]]:
        if not self.client.path_exists(self.quarantine_root, target=self.target):
            return []
        self._safe_existing(self.quarantine_root, self.storage_root)
        sessions = []
        for path in self.client.list_files(self.quarantine_root, target=self.target):
            if posixpath.basename(path) != "manifest.json":
                continue
            session = posixpath.dirname(path)
            journal = self._load(session)
            if journal["state"] not in ("restored", "purged"):
                sessions.append(
                    (session, journal["state"], journal["created_at"], len(journal["entries"]))
                )
        return sorted(sessions, key=lambda x: x[2], reverse=True)

    def restore(self, session: str, *, progress=None, cancel=None) -> None:
        journal = self._load(session)
        if journal["state"] not in ("prepared", "quarantined", "restoring"):
            raise ValueError("Этот карантин уже удалён или восстановлен.")
        root = journal["music_root"]
        # All conflicts are checked before writing anything. Repeating restore
        # is safe after a disconnection: completed moves are already at source.
        for entry in journal["entries"]:
            self._check_cancel(cancel)
            original, quarantined = entry["original"], entry["quarantined"]
            source_exists = self.client.path_exists(original, target=self.target)
            quarantine_exists = self.client.path_exists(quarantined, target=self.target)
            if source_exists == quarantine_exists:
                raise ValueError(
                    f"Конфликт или потерянный файл: {original}. Ничего не перезаписано."
                )
            path, scope = (original, root) if source_exists else (quarantined, session)
            if self._hash(path, scope) != entry["sha256"]:
                raise ValueError(f"Содержимое изменилось: {path}")
        for playlist in journal["playlists"]:
            self._check_cancel(cancel)
            self._safe_existing(playlist["path"], root)
            current = self.client.read_bytes(playlist["path"], target=self.target)
            if current not in (
                base64.b64decode(playlist["before"]),
                base64.b64decode(playlist["after"]),
            ):
                raise ValueError(
                    f"Плейлист отредактирован после очистки: {playlist['path']}. Восстановление остановлено."
                )
        journal["state"] = "restoring"
        self._save(session, journal)
        for index, entry in enumerate(journal["entries"], 1):
            self._check_cancel(cancel)
            if progress:
                progress(f"Восстановление: {index}/{len(journal['entries'])}")
            if self.client.path_exists(entry["quarantined"], target=self.target):
                self._make_parent(entry["original"], root)
                if self._hash(entry["quarantined"], session) != entry["sha256"]:
                    raise ValueError("Файл в карантине изменился.")
                self.client.move_no_replace(
                    entry["quarantined"], entry["original"], target=self.target
                )
        for playlist in journal["playlists"]:
            self._check_cancel(cancel)
            self._safe_existing(playlist["path"], root)
            current = self.client.read_bytes(playlist["path"], target=self.target)
            before, after = (
                base64.b64decode(playlist["before"]),
                base64.b64decode(playlist["after"]),
            )
            if current not in (before, after):
                raise ValueError("Плейлист изменился во время восстановления.")
            if current != before:
                self.client.write_bytes(playlist["path"], before, target=self.target)
        journal["state"] = "restored"
        self._save(session, journal)

    def purge(self, session: str, *, progress=None, cancel=None) -> None:
        journal = self._load(session)
        if journal["state"] not in ("quarantined", "purging"):
            raise ValueError("Окончательное удаление доступно только для завершённой очистки.")
        root = journal["music_root"]
        # Require every kept copy to still exist and match before any deletion.
        for entry in journal["entries"]:
            self._check_cancel(cancel)
            if self._hash(entry["kept"], root) != entry["sha256"]:
                raise ValueError("Сохранённая копия изменилась/исчезла. Удаление запрещено.")
            if self.client.path_exists(entry["quarantined"], target=self.target):
                if self._hash(entry["quarantined"], session) != entry["sha256"]:
                    raise ValueError("Файл в карантине изменился. Удаление остановлено.")
            elif journal["state"] != "purging":
                raise ValueError("Файл исчез из карантина. Удаление остановлено.")
        journal["state"] = "purging"
        self._save(session, journal)
        for index, entry in enumerate(journal["entries"], 1):
            self._check_cancel(cancel)
            if progress:
                progress(f"Окончательное удаление: {index}/{len(journal['entries'])}")
            if self.client.path_exists(entry["quarantined"], target=self.target):
                if (
                    self._hash(entry["kept"], root) != entry["sha256"]
                    or self._hash(entry["quarantined"], session) != entry["sha256"]
                ):
                    raise ValueError("Файл изменился во время удаления.")
                self.client.shell_rm(entry["quarantined"], target=self.target)
        journal["state"] = "purged"
        self._save(session, journal)
