"""Playlists and whole-file reads: the floppy (via the RP2040), the music folder and USB drives."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from link_client import BACKEND_FLOPPY, DirEntry, LinkClient, LinkError

log = logging.getLogger(__name__)

FETCH_ATTEMPTS = 3

SOURCE_FLOPPY = "floppy"
SOURCE_LOCAL = "local"
SOURCE_USB = "usb"
SOURCES = (SOURCE_FLOPPY, SOURCE_LOCAL, SOURCE_USB)

# Where system/install.sh's udev rule mounts drives, and where a desktop would.
USB_MOUNT_ROOTS = ("/run/media/system", "/media")

MIDI_EXTS = {"MID", "MIDI", "RMI"}
AUDIO_EXTS = {"MP3", "OGG", "OGA", "FLAC", "WAV"}

TYPE_ALL = "all"
TYPE_MIDI = "midi"
TYPE_AUDIO = "audio"
TYPES = (TYPE_ALL, TYPE_MIDI, TYPE_AUDIO)

_UNNUMBERED = 999_999

MAX_DEPTH = 5  # enough for Artist/Album/Disc trees


@dataclass(frozen=True)
class Track:
    """One playable file, wherever it lives."""

    display_name: str
    ext: str                       # uppercase, no dot
    entry: DirEntry | None = None  # floppy
    path: Path | None = None       # music folder and USB

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


# -- floppy -------------------------------------------------------------------


def _floppy_track(entry: DirEntry, file_types: str) -> Track | None:
    if entry.is_dir or entry.is_volume_label or not _wanted(entry.ext, file_types):
        return None
    return Track(display_name=entry.filename, ext=entry.ext.upper(), entry=entry)


def _build_floppy(link: LinkClient, file_types: str) -> tuple[list[Folder], str]:
    listing = link.list_root(BACKEND_FLOPPY)
    label_entry = next((e for e in listing if e.is_volume_label and not e.is_dir), None)
    label = (label_entry.name + label_entry.ext).strip() if label_entry else ""

    root = Folder(name="ROOT")
    subdirs = [e for e in listing if e.is_dir and not e.is_volume_label]
    root.tracks = sorted(filter(None, (_floppy_track(e, file_types) for e in listing)), key=_sort_key)

    folders = [root]
    for subdir in subdirs:
        try:
            entries = link.list_subdir(BACKEND_FLOPPY, subdir)
        except LinkError as exc:
            log.warning("Could not list %s: %s", subdir.name, exc)
            continue
        tracks = sorted(filter(None, (_floppy_track(e, file_types) for e in entries)), key=_sort_key)
        folders.append(Folder(name=subdir.name, tracks=tracks))
    return folders, label


# -- music folder and USB drives ----------------------------------------------


def _build_tree(root: Path, file_types: str, prefix: str = "") -> list[Folder]:
    """Each directory with playable files becomes a folder named by its path, e.g. "TH9/MIDI"."""
    folders: list[Folder] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        relative = current.relative_to(root)
        if len(relative.parts) >= MAX_DEPTH:
            dirnames[:] = []

        tracks = [
            Track(display_name=name, ext=Path(name).suffix.lstrip(".").upper(), path=current / name)
            for name in filenames
            if not name.startswith(".") and _wanted(Path(name).suffix.lstrip("."), file_types)
        ]
        if tracks:
            path = "" if relative == Path(".") else relative.as_posix()
            name = "/".join(p for p in (prefix, path) if p) or "ROOT"
            folders.append(Folder(name=name, tracks=sorted(tracks, key=_sort_key)))
    return folders


def usb_drives(roots=USB_MOUNT_ROOTS) -> list[Path]:
    """Mounted USB drives: /run/media/system/<label>, or a desktop's /media/<user>/<label>."""
    drives: list[Path] = []
    for root in roots:
        try:
            children = sorted(Path(root).iterdir())
        except OSError:
            continue
        for child in children:
            if os.path.ismount(child):
                drives.append(child)
            elif child.is_dir():
                try:
                    drives += sorted(p for p in child.iterdir() if os.path.ismount(p))
                except OSError:
                    pass
    return drives


def has_music(root: Path, file_types: str) -> bool:
    """Any playable file within MAX_DEPTH - stops at the first."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if len(Path(dirpath).relative_to(root).parts) >= MAX_DEPTH:
            dirnames[:] = []
        if any(not n.startswith(".") and _wanted(Path(n).suffix.lstrip("."), file_types) for n in filenames):
            return True
    return False


# -- public API -----------------------------------------------------------------


def build_playlist(
    link: LinkClient | None, source: str, file_types: str, music_dir: Path
) -> tuple[list[Folder], str]:
    """Folders with playable files, plus a label for the medium ("" if none)."""
    label = ""
    if source == SOURCE_LOCAL:
        music_dir = Path(music_dir).expanduser()
        if not music_dir.is_dir():
            log.warning("Music folder %s does not exist.", music_dir)
            return [], ""
        folders = _build_tree(music_dir, file_types)
    elif source == SOURCE_USB:
        drives = usb_drives()
        folders = [f for drive in drives for f in _build_tree(drive, file_types, prefix=drive.name)]
        label = ", ".join(drive.name for drive in drives)
    elif link is None:
        return [], ""
    else:
        folders, label = _build_floppy(link, file_types)
    return [f for f in folders if f.tracks], label


def fetch(
    link: LinkClient | None, source: str, track: Track, attempts: int = FETCH_ATTEMPTS
) -> bytes:
    """Read a whole track; raises on failure. Audio files on disk return b"" (streamed by path)."""
    if track.path is not None:
        return b"" if track.kind == "audio" else track.path.read_bytes()
    if link is None or track.entry is None:
        raise LinkError(f"no way to read {track.display_name}")

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        handle = None
        try:
            handle, size = link.open(BACKEND_FLOPPY, track.entry)
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
        finally:
            if handle is not None:
                try:
                    link.close_handle(handle)
                except LinkError:
                    pass
    raise LinkError(f"could not read {track.display_name}: {last_error}")
