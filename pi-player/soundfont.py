"""SoundFonts: single .sf2 files, and folders of one-instrument .sf2 files played as one."""

from __future__ import annotations

import logging
import re
import struct
from pathlib import Path

log = logging.getLogger(__name__)

MAP_FILE = "instruments.txt"
DRUM_BANK = 128

# "6.1 = file.sf2" or "drums 9 = file.sf2"; numbers are 1-based like GM charts.
_MAP_LINE = re.compile(r"^(drums\s+)?(\d+)(?:\.(\d+))?\s*=\s*(.+?)\s*$", re.I)
_TRAILING_COMMENT = re.compile(r"\s+#\s.*$")
# "06.01 Brite FM EP.sf2" -> program 6, variation 1
_LEADING_NUMBER = re.compile(r"^(\d+)(?:\.(\d+))?")


def sf2_presets(path) -> list[tuple[str, int, int]]:
    """(name, bank, preset) of each preset, from the headers - the samples are skipped, not read."""
    with open(path, "rb") as f:
        riff, _, kind = struct.unpack("<4sI4s", f.read(12))
        if riff != b"RIFF" or kind != b"sfbk":
            raise ValueError("not a SoundFont")
        while True:
            head = f.read(8)
            if len(head) < 8:
                raise ValueError("no preset headers")
            chunk, size = struct.unpack("<4sI", head)
            if chunk != b"LIST" or f.read(4) != b"pdta":
                f.seek(size - (4 if chunk == b"LIST" else 0) + (size & 1), 1)
                continue
            end = f.tell() + size - 4
            while f.tell() + 8 <= end:
                sub, sub_size = struct.unpack("<4sI", f.read(8))
                if sub == b"phdr":
                    data = f.read(sub_size)
                    return [  # the last record is the end marker
                        (name.split(b"\0")[0].decode("latin-1").strip(), bank, preset)
                        for name, preset, bank in (
                            struct.unpack_from("<20sHH", data, i) for i in range(0, len(data) - 38, 38)
                        )
                    ]
                f.seek(sub_size + (sub_size & 1), 1)
            raise ValueError("no preset headers")


def _is_sf2(path: Path) -> bool:
    return path.suffix.lower() == ".sf2" and not path.name.startswith(".")


def find_soundfonts(dirs) -> list[Path]:
    """Every .sf2 across the configured folders, plus each sub-folder of .sf2 files, sorted by name."""
    found: dict[str, Path] = {}
    for directory in dirs:
        path = Path(directory).expanduser()
        if not path.is_dir():
            continue
        for entry in path.iterdir():
            if entry.is_file() and _is_sf2(entry):
                found.setdefault(entry.name, entry)
            elif entry.is_dir() and not entry.name.startswith(".") and any(map(_is_sf2, entry.iterdir())):
                found.setdefault(entry.name, entry)
    return [found[name] for name in sorted(found, key=str.lower)]


def read_map(folder: Path) -> dict[tuple[bool, int, int], Path]:
    """{(drum, variation, program 0-127): file} from instruments.txt, else from the file names."""
    mapping: dict[tuple[bool, int, int], Path] = {}
    map_file = folder / MAP_FILE
    if map_file.is_file():
        lines = map_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for number, line in enumerate(lines, 1):
            line = _TRAILING_COMMENT.sub("", line).strip()
            if not line or line.startswith("#"):
                continue
            match = _MAP_LINE.match(line)
            if not match:
                log.warning("%s line %d not understood: %s", map_file, number, line)
                continue
            drums, program, variation, name = match.groups()
            file, program = folder / name, int(program) - 1
            if not 0 <= program <= 127 or not file.is_file():
                log.warning("%s line %d: %s", map_file, number,
                            f"no file {name}" if 0 <= program <= 127 else "numbers go 1-128")
                continue
            mapping.setdefault((bool(drums), int(variation or 0), program), file)
        return mapping
    for file in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        match = _LEADING_NUMBER.match(file.name)
        if match and _is_sf2(file) and 1 <= int(match.group(1)) <= 128:
            mapping.setdefault((False, int(match.group(2) or 0), int(match.group(1)) - 1), file)
    return mapping


_counts: dict[tuple, int] = {}


def instrument_count(folder: Path) -> int:
    """Instruments a folder maps - cached until the folder or its map changes."""
    folder = Path(folder)
    try:
        map_file = folder / MAP_FILE
        stamp = (str(folder), folder.stat().st_mtime, map_file.stat().st_mtime if map_file.exists() else 0)
    except OSError:
        return 0
    if stamp not in _counts:
        _counts[stamp] = len(set(read_map(folder).values()))
    return _counts[stamp]


def instrument_label(file: Path) -> str:
    """ "06.01 Brite FM EP.sf2" -> "Brite FM EP" """
    return _LEADING_NUMBER.sub("", file.stem).lstrip(". ").strip() or file.stem


class FileFont:
    """One .sf2 file."""

    def __init__(self, path):
        self.path = Path(path)
        self.sfid = -1

    def load(self, fs) -> None:
        if self.sfid == -1:
            sfid = fs.sfload(str(self.path))
            if sfid == -1:
                raise RuntimeError(f"could not load soundfont: {self.path}")
            self.sfid = sfid

    def select(self, fs, channel: int, candidates, drum: bool) -> tuple[int, int, int] | None:
        """Select the first candidate (bank, program) this font has; (sfid, bank, preset) or None."""
        self.load(fs)
        for bank, program in candidates:
            if fs.program_select(channel, self.sfid, bank, program) == 0:
                return self.sfid, bank, program
        return None

    def size_of(self, sfid: int) -> int:
        return 0  # a whole-GM font's samples load per preset; their size isn't known up front

    def label_of(self, sfid: int) -> str | None:
        return None

    def unload(self, fs) -> None:
        if self.sfid != -1:
            fs.sfunload(self.sfid)
            self.sfid = -1


class FolderFont:
    """A folder of one-instrument .sf2 files, mapped to programs by instruments.txt or file names.
    Each file loads the first time a song needs it."""

    def __init__(self, path):
        self.path = Path(path)
        self.map = read_map(self.path)
        if not self.map:
            raise RuntimeError(f"no instruments found in {self.path}")
        self._loaded: dict[Path, tuple[int, int, int] | None] = {}  # file -> (sfid, bank, preset)
        self._files: dict[int, Path] = {}  # sfid -> file

    def load(self, fs) -> None:
        pass  # files load when first needed

    def _file_for(self, drum: bool, bank: int, program: int) -> Path | None:
        if drum:
            return self.map.get((True, 0, program))
        exact = self.map.get((False, bank, program))
        if exact is not None:
            return exact
        # A missing variation falls back to the program's main tone, like a GS module.
        variations = sorted(v for d, v, p in self.map if not d and p == program)
        return self.map[(False, variations[0], program)] if variations else None

    def _instrument(self, fs, file: Path) -> tuple[int, int, int] | None:
        if file not in self._loaded:
            found = None
            try:
                presets = sf2_presets(file)
                sfid = fs.sfload(str(file)) if presets else -1
                if sfid != -1:
                    found = (sfid, presets[0][1], presets[0][2])
                    self._files[sfid] = file
                else:
                    log.warning("Could not load %s", file.name)
            except (OSError, ValueError, struct.error) as exc:
                log.warning("Unreadable instrument %s: %s", file.name, exc)
            self._loaded[file] = found
        return self._loaded[file]

    def select(self, fs, channel: int, candidates, drum: bool) -> tuple[int, int, int] | None:
        for bank, program in candidates:
            file = self._file_for(drum or bank == DRUM_BANK, bank, program)
            found = self._instrument(fs, file) if file else None
            if found and fs.program_select(channel, *found) == 0:
                return found
        return None

    def size_of(self, sfid: int) -> int:
        file = self._files.get(sfid)
        try:
            return file.stat().st_size if file else 0
        except OSError:
            return 0

    def label_of(self, sfid: int) -> str | None:
        file = self._files.get(sfid)
        return instrument_label(file) if file else None

    def unload(self, fs) -> None:
        for sfid in self._files:
            fs.sfunload(sfid)
        self._files.clear()
        self._loaded.clear()


def open_font(path) -> FileFont | FolderFont:
    return FolderFont(path) if Path(path).is_dir() else FileFont(path)
