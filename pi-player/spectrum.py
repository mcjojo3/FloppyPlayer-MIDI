"""Frequency bars for audio files. pygame.mixer.music doesn't expose what it plays, so each
file is decoded once in the background and its bars read back by playback position."""

from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import Path

import pygame

log = logging.getLogger(__name__)

BANDS = 16
FPS = 20                  # bar frames per second of audio
WINDOW = 4096             # FFT size: ~12 Hz resolution at 48 kHz, enough for the bass bands
LOW_HZ, HIGH_HZ = 40.0, 16000.0
HEADROOM_DB = 6.0         # kept above a band's loudest moments, so bars don't sit at the top
MIN_SPAN_DB = 18.0        # least quiet-to-loud travel, so a steady band can't jitter at full height
MAX_BOOST_DB = 30.0       # quiet bands are scaled up, but not from nothing
QUIET_DB = 80.0           # band energy (16-bit samples) of a faint -70 dBFS tone; silence stays down
VERSION = 2               # saved bars from an older scaling are worked out again
FALL = 0.8                # per frame, so bars fall away instead of blinking
MAX_SECONDS = 20 * 60     # longer files would decode to hundreds of MB
CHUNK = 256               # frames per FFT batch, bounding memory
CACHE_BYTES = 50_000_000  # saved bars, oldest dropped first (~30 KB a song)


class Spectrum:
    def __init__(self, frames, fps: int = FPS):
        self.frames = frames  # uint8 (frame, band)
        self.fps = fps

    def levels(self, position: float) -> list[float]:
        if not len(self.frames):
            return [0.0] * BANDS
        index = min(max(0, int(position * self.fps)), len(self.frames) - 1)
        return (self.frames[index] / 255).tolist()


def _cache_file(source, cache_dir: Path) -> Path:
    if isinstance(source, str):
        stat = os.stat(source)
        key = f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    else:
        key = bytes(source)
    return cache_dir / (hashlib.sha1(key).hexdigest()[:24] + ".npz")


def _duration_of(path: str) -> float:
    try:
        import mutagen
        return float(mutagen.File(path).info.length)
    except Exception:
        return 0.0


def load_or_analyse(source, duration: float, cache_dir: Path) -> Spectrum | None:
    """Bars from the disk cache, else worked out and saved there - a replay is instant."""
    try:
        import numpy as np
        file = _cache_file(source, cache_dir)
    except (ImportError, OSError):
        return analyse(source, duration)
    try:
        with np.load(file) as saved:
            frames, version = saved["frames"], int(saved["version"])
        if version == VERSION:
            os.utime(file)  # recently used: kept longest
            return Spectrum(frames)
    except (OSError, ValueError, KeyError):
        pass
    if not duration and isinstance(source, str):
        duration = _duration_of(source)  # a prefetched file hasn't been opened yet
    found = analyse(source, duration)
    if found is not None:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            partial = file.with_suffix(".tmp")
            with open(partial, "wb") as out:
                np.savez_compressed(out, frames=found.frames, version=VERSION)
            os.replace(partial, file)  # a power cut leaves no half-written file
            _trim(cache_dir)
        except OSError as exc:
            log.warning("Could not save bars: %s", exc)
    return found


def _trim(cache_dir: Path) -> None:
    files = sorted(cache_dir.glob("*.npz"), key=lambda f: f.stat().st_mtime, reverse=True)
    total = 0
    for file in files:
        total += file.stat().st_size
        if total > CACHE_BYTES:
            file.unlink(missing_ok=True)


def analyse(source, duration: float = 0.0) -> Spectrum | None:
    """Bars for a whole file (a path, or the bytes of one); None if they can't be made."""
    if duration > MAX_SECONDS:
        return None
    try:
        import numpy as np
    except ImportError:
        log.warning("Audio bars need numpy (sudo apt install python3-numpy)")
        return None
    mixer = pygame.mixer.get_init()
    if mixer is None:
        return None
    rate = mixer[0]
    try:
        sound = pygame.mixer.Sound(file=source if isinstance(source, str) else io.BytesIO(source))
        samples = pygame.sndarray.samples(sound)  # the decoded audio itself, not a copy
    except (pygame.error, ValueError, TypeError) as exc:
        log.warning("No bars for this file: %s", exc)
        return None

    hop = rate // FPS
    count = max(0, (len(samples) - WINDOW) // hop + 1)
    freqs = np.fft.rfftfreq(WINDOW, 1 / rate)
    edges = np.geomspace(LOW_HZ, min(HIGH_HZ, rate / 2), BANDS + 1)
    lows = np.searchsorted(freqs, edges[:-1])
    highs = np.maximum(np.searchsorted(freqs, edges[1:]), lows + 1)
    window = np.hanning(WINDOW).astype(np.float32)
    energy = np.zeros((count, BANDS), np.float32)
    for start in range(0, count, CHUNK):
        n = min(CHUNK, count - start)
        block = samples[start * hop:(start + n - 1) * hop + WINDOW].astype(np.float32)
        if block.ndim > 1:
            block = block.mean(axis=1)
        frames = np.lib.stride_tricks.sliding_window_view(block, WINDOW)[::hop][:n] * window
        power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        for band, (low, high) in enumerate(zip(lows, highs)):
            energy[start:start + n, band] = power[:, low:high].sum(axis=1)
    del samples, sound

    if not count:
        return Spectrum(np.zeros((0, BANDS), np.uint8))
    db = 10 * np.log10(energy + 1e-9)
    # Each band's own quiet-to-loud range fills the bar, so it moves instead of sitting near the
    # top: the headroom keeps the very top for peaks, and empty bands stay down.
    top = np.percentile(db, 99, axis=0) + HEADROOM_DB
    top = np.maximum(top, max(top.max() - MAX_BOOST_DB, QUIET_DB))
    floor = np.minimum(np.percentile(db, 10, axis=0), top - MIN_SPAN_DB)
    levels = np.clip((db - floor) / (top - floor), 0.0, 1.0)
    for t in range(1, count):
        np.maximum(levels[t], levels[t - 1] * FALL, out=levels[t])
    return Spectrum((levels * 255).astype(np.uint8))
