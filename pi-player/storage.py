"""Playlists and whole-file reads: floppy and SD (via the RP2040) and the local music folder."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from link_client import BACKEND_FLOPPY, BACKEND_SD, DirEntry, LinkClient, LinkError, LinkStatusError

log = logging.getLogger(__name__)

FETCH_ATTEMPTS = 3

SOURCE_FLOPPY = "floppy"
SOURCE_SD = "sd"
SOURCE_LOCAL = "local"
SOURCES = (SOURCE_FLOPPY, SOURCE_SD, SOURCE_LOCAL)
LINK_BACKENDS = {SOURCE_FLOPPY: BACKEND_FLOPPY, SOURCE_SD: BACKEND_SD}

MIDI_EXTS = {"MID", "MIDI", "RMI"}
AUDIO_EXTS = {"MP3", "OGG", "OGA", "FLAC", "WAV"}

TYPE_ALL = "all"
TYPE_MIDI = "midi"
TYPE_AUDIO = "audio"
TYPES = (TYPE_ALL, TYPE_MIDI, TYPE_AUDIO)

_UNNUMBERED = 999_999

MAX_LOCAL_DEPTH = 5  # enough for Artist/Album/Disc trees


@dataclass(frozen=True)
class Track:
    """One playable file, wherever it lives."""

    display_name: str
    ext: str                       # uppercase, no dot
    entry: DirEntry | None = None  # link-backed sources
    path: Path | None = None       # local source
    folder: tuple[str, ...] = ()   # link sources: directory names from the root

    @property
    def kind(self) -> str:
        return "midi" if self.ext in MIDI_EXTS else "audio"


@dataclass
class Folder:
    name: str
    tracks: list[Track] = field(default_factory=list)


def _leading_number(name: str) -> int:
    digits = ""
    for char in name:
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else _UNNUMBERED


def _sort_key(track: Track):
    # Leading number first ("10THEM~1.MID" -> 10), unnumbered last, then by name.
    return (_leading_number(track.display_name), track.display_name.lower())


def _wanted(ext: str, file_types: str) -> bool:
    ext = ext.upper()
    if file_types == TYPE_MIDI:
        return ext in MIDI_EXTS
    if file_types == TYPE_AUDIO:
        return ext in AUDIO_EXTS
    return ext in MIDI_EXTS or ext in AUDIO_EXTS


# -- link-backed sources (floppy / SD) ------------------------------------


def _link_track(entry: DirEntry, file_types: str, folder: tuple[str, ...]) -> Track | None:
    if entry.is_dir or entry.is_volume_label or not _wanted(entry.ext, file_types):
        return None
    return Track(display_name=entry.filename, ext=entry.ext.upper(), entry=entry, folder=folder)


def _volume_label(listing: list[DirEntry]) -> str:
    entry = next((e for e in listing if e.is_volume_label and not e.is_dir), None)
    return (entry.name + entry.ext).strip() if entry else ""


def _build_link(link: LinkClient, backend: int, file_types: str) -> tuple[list[Folder], str]:
    if backend == BACKEND_SD:
        sd_root = link.list_root(BACKEND_SD)
        label = _volume_label(sd_root)
        midi_dir = next((e for e in sd_root if e.is_dir and e.name.upper() == "MIDI"), None)
        if midi_dir is None:
            log.warning("Source is SD but no /MIDI folder found - nothing to play.")
            return [], label
        listing = link.list_subdir(BACKEND_SD, midi_dir)
        base: tuple[str, ...] = (midi_dir.name,)
    else:
        listing = link.list_root(BACKEND_FLOPPY)
        label = _volume_label(listing)
        base = ()

    root = Folder(name="ROOT")
    subdirs: list[DirEntry] = []
    for entry in listing:
        if entry.is_volume_label:
            continue
        if entry.is_dir:
            subdirs.append(entry)
            continue
        track = _link_track(entry, file_types, base)
        if track:
            root.tracks.append(track)
    root.tracks.sort(key=_sort_key)

    folders = [root]
    for subdir in subdirs:
        try:
            entries = link.list_subdir(backend, subdir)
        except LinkError as exc:
            log.warning("Could not list %s: %s", subdir.name, exc)
            continue
        tracks = [t for t in (_link_track(e, file_types, base + (subdir.name,)) for e in entries) if t]
        if tracks:
            folders.append(Folder(name=subdir.name, tracks=sorted(tracks, key=_sort_key)))
    return folders, label


# -- local source ---------------------------------------------------------


def _build_local(music_dir: Path, file_types: str) -> list[Folder]:
    """Each directory with playable files becomes a folder named by its path, e.g. "TH9/MIDI"."""
    music_dir = Path(music_dir).expanduser()
    if not music_dir.is_dir():
        log.warning("Music folder %s does not exist.", music_dir)
        return []

    folders: list[Folder] = []
    for dirpath, dirnames, filenames in os.walk(music_dir):
        current = Path(dirpath)
        # Deterministic order, and don't descend into hidden folders.
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        relative = current.relative_to(music_dir)
        if len(relative.parts) >= MAX_LOCAL_DEPTH:
            dirnames[:] = []

        tracks = []
        for name in filenames:
            if name.startswith("."):
                continue
            ext = Path(name).suffix.lstrip(".").upper()
            if _wanted(ext, file_types):
                tracks.append(Track(display_name=name, ext=ext, path=current / name))
        if tracks:
            label = "ROOT" if relative == Path(".") else relative.as_posix()
            folders.append(Folder(name=label, tracks=sorted(tracks, key=_sort_key)))
    return folders


# -- public API -----------------------------------------------------------


def build_playlist(
    link: LinkClient | None, source: str, file_types: str, music_dir: Path
) -> tuple[list[Folder], str]:
    """Folders with playable files, plus the disk's volume label ("" if none)."""
    label = ""
    if source == SOURCE_LOCAL:
        folders = _build_local(music_dir, file_types)
    else:
        if link is None:
            return [], ""
        folders, label = _build_link(link, LINK_BACKENDS[source], file_types)

    return [f for f in folders if f.tracks], label


def fetch(
    link: LinkClient | None, source: str, track: Track, attempts: int = FETCH_ATTEMPTS
) -> bytes:
    """Read a whole track; raises on failure. Local audio returns b"" (streamed by path)."""
    if track.path is not None:
        return b"" if track.kind == "audio" else track.path.read_bytes()
    if link is None or track.entry is None:
        raise LinkError(f"no way to read {track.display_name}")

    backend = LINK_BACKENDS[source]
    entry = track.entry
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        handle = None
        try:
            handle, size = link.open(backend, entry)
            data = link.read_range(handle, 0, size)
            if len(data) != size:
                raise LinkError(f"short read: got {len(data)} of {size} bytes")
            return data
        except LinkError as exc:
            last_error = exc
            log.warning(
                "Fetch of %s failed (attempt %d/%d): %s",
                track.display_name, attempt, attempts, exc,
            )
            if backend == BACKEND_SD and handle is None and isinstance(exc, LinkStatusError):
                # The RP2040 may have recycled this SD folder's slot; re-listing reopens it.
                entry = _relocate(link, backend, track) or entry
        finally:
            if handle is not None:
                try:
                    link.close_handle(handle)
                except LinkError:
                    pass
    raise LinkError(f"could not read {track.display_name}: {last_error}")


def _relocate(link: LinkClient, backend: int, track: Track) -> DirEntry | None:
    """A fresh directory entry for track, found by walking its folder names."""
    try:
        listing = link.list_root(backend)
        for name in track.folder:
            folder = next((e for e in listing if e.is_dir and e.name == name), None)
            if folder is None:
                return None
            listing = link.list_subdir(backend, folder)
    except LinkError:
        return None
    old = track.entry
    return next(
        (e for e in listing if not e.is_dir and e.filename == old.filename and e.size == old.size),
        None,
    )
