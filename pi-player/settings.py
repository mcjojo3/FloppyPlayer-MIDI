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
    "source": "floppy",          # floppy | local | usb
    "auto_source": False,        # switch to a floppy or USB drive when one is put in
    "file_types": "all",         # all | midi | audio
    "play_mode": "normal",       # normal | shuffle_folder | shuffle_all | shuffle_favorites | repeat
    "autoplay": True,
    "back_limit": 25,            # tracks of play history kept, for stepping back with Prev
    "skip_bad_tracks": True,
    "loop_repeats": 1,          # ZUN loop count, -1 = forever like the games
    "volume": 0.5,               # 0.0-1.0, the single user-facing level
    # Per-engine trim so MIDI and audio match; raise midi_gain for a quiet soundfont.
    "midi_gain": 0.3,
    "audio_gain": 0.4,
    "loudness_match": False,     # level audio files by their ReplayGain tags
    "show_lyrics": False,
    "show_audio_bars": False,    # audio files: frequency bars instead of the cover
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
    # Recently played, newest first: {source scope: ["folder/file", ...]}.
    "recent": {},
    "listen_days": {},           # seconds listened per day: {"2026-09-19": 5400.0}
    "output": "speaker",         # "speaker" or a Bluetooth address
    "output_name": "Speaker jack",
    "alarm_enabled": False,      # Monday to Friday
    "alarm_time": 7 * 60 + 30,   # minutes since midnight
    "alarm_weekend_enabled": False,
    "alarm_weekend_time": 9 * 60,
    "alarm_volume": 0.5,         # what the alarm fades up to; becomes the volume
    "alarm_pick": "",            # what it wakes you on: "" favorites, "*" anything, else a folder
    "alarm_snooze": 9,           # minutes the Snooze button adds; 0 hides it
    "brightness": 1.0,
    "dim_after": 60,             # seconds idle before dimming, 0 = never
    "dim_level": 0.08,           # backlight when dimmed; 0.01 is the dimmest offered, 0 = off
    "dim_clock": False,          # dimmed: show a big clock instead of the dimmed screen
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
        if self._values["source"] == "sd":  # the RP2040's SD card is no longer a source
            self._values["source"] = "floppy"
        if 0 < self._values["dim_level"] < 0.01:  # below what the page now offers
            self._values["dim_level"] = 0.01
        # Before weekend alarms, the one alarm rang every day: keep weekends ringing.
        if "alarm_weekend_enabled" not in stored and self._values["alarm_enabled"]:
            self._values["alarm_weekend_enabled"] = True
            self._values["alarm_weekend_time"] = self._values["alarm_time"]
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
