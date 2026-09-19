"""FloppyPlayer: MIDI and audio from a floppy, SD card or the Pi's music folder.

Link, PipeWire and Bluetooth calls block, so they run on worker threads; the
UI thread only reads state and posts tasks.
"""

from __future__ import annotations

import logging
import queue
import random
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

import bt
import eq
import pw
import storage
from backlight import Backlight
from link_client import BACKEND_FLOPPY, LinkClient, LinkError
from playback import Playback
from player import find_soundfonts, parse
from settings import Settings
from shuffle import ShuffleOrder
from storage import SOURCE_FLOPPY, SOURCE_LOCAL, SOURCES, TYPES
from ui import Ui, decode_art

log = logging.getLogger("floppyplayer")

PLAY_MODES = ("normal", "shuffle_folder", "shuffle_all", "shuffle_favorites", "repeat")
SHUFFLE_MODES = ("shuffle_folder", "shuffle_all", "shuffle_favorites")
LOOP_CHOICES = (-1, 0, 1, 2, 4, 8)
SLEEP_CHOICES = (0, 15, 30, 45, 60, 90)  # minutes
BRIGHTNESS_CHOICES = (1.0, 0.75, 0.5, 0.25)
DIM_CHOICES = (0, 30, 60, 120, 300)  # seconds
DIM_LEVEL = 0.08
SLEEP_FADE_S = 8.0
ALARM_FADE_S = 60.0
ALARM_START_LEVEL = 0.15
PLAY_COUNT_FRACTION = 0.1   # share of a track listened to that counts as a play
PLAY_COUNT_FALLBACK_S = 30  # when the length is unknown
SOUNDFONT_SLOTS = ("a", "b")
RESUME_SAVE_INTERVAL = 30.0
SONG_CACHE_SIZE = 8  # parsed MIDIs kept for Prev and re-picks
DISK_POLL_INTERVAL = 2.0
EQ_RETRY_S = 10.0
BT_SCAN_S = 8
BT_SINK_WAIT_S = 8.0
BOOT_SINK_WAIT_S = 15.0
SPEAKER = "speaker"
EXIT_RESTART = 42  # non-zero: systemd's Restart=on-failure relaunches


class App:
    def __init__(self):
        self.settings = Settings()
        self.folders: list[storage.Folder] = []
        self.folder_index = 0
        self.track_index = 0
        self.disk_label = ""
        self.status_text = "Starting..."

        self.link: LinkClient | None = None
        self.playback: Playback | None = None
        self.ui: Ui | None = None
        self.backlight = Backlight()
        self.equalizer = eq.Equalizer()
        self.eq_available = False
        self.bt_available = bt.available()
        self.bt_devices: list[bt.Device] = []
        self.bt_status = ""
        self.sleep_minutes = 0

        self._tasks: queue.Queue = queue.Queue()      # link + playback
        self._eq_tasks: queue.Queue = queue.Queue()
        self._bt_tasks: queue.Queue = queue.Queue()
        self._running = True
        self._last_poll = 0.0
        self._last_remember = 0.0
        self._exit_code = 0
        self._songs: OrderedDict = OrderedDict()  # worker thread only
        self._shuffle = ShuffleOrder()
        self._shuffle_key = None  # scope the order was built for
        self._playlist_gen = 0
        self._resume_pending = (
            self.settings["resume"] if self.settings["resume_on_boot"] else None
        )
        self._art: tuple | None = None  # (Track, Surface | None)
        # [scope, track key, seconds listened, counted] for the loaded track.
        self._listen: list | None = None
        self._listen_lock = threading.Lock()  # worker resets, UI tick adds
        self._last_tick = time.monotonic()
        self._eq_pending = False
        self._last_eq_check = 0.0
        self._bt_busy = False
        self._sleep_deadline: float | None = None
        self._alarm_fade_end: float | None = None
        self._alarm_last: tuple | None = None  # day and minute the alarm last fired

    # -- state the UI reads ----------------------------------------------

    @property
    def source(self) -> str:
        return self.settings["source"]

    @property
    def needs_link(self) -> bool:
        return self.source != SOURCE_LOCAL

    @property
    def current_folder(self) -> storage.Folder | None:
        if 0 <= self.folder_index < len(self.folders):
            return self.folders[self.folder_index]
        return None

    @property
    def current_track(self) -> storage.Track | None:
        folder = self.current_folder
        if folder and 0 <= self.track_index < len(folder.tracks):
            return folder.tracks[self.track_index]
        return None

    @property
    def current_track_name(self) -> str:
        track = self.current_track
        return track.display_name if track else ""

    @property
    def current_folder_name(self) -> str:
        folder = self.current_folder
        if not folder:
            return ""
        return f"{folder.name}  -  {self.track_index + 1}/{len(folder.tracks)}"

    @property
    def _loaded_info(self) -> dict:
        """Tags for the current track, once it has actually loaded."""
        if not self.playback or self.playback.track is None:
            return {}
        if self.playback.track != self.current_track:
            return {}  # cursor moved, new track still loading
        return self.playback.info

    @property
    def track_title(self) -> str:
        return self._loaded_info.get("title") or self.current_track_name

    @property
    def track_info(self) -> dict:
        return self._loaded_info

    @property
    def track_subtitle(self) -> str:
        info = self._loaded_info
        parts = [info.get("artist", ""), info.get("album", ""), info.get("copyright", "")]
        return "  -  ".join(p for p in parts if p)

    @property
    def track_art(self):
        track = self.current_track
        if self._art and track is not None and self._art[0] == track:
            return self._art[1]
        return None

    @property
    def is_playing(self) -> bool:
        return bool(self.playback and self.playback.is_playing)

    @property
    def position(self) -> float:
        return self.playback.position if self.playback else 0.0

    @property
    def duration(self) -> float:
        return self.playback.duration if self.playback else 0.0

    @property
    def loop_status(self) -> str:
        return self.playback.loop_status if self.playback else ""

    @property
    def track_kind(self) -> str:
        track = self.current_track
        return track.ext if track else ""

    @property
    def midi(self):
        """The MIDI engine while a MIDI track is loaded, else None."""
        if self.playback and self.playback.is_midi and self.playback.midi.song:
            return self.playback.midi
        return None

    @property
    def bt_busy(self) -> bool:
        return self._bt_busy

    @property
    def sleep_remaining(self) -> float | None:
        if self._sleep_deadline is None:
            return None
        return max(0.0, self._sleep_deadline - time.monotonic())

    # -- actions the UI calls (must not block) ---------------------------

    def toggle_play(self) -> None:
        if self.playback and self.playback.has_track:
            self.playback.toggle()
            if not self.playback.is_playing:
                self._remember()

    def next_track(self) -> None:
        self._tasks.put(lambda: self._advance(+1))

    def prev_track(self) -> None:
        self._tasks.put(lambda: self._advance(-1))

    def select(self, folder_index: int, track_index: int) -> None:
        def task():
            self.folder_index = folder_index
            self.track_index = track_index
            self._shuffle_key = None
            self._load_current(autoplay=True)
        self._tasks.put(task)

    def in_shuffle_all(self, folder_index: int) -> bool:
        if not 0 <= folder_index < len(self.folders):
            return True
        return self.folders[folder_index].name not in self._scoped("shuffle_excluded")

    def toggle_shuffle_all(self, folder_index: int) -> None:
        if 0 <= folder_index < len(self.folders):
            self._toggle_scoped("shuffle_excluded", self.folders[folder_index].name)

    def is_favorite(self, folder_index: int, track_index: int) -> bool:
        key = self._track_key(folder_index, track_index)
        return key is not None and key in self._scoped("favorites")

    def toggle_favorite(self, folder_index: int, track_index: int) -> None:
        key = self._track_key(folder_index, track_index)
        if key is not None:
            self._toggle_scoped("favorites", key)

    @property
    def current_is_favorite(self) -> bool:
        return self.is_favorite(self.folder_index, self.track_index)

    def toggle_current_favorite(self) -> None:
        self.toggle_favorite(self.folder_index, self.track_index)

    @property
    def favorite_count(self) -> int:
        return len(self._favorite_positions())

    def _track_key(self, folder_index: int, track_index: int) -> str | None:
        if not 0 <= folder_index < len(self.folders):
            return None
        folder = self.folders[folder_index]
        if not 0 <= track_index < len(folder.tracks):
            return None
        return f"{folder.name}/{folder.tracks[track_index].display_name}"

    def play_count(self, folder_index: int, track_index: int) -> int:
        counts = self.settings["play_counts"].get(self._scope(), {})
        key = self._track_key(folder_index, track_index)
        return counts.get(key, 0) if isinstance(counts, dict) and key else 0

    def _start_listening(self) -> None:
        key = self._track_key(self.folder_index, self.track_index)
        with self._listen_lock:
            self._listen = [self._scope(), key, 0.0, False] if key else None

    def _tally_listening(self, seconds: float) -> None:
        with self._listen_lock:
            state = self._listen
            if state is None or state[3]:
                return
            state[2] += min(seconds, 1.0)  # a stalled frame isn't listening
            duration = self.duration
            needed = duration * PLAY_COUNT_FRACTION if duration > 0 else PLAY_COUNT_FALLBACK_S
            if state[2] <= needed:
                return
            state[3] = True
            scope, key = state[0], state[1]
        table = dict(self.settings["play_counts"])
        counts = dict(table.get(scope) or {})
        counts[key] = counts.get(key, 0) + 1
        table[scope] = counts
        self.settings.set_deferred("play_counts", table)
        log.info("Play %d of %s", counts[key], key)

    def _favorite_positions(self) -> list[tuple[int, int]]:
        favorites = self._scoped("favorites")
        return [
            (f, t) for f, folder in enumerate(self.folders)
            for t, track in enumerate(folder.tracks)
            if f"{folder.name}/{track.display_name}" in favorites
        ]

    def _scope(self) -> str:
        # Every floppy has a ROOT, so link sources are keyed by disk label too.
        return self.source if self.source == SOURCE_LOCAL else f"{self.source}:{self.disk_label}"

    def _scoped(self, setting: str) -> set[str]:
        """This source's entries in a {scope: [names]} setting."""
        names = self.settings[setting].get(self._scope(), [])
        return set(names) if isinstance(names, list) else set()

    def _toggle_scoped(self, setting: str, name: str) -> None:
        values = self._scoped(setting) ^ {name}
        table = dict(self.settings[setting])
        if values:
            table[self._scope()] = sorted(values)
        else:
            table.pop(self._scope(), None)
        self.settings[setting] = table
        self._tasks.put(self._prefetch_next)  # the next track may have changed

    def seek_fraction(self, fraction: float) -> None:
        """Jump to a point in the track, 0-1 across the progress bar."""
        if self.playback and self.duration > 0:
            self.playback.seek(max(0.0, min(fraction, 1.0)) * self.duration)

    def set_volume(self, value: float) -> None:
        self._alarm_fade_end = None  # touching the volume ends the alarm fade
        value = max(0.0, min(value, 1.0))
        self.settings.set_deferred("volume", value)
        if self.playback:
            self.playback.set_volume(value)

    def cycle(self, key: str) -> None:
        current = self.settings[key]
        if key == "source":
            self.settings[key] = SOURCES[(SOURCES.index(current) + 1) % len(SOURCES)]
            self._tasks.put(self._reconnect_and_rebuild)
        elif key == "file_types":
            self.settings[key] = TYPES[(TYPES.index(current) + 1) % len(TYPES)]
            self._tasks.put(self._rebuild_playlist)
        elif key == "play_mode":
            self.settings[key] = _next(PLAY_MODES, current)
            self._tasks.put(self._prefetch_next)
        elif key == "loop_repeats":
            nxt = _next(LOOP_CHOICES, current)
            self.settings[key] = nxt
            if self.playback:
                self.playback.loop_repeats = nxt
        elif key == "brightness":
            self.settings[key] = _next(BRIGHTNESS_CHOICES, current)
            self.set_dimmed(False)
        elif key == "dim_after":
            self.settings[key] = _next(DIM_CHOICES, current)
        else:
            self.settings[key] = not current

    # -- mixer -----------------------------------------------------------

    def change_tempo(self, delta: float) -> None:
        if self.midi:
            self.midi.set_tempo(round(self.midi.tempo + delta, 2))

    def change_transpose(self, delta: int) -> None:
        if self.midi:
            self.midi.set_transpose(self.midi.transpose + delta)

    def reset_mixer(self) -> None:
        if self.midi:
            self.midi.reset_mixer()

    def toggle_channel(self, channel: int, solo: bool) -> None:
        if self.midi:
            (self.midi.toggle_solo if solo else self.midi.toggle_mute)(channel)

    # -- EQ --------------------------------------------------------------

    def set_eq_band(self, band: int, db: float) -> None:
        gains = eq.normalise(self.settings["eq_gains"])
        gains[band] = max(-eq.MAX_DB, min(round(db * 2) / 2, eq.MAX_DB))
        self.settings.set_deferred("eq_gains", gains)
        self.settings.set_deferred("eq_preset", "Custom")
        self._schedule_eq()

    def apply_eq_preset(self, name: str) -> None:
        self.settings.set_deferred("eq_gains", list(eq.PRESETS[name]))
        self.settings.set_deferred("eq_preset", name)
        self._schedule_eq()

    def _schedule_eq(self) -> None:
        # Coalesced: one queued update, reading the latest gains when it runs.
        if self._eq_pending:
            return
        self._eq_pending = True

        def task():
            self._eq_pending = False
            self._apply_eq()
        self._eq_tasks.put(task)

    def _apply_eq(self) -> None:
        was_available = self.eq_available
        gains = eq.normalise(self.settings["eq_gains"])
        self.eq_available = self.equalizer.apply(gains)
        if self.playback:
            self.playback.set_headroom_db(eq.headroom_db(gains) if self.eq_available else 0.0)
        if self.eq_available and not was_available:
            self._bt_tasks.put(self._reroute)  # audio only passes the EQ if routed through it

    def _eq_idle(self) -> None:
        # Keep looking: PipeWire can come up after the app, or be restarted.
        if not self.eq_available and time.monotonic() - self._last_eq_check > EQ_RETRY_S:
            self._last_eq_check = time.monotonic()
            self._apply_eq()

    @property
    def eq_problem(self) -> str:
        return self.equalizer.problem

    # -- output ----------------------------------------------------------

    def refresh_bluetooth(self, scan: bool = False) -> None:
        if not self.bt_available or self._bt_busy:
            return
        self._bt_busy = True

        def task():
            try:
                if scan:
                    self.bt_status = "Scanning..."
                    bt.scan(BT_SCAN_S)
                    self.bt_status = ""
                self.bt_devices = bt.devices()
            finally:
                self._bt_busy = False
        self._bt_tasks.put(task)

    def select_output(self, address: str) -> None:
        if self._bt_busy:
            return
        self._bt_busy = True

        def task():
            try:
                self._select_output(address)
            finally:
                self._bt_busy = False
        self._bt_tasks.put(task)

    def _select_output(self, address: str) -> None:
        if address == SPEAKER:
            if not self._route_to(self._speaker_sink()):
                self.bt_status = "Could not switch to the speaker jack"
                return
            self.bt_status = ""
            self.settings["output"] = SPEAKER
            self.settings["output_name"] = "Speaker jack"
            return

        device = next((d for d in self.bt_devices if d.address == address), None)
        name = device.name if device else address
        if self.settings["output"] == address and device and device.connected:
            # Tapping the active speaker releases it.
            bt.disconnect(address)
            self._select_output(SPEAKER)
            self.bt_devices = bt.devices()
            return

        self.bt_status = f"Connecting to {name}..."
        if not bt.connect(address):
            self.bt_status = f"Could not connect to {name}"
            return
        if not self._route_to(self._bt_sink(address)):
            self.bt_status = f"{name} has no audio output"
            return
        self.bt_status = ""
        self.settings["output"] = address
        self.settings["output_name"] = name
        self.bt_devices = bt.devices()

    def _restore_output(self) -> None:
        # At boot the app can start before WirePlumber has created the sinks.
        deadline = time.monotonic() + BOOT_SINK_WAIT_S
        while self._running and self._speaker_sink() is None and time.monotonic() < deadline:
            time.sleep(1.0)
        address = self.settings["output"]
        if address != SPEAKER and self.bt_available:
            name = self.settings["output_name"]
            self.bt_status = f"Connecting to {name}..."
            connected = bt.is_connected(address) or bt.connect(address)
            self.bt_status = ""
            if connected and self._route_to(self._bt_sink(address)):
                return
            log.info("%s not reachable - using the speaker jack for now", name)
        self._route_to(self._speaker_sink())

    def _reroute(self) -> None:
        sink = None
        if self.settings["output"] != SPEAKER:
            prefix = bt.sink_name(self.settings["output"])
            sink = next((s for s in pw.sinks() if s["name"].startswith(prefix)), None)
        self._route_to(sink or self._speaker_sink())

    def _speaker_sink(self) -> dict | None:
        sinks = [s for s in pw.sinks() if s["name"].startswith("alsa_output.")]
        if not sinks:
            return None
        # The Pi also lists HDMI outputs; the headphone jack is the one wanted.
        return min(sinks, key=lambda s: ("hdmi" in (s["name"] + s["description"]).lower(), s["name"]))

    def _bt_sink(self, address: str) -> dict | None:
        # The sink appears a few seconds after the connection does.
        prefix = bt.sink_name(address)
        deadline = time.monotonic() + BT_SINK_WAIT_S
        while self._running:
            sink = next((s for s in pw.sinks() if s["name"].startswith(prefix)), None)
            if sink or time.monotonic() > deadline:
                return sink
            time.sleep(0.5)
        return None

    def _route_to(self, sink: dict | None) -> bool:
        """Send all playback to `sink`, through the EQ when it's installed."""
        if sink is None:
            return False
        nodes = pw.nodes()
        eq_in = pw.find_node(eq.INPUT_NODE, nodes)
        eq_out = pw.find_node(eq.OUTPUT_NODE, nodes)
        if eq_in and eq_out:
            ok = pw.set_default_sink(eq_in["id"]) and pw.set_target(eq_out["id"], sink)
        else:
            ok = pw.set_default_sink(sink["id"])
        log.info("Output: %s%s", sink["description"] or sink["name"], "" if ok else " (failed)")
        return ok

    # -- sleep timer and screen ------------------------------------------

    def cycle_sleep(self) -> None:
        self.sleep_minutes = _next(SLEEP_CHOICES, self.sleep_minutes)
        if self.sleep_minutes:
            self._sleep_deadline = time.monotonic() + self.sleep_minutes * 60
        else:
            self._sleep_deadline = None
        if self.playback:
            self.playback.set_volume(self.settings["volume"])  # undo a fade in progress

    @property
    def alarm_time_text(self) -> str:
        minutes = self.settings["alarm_time"]
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def toggle_alarm(self) -> None:
        self.settings["alarm_enabled"] = not self.settings["alarm_enabled"]
        self._alarm_last = None

    def change_alarm(self, minutes: int) -> None:
        self.settings["alarm_time"] = (self.settings["alarm_time"] + minutes) % (24 * 60)
        self._alarm_last = None

    def _check_alarm(self) -> None:
        if not self.settings["alarm_enabled"]:
            return
        now = time.localtime()
        stamp = (now.tm_yday, now.tm_hour * 60 + now.tm_min)
        if stamp[1] != self.settings["alarm_time"] or stamp == self._alarm_last:
            return
        self._alarm_last = stamp
        self._tasks.put(self._alarm_fire)

    def _alarm_fire(self) -> None:
        """Wake up on a favourite, quietly at first."""
        self.sleep_minutes = 0
        self._sleep_deadline = None
        positions = self._favorite_positions() or [
            (f, t) for f, folder in enumerate(self.folders) for t in range(len(folder.tracks))
        ]
        if not positions or not self.playback:
            self.status_text = "Alarm - nothing to play"
            return
        self.folder_index, self.track_index = random.choice(positions)
        self._shuffle_key = None
        self.status_text = "Alarm"
        self.playback.set_volume(self.settings["volume"] * ALARM_START_LEVEL)
        self._alarm_fade_end = time.monotonic() + ALARM_FADE_S
        self._load_current(autoplay=True)
        if self.ui:
            self.ui.wake()

    def tick(self) -> None:
        """Called every UI frame: play counting, the alarm, and the two volume fades."""
        now = time.monotonic()
        elapsed, self._last_tick = now - self._last_tick, now
        if self.is_playing:
            self._tally_listening(elapsed)
        self._check_alarm()
        if not self.playback:
            return
        if self._alarm_fade_end is not None:
            left = self._alarm_fade_end - time.monotonic()
            volume = self.settings["volume"]
            if left <= 0:
                self._alarm_fade_end = None
                self.playback.set_volume(volume)
            else:
                done = 1 - left / ALARM_FADE_S
                self.playback.set_volume(volume * (ALARM_START_LEVEL + (1 - ALARM_START_LEVEL) * done))
            return
        remaining = self.sleep_remaining
        if remaining is None or remaining > SLEEP_FADE_S:
            return
        volume = self.settings["volume"]
        if remaining > 0:
            self.playback.set_volume(volume * remaining / SLEEP_FADE_S)
            return
        self._sleep_deadline = None
        self.sleep_minutes = 0
        self.playback.pause()
        self.playback.set_volume(volume)
        self._remember()
        self.status_text = "Sleep timer - paused"

    def set_dimmed(self, dimmed: bool) -> None:
        brightness = self.settings["brightness"]
        self.backlight.set(min(brightness, DIM_LEVEL) if dimmed else brightness)

    def _power_off(self, args: list[str], label: str) -> None:
        # -n: fail instead of waiting for a password. Keep running if refused.
        self.status_text = f"{label}..."
        self._remember()
        self.settings.flush()
        try:
            result = subprocess.run(
                ["sudo", "-n", *args], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("Could not run %s: %s", args[0], exc)
            self.status_text = f"{label} failed - see log"
            return
        if result.returncode != 0:
            log.error("%s failed (%d): %s", args[0], result.returncode, result.stderr.strip())
            self.status_text = f"{label} refused - needs passwordless sudo"
            return
        self._running = False
        if self.playback:
            self.playback.stop()
        if self.ui:
            self.ui.stop()

    def shutdown(self) -> None:
        self._power_off(["shutdown", "-h", "now"], "Shutting down")

    def reboot(self) -> None:
        self._power_off(["reboot"], "Rebooting")

    def restart_app(self) -> None:
        """Exit with EXIT_RESTART, which systemd relaunches straight away."""
        self.status_text = "Restarting..."
        self._exit_code = EXIT_RESTART
        self._running = False
        if self.ui:
            self.ui.stop()

    def quit(self) -> None:
        """Exit cleanly (status 0) - systemd leaves it stopped."""
        self._exit_code = 0
        self._running = False
        if self.ui:
            self.ui.stop()

    # -- workers ---------------------------------------------------------

    def _serve(self, tasks: queue.Queue, idle=None) -> None:
        while self._running:
            try:
                task = tasks.get(timeout=0.25)
            except queue.Empty:
                if idle:
                    idle()
                continue
            try:
                task()
            except Exception:
                log.exception("Task failed")
                self.status_text = "Error - see log"

    def _idle_poll(self) -> None:
        """Save state, and watch for a disk swap or an RP2040 reboot."""
        if self.is_playing and time.monotonic() - self._last_remember > RESUME_SAVE_INTERVAL:
            self._remember()
        self.settings.flush()
        if not self.link or not self.needs_link:
            return
        if time.monotonic() - self._last_poll < DISK_POLL_INTERVAL:
            return
        self._last_poll = time.monotonic()
        try:
            if self.link.take_peer_changed():
                self.status_text = "Storage board rebooted - reconnecting"
                self.link.hello()
                self._rebuild_playlist()
                return
            if self.source == SOURCE_FLOPPY and self.link.disk_change_poll():
                self.status_text = "Disk changed - reloading"
                self.link.remount(BACKEND_FLOPPY)
                self._rebuild_playlist()
        except LinkError as exc:
            log.warning("Poll failed: %s", exc)

    def _remember(self) -> None:
        """Note where playback is, for resuming after a restart."""
        track, folder = self.current_track, self.current_folder
        if not self.playback or track is None or folder is None:
            return
        loaded = self.playback.track == track
        self._last_remember = time.monotonic()
        self.settings.set_deferred("resume", {
            "source": self.source,
            "label": self.disk_label,
            "folder": folder.name,
            "track": track.display_name,
            "position": round(self.playback.position, 1) if loaded else 0.0,
        })

    # -- playlist / playback ---------------------------------------------

    def _reconnect_and_rebuild(self) -> None:
        if self.needs_link and self.link is None:
            self._connect()
        self._rebuild_playlist()

    def _rebuild_playlist(self) -> None:
        if self.playback:
            self.playback.stop()
        self.status_text = "Reading..." if self.needs_link else "Scanning music folder..."
        try:
            folders, label = storage.build_playlist(
                self.link,
                self.source,
                self.settings["file_types"],
                Path(self.settings["music_dir"]),
            )
        except LinkError as exc:
            self.folders = []
            self.disk_label = ""
            self.status_text = f"Cannot read: {exc}"
            return

        self.folders = folders
        self.disk_label = label
        self.folder_index = 0
        self.track_index = 0
        self._playlist_gen += 1
        self._shuffle_key = None
        if not folders:
            self.status_text = "Nothing playable found"
            return
        total = sum(len(f.tracks) for f in folders)
        self.status_text = ""
        log.info("Playlist: %d folder(s), %d track(s)", len(folders), total)
        start_at = self._choose_start()
        self._load_current(autoplay=self.settings["autoplay"], start_at=start_at)

    def _choose_start(self) -> float:
        """Pick the first track of a new playlist; returns where to start in it.
        The first playlist after boot resumes the last session."""
        resume, self._resume_pending = self._resume_pending, None
        position = 0.0
        if (
            isinstance(resume, dict)
            and resume.get("source") == self.source
            and resume.get("label", "") == self.disk_label
        ):
            folder = next(
                (i for i, f in enumerate(self.folders) if f.name == resume.get("folder")), None
            )
            if folder is not None:
                self.folder_index = folder
                names = [t.display_name for t in self.folders[folder].tracks]
                if resume.get("track") in names and self.settings["play_mode"] not in SHUFFLE_MODES:
                    self.track_index = names.index(resume["track"])
                    position = float(resume.get("position") or 0.0)
        self._sync_shuffle(random_start=True)
        return position

    def _peek(self, direction: int = 1) -> tuple[int, int] | None:
        """Where _move() would land, without moving the cursor."""
        if not self.folders:
            return None
        if self.settings["play_mode"] in SHUFFLE_MODES:
            self._sync_shuffle()
            return self._shuffle.peek(direction)
        folder_index, track_index = self.folder_index, self.track_index + direction
        folder = self.folders[folder_index]
        if track_index >= len(folder.tracks):
            folder_index = (folder_index + 1) % len(self.folders)
            track_index = 0
        elif track_index < 0:
            folder_index = (folder_index - 1) % len(self.folders)
            track_index = max(0, len(self.folders[folder_index].tracks) - 1)
        return folder_index, track_index

    def _prefetch_next(self) -> None:
        if not self._tasks.empty():
            return  # never make a button press wait behind speculative work
        if self.settings["play_mode"] == "repeat":
            return
        target = self._peek(+1)
        if target is None:
            return
        track = self.folders[target[0]].tracks[target[1]]
        if track.kind != "midi" or self._song_key(target[0], track) in self._songs:
            return
        try:
            # One attempt: three failing floppy reads would block the worker ~30s.
            self._parsed(target[0], track, attempts=1)
        except Exception as exc:
            log.warning("Prefetch of %s failed: %s", track.display_name, exc)

    def _song_key(self, folder_index: int, track: storage.Track) -> tuple:
        # The label tells floppies apart; mtime catches a replaced local file.
        mtime = None
        if track.path is not None:
            try:
                mtime = track.path.stat().st_mtime
            except OSError:
                pass
        return (self.source, self.disk_label, self.folders[folder_index].name, track, mtime)

    def _parsed(self, folder_index: int, track: storage.Track, attempts: int = storage.FETCH_ATTEMPTS):
        key = self._song_key(folder_index, track)
        song = self._songs.get(key)
        if song is not None:
            self._songs.move_to_end(key)
            return song
        song = parse(storage.fetch(self.link, self.source, track, attempts=attempts), track.display_name)
        self._songs[key] = song
        while len(self._songs) > SONG_CACHE_SIZE:
            self._songs.popitem(last=False)
        return song

    def _load_current(self, autoplay: bool, start_at: float = 0.0) -> bool:
        remaining = sum(len(f.tracks) for f in self.folders)
        while remaining > 0:
            remaining -= 1
            track = self.current_track
            if track is None:
                self.status_text = "Nothing to play"
                return False

            cached = self._song_key(self.folder_index, track) in self._songs
            if not cached:
                self.status_text = f"Loading {track.display_name}..."
            started = time.perf_counter()
            try:
                self.playback.loop_repeats = self.settings["loop_repeats"]
                if track.kind == "midi":
                    self.playback.load_song(track, self._parsed(self.folder_index, track))
                else:
                    self.playback.load(track, storage.fetch(self.link, self.source, track))
            except Exception as exc:
                log.warning(
                    "Skipping %s: %s: %s",
                    track.display_name, type(exc).__name__, exc,
                    exc_info=True,
                )
                self.status_text = f"Skipped {track.display_name}"
                if not self.settings["skip_bad_tracks"]:
                    return False
                self._move(+1)
                start_at = 0.0  # the saved position belonged to the skipped track
                continue

            log.info(
                "Loaded %s in %.0fms%s", track.display_name,
                (time.perf_counter() - started) * 1000, " (cached)" if cached else "",
            )
            self._started(track, autoplay, start_at)
            return True

        self.status_text = "No playable tracks"
        return False

    def _started(self, track: storage.Track, autoplay: bool, start_at: float) -> None:
        self.status_text = ""
        self._start_listening()
        if start_at > 0:
            self.playback.seek(start_at)
        if autoplay:
            self.playback.play()
        self._art = (track, decode_art(self.playback.info.get("art")))
        self._remember()
        self._tasks.put(self._prefetch_next)

    def _step(self, direction: int) -> None:
        """Move the cursor one track, wrapping across folders."""
        folder = self.current_folder
        if not folder:
            return
        self.track_index += direction
        if self.track_index >= len(folder.tracks):
            self.folder_index = (self.folder_index + 1) % len(self.folders)
            self.track_index = 0
        elif self.track_index < 0:
            self.folder_index = (self.folder_index - 1) % len(self.folders)
            self.track_index = max(0, len(self.folders[self.folder_index].tracks) - 1)

    def _sync_shuffle(self, random_start: bool = False) -> None:
        """Rebuild the shuffle order when its scope changed."""
        mode = self.settings["play_mode"]
        if mode not in SHUFFLE_MODES or not self.folders:
            self._shuffle_key = None
            return
        excluded = self._scoped("shuffle_excluded")
        positions = None
        if mode == "shuffle_folder":
            key = (mode, self._playlist_gen, self.folder_index)
            count = len(self.folders[self.folder_index].tracks)
            positions = [(self.folder_index, t) for t in range(count)]
        elif mode == "shuffle_favorites":
            favorites = self._scoped("favorites")
            key = (mode, self._playlist_gen, frozenset(favorites), frozenset(excluded))
            positions = self._favorite_positions() or None  # none yet - shuffle all instead
        else:
            key = (mode, self._playlist_gen, frozenset(excluded))
        if not positions:
            positions = [
                (f, t) for f, folder in enumerate(self.folders)
                if folder.name not in excluded
                for t in range(len(folder.tracks))
            ]
        if not positions:  # everything unticked - shuffle it all rather than stop
            positions = [
                (f, t) for f, folder in enumerate(self.folders)
                for t in range(len(folder.tracks))
            ]
        if key == self._shuffle_key:
            return
        first = None if random_start else (self.folder_index, self.track_index)
        self._shuffle.reset(positions, first)
        self._shuffle_key = key
        if random_start and self._shuffle.current:
            self.folder_index, self.track_index = self._shuffle.current

    def _move(self, direction: int) -> None:
        if self.settings["play_mode"] in SHUFFLE_MODES:
            self._sync_shuffle()
            position = self._shuffle.step(direction)
            if position:
                self.folder_index, self.track_index = position
        else:
            self._step(direction)

    def _advance(self, direction: int) -> None:
        """Next obeys the play mode, like a track ending; Prev always moves."""
        if direction > 0 and self.settings["play_mode"] == "repeat":
            self.playback.restart()  # replays from memory, no disk read
            self._start_listening()  # each repeat is a play of its own
            return
        self._move(direction)
        self._load_current(autoplay=True)

    def _on_song_finished(self) -> None:
        # Runs on a playback thread - hand the work to the worker.
        if self.settings["autoplay"] or self.settings["play_mode"] == "repeat":
            self._tasks.put(lambda: self._advance(+1))

    # -- startup ---------------------------------------------------------

    def _connect(self) -> None:
        port = self.settings["serial_port"]
        attempts = 0
        while self._running:
            try:
                self.status_text = f"Connecting to {port}..."
                self.link = LinkClient(port)
                self.link.hello()
                log.info("Link established on %s", port)
                self.status_text = ""
                return
            except (LinkError, OSError) as exc:
                attempts += 1
                if self.link is not None:
                    try:
                        self.link.close()  # or every retry leaks the port
                    except Exception:
                        pass
                    self.link = None
                self.status_text = f"Waiting for storage board ({exc})"
                log.warning("Link not ready: %s", exc)
                # Don't block forever - the local source works without it.
                if attempts >= 5:
                    self.status_text = "No storage board - use Settings to pick Local"
                    return
                time.sleep(2.0)

    def _startup(self) -> None:
        if self.needs_link:
            self._connect()
        if self._running:
            self._rebuild_playlist()

    # -- soundfonts ------------------------------------------------------

    def available_soundfonts(self) -> list[Path]:
        return find_soundfonts(self.settings["soundfont_dirs"])

    def _resolve_soundfont(self) -> str:
        """The active slot's soundfont, else the other slot's, else the first found."""
        slot = self.soundfont_slot
        for candidate in (slot, _other_slot(slot)):
            path = self.settings[f"soundfont_{candidate}"]
            if path and Path(path).is_file():
                self.settings["soundfont_slot"] = candidate
                return path
            if path:
                log.warning("Soundfont %s is missing", path)
        available = self.available_soundfonts()
        if not available:
            raise RuntimeError("no .sf2 found in " + ", ".join(self.settings["soundfont_dirs"]))
        self.settings[f"soundfont_{slot}"] = str(available[0])
        self.settings["soundfont_slot"] = slot
        return str(available[0])

    @property
    def soundfont_slot(self) -> str:
        slot = self.settings["soundfont_slot"]
        return slot if slot in SOUNDFONT_SLOTS else "a"

    def slot_soundfont(self, slot: str) -> str:
        """Full path of the soundfont in a slot, "" if unset."""
        return self.settings[f"soundfont_{slot}"]

    def slot_name(self, slot: str) -> str:
        path = self.slot_soundfont(slot)
        return Path(path).name if path else "not set"

    def assign_soundfont(self, slot: str, path: str) -> None:
        """Put a soundfont in a slot, loading it if that slot is playing."""
        self.settings[f"soundfont_{slot}"] = path
        if slot == self.soundfont_slot:
            self._tasks.put(lambda: self._switch_soundfont(path))

    def use_soundfont_slot(self, slot: str) -> None:
        path = self.slot_soundfont(slot)
        if not path or not Path(path).is_file():
            self.status_text = f"Soundfont {slot.upper()} is not set"
            return
        self.settings["soundfont_slot"] = slot
        self._tasks.put(lambda: self._switch_soundfont(path))

    def swap_soundfont(self) -> None:
        self.use_soundfont_slot(_other_slot(self.soundfont_slot))

    def _switch_soundfont(self, chosen: str) -> None:
        if chosen == self.playback.soundfont:
            return
        self.status_text = f"Loading {Path(chosen).name}..."
        try:
            self.playback.set_soundfont(chosen)
        except Exception as exc:
            log.error("Could not switch soundfont: %s", exc)
            self.status_text = f"Could not load {Path(chosen).name}"
            return
        self.status_text = ""

    def run(self) -> int:
        try:
            soundfont = self._resolve_soundfont()
        except RuntimeError as exc:
            log.error("%s", exc)
            return 1
        s = self.settings
        try:
            self.playback = Playback(
                soundfont, volume=s["volume"], midi_gain=s["midi_gain"],
                audio_gain=s["audio_gain"], driver=s["audio_driver"], rate=s["audio_rate"],
                polyphony=s["synth_polyphony"], reverb=s["synth_reverb"], chorus=s["synth_chorus"],
            )
        except Exception as exc:
            log.error("Could not start the synth with %s: %s", soundfont, exc)
            return 1
        self.playback.on_finished = self._on_song_finished
        self.playback.loop_repeats = self.settings["loop_repeats"]

        for tasks, idle in (
            (self._tasks, self._idle_poll), (self._eq_tasks, self._eq_idle), (self._bt_tasks, None)
        ):
            threading.Thread(target=self._serve, args=(tasks, idle), daemon=True).start()
        self._tasks.put(self._startup)
        self._bt_tasks.put(self._restore_output)
        self.set_dimmed(False)

        self.ui = Ui(self, fullscreen="--windowed" not in sys.argv)
        try:
            self.ui.run()
        finally:
            self._running = False
            self._remember()
            self.settings.flush()
            self.set_dimmed(False)
            self.playback.shutdown()
            if self.link:
                self.link.close()
            self.ui.close()  # last - pygame.quit() takes the mixer with it
        return self._exit_code


def _other_slot(slot: str) -> str:
    return "b" if slot == "a" else "a"


def _next(choices, current):
    """The choice after `current`, wrapping; the first if it isn't one."""
    index = choices.index(current) if current in choices else -1
    return choices[(index + 1) % len(choices)]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return App().run()


if __name__ == "__main__":
    sys.exit(main())
