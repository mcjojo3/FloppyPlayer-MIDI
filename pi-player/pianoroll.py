"""The notes of a song as (start, end, channel, note), for the falling-notes view."""

from __future__ import annotations

import bisect
from dataclasses import dataclass

DRUM_CHANNEL = 9  # drums aren't pitches - left out


@dataclass
class Roll:
    notes: list[tuple[float, float, int, int]]  # sorted by start
    starts: list[float]
    longest: float
    low: int   # lowest and highest note played
    high: int

    def window(self, start: float, end: float):
        """Notes sounding at any time between start and end."""
        first = bisect.bisect_left(self.starts, start - self.longest)
        last = bisect.bisect_right(self.starts, end)
        return [n for n in self.notes[first:last] if n[1] > start]


def build(song) -> Roll:
    sounding: dict[tuple[int, int], float] = {}
    notes: list[tuple[float, float, int, int]] = []
    for when, msg in song.events:
        if msg.type not in ("note_on", "note_off") or msg.channel == DRUM_CHANNEL:
            continue
        key = (msg.channel, msg.note)
        if key in sounding:  # a note-off, or the same note struck again
            notes.append((sounding.pop(key), when, *key))
        if msg.type == "note_on" and msg.velocity > 0:
            sounding[key] = when
    notes += [(start, song.duration, *key) for key, start in sounding.items()]
    notes.sort()
    pitches = [n[3] for n in notes] or [60]
    return Roll(
        notes=notes,
        starts=[n[0] for n in notes],
        longest=max((end - start for start, end, _, _ in notes), default=0.0),
        low=min(pitches),
        high=max(pitches),
    )
