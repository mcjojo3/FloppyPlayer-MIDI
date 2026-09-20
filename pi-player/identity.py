"""Tracks identified by what's in them, so the same music is one track wherever it sits:
two folders, a USB drive and the music folder, or a floppy and a copy of it."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

WHOLE_FILE = 2_000_000  # smaller files are hashed entirely; bigger ones by their ends
ENDS = 65536
ID_BYTES = 8  # 16 hex characters: plenty against accidental collisions


def id_of_bytes(data: bytes) -> str:
    """For a file already in memory - MIDI files and anything read off a floppy."""
    return hashlib.blake2b(data, digest_size=ID_BYTES).hexdigest()


def id_of_file(path) -> str | None:
    """Whole file when small, else its size with the first and last 64 KB - fast on any drive."""
    try:
        size = os.path.getsize(path)
        digest = hashlib.blake2b(digest_size=ID_BYTES)
        with open(path, "rb") as f:
            if size <= WHOLE_FILE:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
                return digest.hexdigest()
            digest.update(str(size).encode())
            digest.update(f.read(ENDS))
            f.seek(-ENDS, os.SEEK_END)
            digest.update(f.read(ENDS))
        return digest.hexdigest()
    except OSError as exc:
        log.debug("No id for %s: %s", path, exc)
        return None


class Ids:
    """Known content ids, cached so a file is only ever hashed once, and the name each id was
    last seen under, for screens that list tracks which aren't in the playlist."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.dirty = False
        self._files: dict[str, list] = {}  # cache key -> [size, mtime_ns, id]
        self._names: dict[str, str] = {}   # id -> "folder/file"
        try:
            stored = json.loads(self.path.read_text())
            self._files = {k: v for k, v in stored.get("files", {}).items() if len(v) == 3}
            self._names = dict(stored.get("names", {}))
        except (OSError, ValueError, AttributeError):
            pass

    def known(self, key: str, path=None) -> str | None:
        """The id if it's already worked out and the file hasn't changed since."""
        found = self._files.get(key)
        if found is None:
            return None
        if path is not None:
            try:
                stat = os.stat(path)
            except OSError:
                return None
            if [stat.st_size, stat.st_mtime_ns] != found[:2]:
                return None  # replaced since: worth hashing again
        return found[2]

    def remember(self, key: str, content_id: str, name: str = "", path=None) -> None:
        size, mtime = 0, 0
        if path is not None:
            try:
                stat = os.stat(path)
                size, mtime = stat.st_size, stat.st_mtime_ns
            except OSError:
                pass
        self._files[key] = [size, mtime, content_id]
        if name:
            self._names[content_id] = name
        self.dirty = True

    def name_of(self, content_id: str) -> str:
        return self._names.get(content_id, "")

    def of_file(self, key: str, path, name: str = "") -> str | None:
        """The id of a file, working it out and caching it the first time."""
        found = self.known(key, path)
        if found is None:
            found = id_of_file(path)
            if found is not None:
                self.remember(key, found, name, path)
        elif name and self._names.get(found) != name:
            self._names[found] = name
            self.dirty = True
        return found

    def save(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"files": self._files, "names": self._names}))
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("Could not save track ids: %s", exc)
