"""Song lyrics: a .lrc file beside the track, or the file's own tags; timed when possible."""

from __future__ import annotations

import bisect
import codecs
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_STAMP = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD_STAMP = re.compile(r"<\d+:\d{1,2}(?:[.:]\d{1,3})?>")  # enhanced LRC, per word
_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]$", re.I)
_HEADER = re.compile(r"^\[[a-z#]+:.*\]$", re.I)  # [ar:...], [ti:...], [length:...]
_TAG_KEYS = ("lyrics", "unsyncedlyrics", "syncedlyrics")


@dataclass(frozen=True)
class Lyrics:
    lines: tuple[str, ...]
    times: tuple[float, ...] | None = None  # seconds per line; None when untimed

    @property
    def synced(self) -> bool:
        return self.times is not None

    def line_at(self, position: float, duration: float) -> int:
        """The current line; -1 before the first timed one. Untimed lyrics follow the progress."""
        if self.times is not None:
            return bisect.bisect_right(self.times, position) - 1
        if duration <= 0:
            return 0
        return min(len(self.lines) - 1, int(position / duration * len(self.lines)))


def parse(text: str) -> Lyrics | None:
    """LRC ("[01:23.45]words") or plain text."""
    offset = 0.0
    timed: list[tuple[float, str]] = []
    plain: list[str] = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        match = _OFFSET.match(line)
        if match:
            offset = int(match.group(1)) / 1000  # positive shows lyrics sooner
            continue
        stamps = []
        while (match := _STAMP.match(line)):
            stamps.append(int(match.group(1)) * 60 + float(match.group(2).replace(":", ".")))
            line = line[match.end():].strip()
        if stamps:
            words = _WORD_STAMP.sub("", line).strip()
            timed += [(t, words) for t in stamps]
        elif not _HEADER.match(line):
            plain.append(line)
    if timed:
        timed.sort(key=lambda pair: pair[0])
        return Lyrics(tuple(w for _, w in timed), tuple(max(0.0, t - offset) for t, _ in timed))
    # Untimed: keep single blank lines between verses, drop the rest.
    lines: list[str] = []
    for line in plain:
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return Lyrics(tuple(lines)) if lines else None


def _decode(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    for encoding in ("utf-8-sig", "cp932"):  # cp932: Japanese .lrc files
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def _sidecar(path: Path) -> Path | None:
    for name in (path.stem + ".lrc", path.stem + ".LRC"):
        candidate = path.with_name(name)
        if candidate.is_file():
            return candidate
    wanted = (path.stem + ".lrc").lower()
    try:
        return next((p for p in path.parent.iterdir() if p.name.lower() == wanted), None)
    except OSError:
        return None


def _from_sylt(frame) -> Lyrics | None:
    """ID3 timed lyrics. Karaoke files split lines into syllables, marked by newlines."""
    if frame.format != 2 or not frame.text:  # 2 = milliseconds
        return None
    entries = sorted(((time / 1000, str(text)) for text, time in frame.text), key=lambda e: e[0])
    by_syllable = any("\n" in text for _, text in entries)
    lines: list[list] = []
    for start, text in entries:
        if not lines or not by_syllable or text.startswith(("\n", "\r")):
            lines.append([start, text.lstrip("\r\n")])  # keep the space before the next syllable
        else:
            lines[-1][1] += text
    lines = [(start, text.strip()) for start, text in lines]
    return Lyrics(tuple(t for _, t in lines), tuple(s for s, _ in lines)) if lines else None


def _from_tags(media) -> Lyrics | None:
    tags = getattr(media, "tags", None)
    if tags is None:
        return None
    if hasattr(tags, "getall"):  # ID3: MP3, WAV
        for frame in tags.getall("SYLT"):
            found = _from_sylt(frame)
            if found:
                return found
        for frame in tags.getall("USLT"):
            found = parse(str(frame.text))
            if found:
                return found
        return None
    for key in _TAG_KEYS:  # Vorbis comments: OGG, FLAC
        values = tags.get(key)
        if values:
            found = parse(str(values[0]))
            if found:
                return found
    return None


def find(path: Path | None, media) -> Lyrics | None:
    """A .lrc beside the file wins - usually timed - then the file's own tags."""
    if path is not None:
        lrc = _sidecar(path)
        if lrc is not None:
            try:
                found = parse(_decode(lrc.read_bytes()))
                if found:
                    return found
            except OSError as exc:
                log.debug("Could not read %s: %s", lrc, exc)
    try:
        return _from_tags(media)
    except Exception as exc:
        log.debug("No usable lyrics tag: %s", exc)
        return None
