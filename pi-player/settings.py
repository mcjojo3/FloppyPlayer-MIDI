"""Settings, persisted as JSON."""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "floppyplayer" / "settings.json"

DEFAULTS = {
    "serial_port": "/dev/ttyACM0",
    # Scanned for .sf2 files.
    "soundfont_dirs": [
        str(Path(__file__).resolve().parent / "soundfonts"),
        str(Path.home() / "soundfonts"),
        "/usr/share/sounds/sf2",
    ],
    # Two soundfonts, picked in Settings and swapped from the Playing screen.
    "soundfont_a": "",
    "soundfont_b": "",
    "soundfont_slot": "a",
    "music_dir": str(Path.home() / "Music"),
    # auto tries pulseaudio then alsa; force one by name if auto picks wrong.
    "audio_driver": "auto",
    "audio_rate": 48000,  # PipeWire's native rate - avoids resampling
    # Chorus is the costly effect; the SC-88Pro these files target had 64 voices.
    "synth_polyphony": 128,
    "synth_reverb": True,
    "synth_chorus": False,
    # Set when the display is rotated 180 in cmdline.txt - touch isn't rotated with it.
    "rotate_touch_180": False,
    "source": "floppy",          # floppy | sd | local
    "file_types": "all",         # all | midi | audio
    "play_mode": "normal",       # normal | shuffle_folder | shuffle_all | shuffle_favorites | repeat
    "autoplay": True,
    "skip_bad_tracks": True,
    "loop_repeats": -1,          # ZUN loop count, -1 = forever like the games
    "volume": 0.6,               # 0.0-1.0, the single user-facing level
    # Per-engine trim so MIDI and audio match; raise midi_gain for a quiet soundfont.
    "midi_gain": 0.5,
    "audio_gain": 0.5,
    "resume_on_boot": True,
    "resume": {},                # where playback was when the app last stopped
    "eq_gains": [0.0, 0.0, 0.0, 0.0, 0.0],
    "eq_preset": "Flat",
    # Folders left out of Shuffle all: {source scope: [folder names]}.
    "shuffle_excluded": {},
    # Hearted tracks: {source scope: ["folder/file", ...]}.
    "favorites": {},
    # Play counts: {source scope: {"folder/file": plays}}.
    "play_counts": {},
    "output": "speaker",         # "speaker" or a Bluetooth address
    "output_name": "Speaker jack",
    "alarm_enabled": False,
    "alarm_time": 7 * 60 + 30,   # minutes since midnight
    "brightness": 1.0,
    "dim_after": 60,             # seconds idle before dimming, 0 = never
}


def _compatible(default, value) -> bool:
    """Reject hand-edited values of the wrong type instead of crashing later."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


class Settings:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._values = copy.deepcopy(DEFAULTS)
        self._dirty = False
        self._save_lock = threading.Lock()  # the UI and worker threads both save
        self.load()

    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable settings file %s: %s", self.path, exc)
            return
        if not isinstance(stored, dict):
            return
        for key, value in stored.items():
            if key in DEFAULTS and _compatible(DEFAULTS[key], value):
                self._values[key] = value
        if self._values["play_mode"] == "shuffle":  # before shuffle was split
            self._values["play_mode"] = "shuffle_folder"
        # Before the A/B slots: one font plus the previously used one.
        if not self._values["soundfont_a"] and isinstance(stored.get("soundfont"), str):
            self._values["soundfont_a"] = stored["soundfont"]
        if not self._values["soundfont_b"] and isinstance(stored.get("soundfont_alt"), str):
            self._values["soundfont_b"] = stored["soundfont_alt"]

    def save(self) -> None:
        with self._save_lock:
            self._dirty = False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._values, indent=2))
                tmp.replace(self.path)  # atomic, so a power cut can't truncate it
            except OSError as exc:
                log.warning("Could not save settings: %s", exc)

    def flush(self) -> None:
        """Write pending deferred changes."""
        if self._dirty:
            self.save()

    def __getitem__(self, key: str):
        return self._values[key]

    def __setitem__(self, key: str, value) -> None:
        if self._values.get(key) == value:
            return
        self._values[key] = value
        self.save()

    def set_deferred(self, key: str, value) -> None:
        """Change a value without writing yet - for fast-changing ones like volume."""
        if self._values.get(key) == value:
            return
        self._values[key] = value
        self._dirty = True

    def get(self, key: str, default=None):
        return self._values.get(key, default)
