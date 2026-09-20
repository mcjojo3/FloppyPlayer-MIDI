"""FloppyPlayer: MIDI and audio from a floppy, USB drives or the Pi's music folder.

Link, PipeWire and Bluetooth calls block, so they run on worker threads; the
UI thread only reads state and posts tasks.
"""

from __future__ import annotations

import datetime
import logging
import queue
import random
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

import bt
import eq
import health
import identity
import pw
import spectrum
import stats
import storage
from backlight import Backlight
from link_client import BACKEND_FLOPPY, DISK_EMPTY, DISK_NEW, LinkClient, LinkError
from playback import Playback
from player import parse
from settings import Settings
from shuffle import ShuffleOrder
from soundfont import find_soundfonts, instrument_count
from storage import SOURCE_FLOPPY, SOURCE_USB, SOURCES, TYPES
from ui import Ui, decode_art

log = logging.getLogger("floppyplayer")

PLAY_MODES = ("normal", "shuffle_folder", "shuffle_all", "shuffle_favorites",
              "shuffle_favorites_ticked", "repeat")
SHUFFLE_MODES = ("shuffle_folder", "shuffle_all", "shuffle_favorites", "shuffle_favorites_ticked")
FAVORITE_MODES = ("shuffle_favorites", "shuffle_favorites_ticked")
LOOP_CHOICES = (-1, 0, 1, 2, 3, 4, 6, 8)
POLYPHONY_CHOICES = (8, 16, 32, 64, 128, 256, 512, -1)  # voices; the SD-90 had 128, the SC-88Pro 64
SLEEP_CHOICES = (0, 15, 30, 45, 60, 90, 120)  # minutes
BRIGHTNESS_CHOICES = (1.0, 0.75, 0.5, 0.25)
DIM_CHOICES = (0, 30, 60, 120, 300)  # seconds
# What the screen dims to; 0 writes 0, which most panels take as backlight off.
DIM_LEVEL_CHOICES = (0.01, 0.02, 0.05, 0.08, 0.15, 0.3, 0.0)  # 1% is as dim as is worth offering
SLEEP_FADE_S = 8.0
ALARM_FADE_S = 60.0
ALARM_START_LEVEL = 0.15
PLAY_COUNT_FRACTION = 0.1   # share of a track listened to that counts as a play
PLAY_COUNT_FALLBACK_S = 30  # when the length is unknown
RECENT_LIMIT = 50
MOST_PLAYED_LIMIT = 30
LINK_RETRY_S = 15.0  # auto-switch: how often to look for the storage board
SOUNDFONT_SLOTS = ("a", "b")
ALARM_KINDS = (("weekday", "Mon-Fri"), ("weekend", "Sat-Sun"))
ALARM_ANY = "*"             # alarm_pick: any track, rather than a folder or the favourites
SNOOZE_CHOICES = (0, 5, 9, 10, 15, 20)
BACK_CHOICES = (0, 10, 25, 50, 100)  # tracks of play history kept for stepping back
RESUME_SAVE_INTERVAL = 30.0
SONG_CACHE_SIZE = 8  # parsed MIDIs kept for Prev and re-picks
TRACK_HISTORY = "track"   # history kept by content id: one per track, whatever source it's on
ID_FILL_CHUNK = 40        # files identified per turn on the worker, so button presses don't wait
SPECTRUM_CACHE_SIZE = 32  # audio bars of recent tracks (~100 KB each)
BARS_DELAY_S = 0.1  # the mixer and PipeWire buffers put the sound this far behind the position
DISK_POLL_INTERVAL = 2.0
HEALTH_EVERY_S = 2.0    # how often the Health page's readings are taken
HEALTH_STALE_S = 5.0    # ... and how long after it closes they stop
EQ_RETRY_S = 10.0
BT_SCAN_S = 8
BT_SINK_WAIT_S = 8.0
BT_CHECK_S = 20.0  # how often a missing speaker is looked for
BT_RECONNECT_PAUSE_S = 4.0  # speakers refuse a reconnect straight after a disconnect
BOOT_SINK_WAIT_S = 15.0
BOOT_BT_ATTEMPTS = 2
OUTPUT_WAIT_S = 60.0  # longest the first track waits for the speaker at boot
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
        self._usb_drives: list[Path] = []
        self._usb_seen: set[Path] | None = None  # auto-switch: drives already known
        self._last_link_try = 0.0
        self._floppy_quiet_until = 0.0
        self._disk_missing = False
        # Up-next queue of "folder/file" keys, and where the play mode picks up after it.
        self._queue: list[str] = []
        self._queue_lock = threading.Lock()
        self._return_to: str | None = None
        self._index: tuple = (None, {})  # (folders it was built from, {"folder/file": (f, t)})
        self._last_remember = 0.0
        self._exit_code = 0
        self._songs: OrderedDict = OrderedDict()  # worker thread only
        self._spectra: OrderedDict = OrderedDict()  # Track -> Spectrum, or False if it can't have one
        self._bars_jobs: deque = deque()  # (track, source, duration) waiting for bars, next first
        self._bars_ready = threading.Condition()
        self._bars_worker: threading.Thread | None = None
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
        self._play_log = stats.PlayLog(Path(self.settings.path).parent / "plays.log")
        self._ids = identity.Ids(Path(self.settings.path).parent / "track-ids.json")
        self._ids_version = 0
        self._ref_cache: tuple = (None, -1, {})  # (folders, ids version, {(f, t): history ref})
        self._listened_unsaved = 0.0  # seconds, added to listen_days with the resume point
        self._last_tick = time.monotonic()
        self._eq_pending = False
        self._last_eq_check = 0.0
        self._bt_busy = False
        self._bt_waiting = False  # the chosen speaker isn't there; keep looking
        self._bt_paused = False   # playback we paused when it went, to resume when it's back
        self.health: dict = {}    # the Pi's own readings, while the Health page is open
        self._health_wanted = 0.0
        self._last_health = 0.0
        self._last_bt_check = time.monotonic()
        self._output_ready = threading.Event()  # cleared at boot while the speaker connects
        self._output_ready.set()
        self._sleep_deadline: float | None = None
        self._alarm_fade_end: float | None = None
        self._alarm_last: tuple | None = None  # day and minute the alarm last fired
        self.alarm_ringing = False             # the alarm woke this track: Snooze is offered
        # Where playback has been, oldest first, and what Prev has stepped back past.
        self._played: deque[str] = deque(maxlen=max(BACK_CHOICES))
        self._ahead: list[str] = []
        self._retracing = False                # moving through the history, not adding to it
        self._snooze_until: float | None = None

    # -- state the UI reads ----------------------------------------------

    @property
    def source(self) -> str:
        return self.settings["source"]

    @property
    def needs_link(self) -> bool:
        return self.source == SOURCE_FLOPPY

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
    def lyrics(self):
        return self._loaded_info.get("lyrics")

    @property
    def lyric_line(self) -> int | None:
        lyrics = self.lyrics
        return lyrics.line_at(self.position, self.duration) if lyrics else None

    def toggle_lyrics(self) -> None:
        if self.lyrics:
            self.settings["show_lyrics"] = not self.settings["show_lyrics"]

    @property
    def channel_levels(self) -> list[float] | None:
        midi = self.midi
        return midi.levels() if midi else None

    @property
    def audio_bars(self) -> list[float] | None:
        """Frequency bars for the playing audio file, once worked out; None until then."""
        if self.midi or not self.playback or not self.settings["show_audio_bars"]:
            return None
        track = self.playback.track
        if track is None or track.kind != "audio" or track != self.current_track:
            return None
        found = self._spectra.get(track)
        if found is None:
            self._want_bars(track, self.playback.audio.source, self.duration, first=True)
            return None
        if found is False:
            return None
        return found.levels(self.position - BARS_DELAY_S) if self.is_playing else [0.0] * spectrum.BANDS

    @property
    def bars_unavailable(self) -> bool:
        """The playing file can't have bars: too long, unreadable, or numpy isn't installed."""
        track = self.playback.track if self.playback else None
        return track is not None and self._spectra.get(track) is False

    @property
    def visual_levels(self) -> list[float] | None:
        """Whatever bars the Playing screen shows: MIDI channels, or an audio file's frequencies."""
        return self.channel_levels if self.midi else self.audio_bars

    def toggle_cover_view(self) -> None:
        self.settings["show_audio_bars"] = not self.settings["show_audio_bars"]

    def _want_bars(self, track: storage.Track, source, duration: float, first: bool) -> None:
        """Queue a file for bars: the playing one goes first, a prefetched next one after."""
        with self._bars_ready:
            if track in self._spectra:
                return
            queued = next((job for job in self._bars_jobs if job[0] == track), None)
            if queued is not None:
                if not first:
                    return
                self._bars_jobs.remove(queued)
            job = (track, source, duration)
            self._bars_jobs.appendleft(job) if first else self._bars_jobs.append(job)
            if self._bars_worker is None:
                self._bars_worker = threading.Thread(target=self._work_out_bars, daemon=True)
                self._bars_worker.start()
            self._bars_ready.notify()

    def _work_out_bars(self) -> None:
        """One file at a time, so a prefetch never slows the playing file's bars down."""
        cache = Path(self.settings.path).parent / "bars"
        while self._running:
            with self._bars_ready:
                if not self._bars_jobs:
                    self._bars_ready.wait(1.0)
                    continue
                track, source, duration = self._bars_jobs[0]
            try:
                found = spectrum.load_or_analyse(source, duration, cache)
            except Exception:
                log.exception("Audio bars failed for %s", track.display_name)
                found = None
            with self._bars_ready:
                self._spectra[track] = found or False
                while len(self._spectra) > SPECTRUM_CACHE_SIZE:
                    self._spectra.popitem(last=False)
                self._bars_jobs = deque(job for job in self._bars_jobs if job[0] != track)

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
            self.alarm_ringing = False
            self._bt_paused = False  # whatever the speaker does now, this was the user's choice
            self.playback.toggle()
            if not self.playback.is_playing:
                self._remember()

    def next_track(self) -> None:
        self.alarm_ringing = False
        self._tasks.put(lambda: self._advance(+1))

    def prev_track(self) -> None:
        self.alarm_ringing = False
        self._tasks.put(lambda: self._advance(-1))

    def select(self, folder_index: int, track_index: int) -> None:
        def task():
            self.folder_index = folder_index
            self.track_index = track_index
            self._shuffle_key = None
            self._return_to = None  # the play mode carries on from here
            self._load_current(autoplay=True)
        self._tasks.put(task)

    # -- queue -----------------------------------------------------------

    def _position_of(self, key: str | None) -> tuple[int, int] | None:
        folders, index = self._index
        if folders is not self.folders:
            index = {
                f"{folder.name}/{track.display_name}": (f, t)
                for f, folder in enumerate(self.folders) for t, track in enumerate(folder.tracks)
            }
            self._index = (self.folders, index)
        return index.get(key) if key else None

    @property
    def queue(self) -> list[tuple[int, int]]:
        """Queued tracks still in the playlist, in play order."""
        with self._queue_lock:
            keys = list(self._queue)
        return [p for p in map(self._position_of, keys) if p is not None]

    def queue_position(self, folder_index: int, track_index: int) -> int:
        """1-based place in the queue; 0 if not queued."""
        key = self._track_key(folder_index, track_index)
        with self._queue_lock:
            return self._queue.index(key) + 1 if key in self._queue else 0

    def toggle_queued(self, folder_index: int, track_index: int) -> None:
        key = self._track_key(folder_index, track_index)
        if key is None:
            return
        with self._queue_lock:
            if key in self._queue:
                self._queue.remove(key)
            else:
                self._queue.append(key)
        self._tasks.put(self._prefetch_next)

    def move_queued(self, folder_index: int, track_index: int, delta: int) -> None:
        """Shuffle a queued track up or down the queue by one place."""
        key = self._track_key(folder_index, track_index)
        with self._queue_lock:
            if key not in self._queue:
                return
            at = self._queue.index(key)
            to = at + delta
            if not 0 <= to < len(self._queue):
                return
            self._queue[at], self._queue[to] = self._queue[to], self._queue[at]
        self._tasks.put(self._prefetch_next)

    def queue_all(self, positions) -> None:
        with self._queue_lock:
            for folder_index, track_index in positions:
                key = self._track_key(folder_index, track_index)
                if key and key not in self._queue:
                    self._queue.append(key)
        self._tasks.put(self._prefetch_next)

    def clear_queue(self) -> None:
        with self._queue_lock:
            self._queue.clear()

    def play_queued(self, folder_index: int, track_index: int) -> None:
        """Play a queued track now; the rest of the queue follows it."""
        key = self._track_key(folder_index, track_index)

        def task():
            with self._queue_lock:
                if key in self._queue:
                    self._queue.remove(key)
            if self._enter_queue(key):
                self._load_current(autoplay=True)
        self._tasks.put(task)

    def _enter_queue(self, key: str | None) -> bool:
        """Move the cursor to a queued track, remembering where the play mode was."""
        position = self._position_of(key)
        if position is None:
            return False
        if self._return_to is None:
            self._return_to = self._track_key(self.folder_index, self.track_index)
        self.folder_index, self.track_index = position
        return True

    def _queue_next(self) -> bool:
        """Cursor onto the next queued track still in the playlist; False if there's none."""
        while True:
            with self._queue_lock:
                if not self._queue:
                    return False
                key = self._queue.pop(0)
            if self._enter_queue(key):
                return True

    def _leave_queue(self) -> bool:
        """Cursor back to where the queue cut in, so the play mode carries on from there."""
        key, self._return_to = self._return_to, None
        position = self._position_of(key)
        if position is not None:
            self.folder_index, self.track_index = position
        return position is not None

    # -- history ---------------------------------------------------------

    def _by_history(self) -> dict:
        """(bucket, key) -> position, for finding the tracks a history entry refers to."""
        return {ref: position for position, ref in self._refs().items()}

    def most_played(self) -> list[tuple[int, int]]:
        where = self._by_history()
        order = self._recent_order()  # equal counts: the one played most recently first
        ranked: list[tuple[int, int, tuple]] = []
        for bucket in (TRACK_HISTORY, self._scope()):
            counts = self.settings["play_counts"].get(bucket, {})
            if isinstance(counts, dict):
                ranked += [(-n, order.get((bucket, key), len(order)), (bucket, key))
                           for key, n in counts.items() if n > 0]
        found = [where[ref] for _, _, ref in sorted(ranked) if ref in where]
        return list(dict.fromkeys(found))[:MOST_PLAYED_LIMIT]

    def _recent_order(self) -> dict:
        """(bucket, key) -> how far back it was played, newest first."""
        seen: dict = {}
        for bucket in (TRACK_HISTORY, self._scope()):
            keys = self.settings["recent"].get(bucket, [])
            for key in (keys if isinstance(keys, list) else []):
                seen.setdefault((bucket, key), len(seen))
        return seen

    def recently_played(self) -> list[tuple[int, int]]:
        where = self._by_history()
        found = []
        for bucket in (TRACK_HISTORY, self._scope()):
            keys = self.settings["recent"].get(bucket, [])
            found += [where[(bucket, key)] for key in (keys if isinstance(keys, list) else [])
                      if (bucket, key) in where]
        return list(dict.fromkeys(found))

    def in_shuffle_all(self, folder_index: int) -> bool:
        if not 0 <= folder_index < len(self.folders):
            return True
        return self.folders[folder_index].name not in self._scoped("shuffle_excluded")

    def toggle_shuffle_all(self, folder_index: int) -> None:
        if 0 <= folder_index < len(self.folders):
            self._toggle_scoped("shuffle_excluded", self.folders[folder_index].name)

    def is_favorite(self, folder_index: int, track_index: int) -> bool:
        ref = self._history_ref(folder_index, track_index)
        return ref is not None and ref[1] in self._bucket(ref[0], "favorites")

    def toggle_favorite(self, folder_index: int, track_index: int) -> None:
        ref = self._history_ref(folder_index, track_index)
        if ref is not None:
            self._toggle_in_bucket("favorites", ref)

    @property
    def current_is_favorite(self) -> bool:
        return self.is_favorite(self.folder_index, self.track_index)

    def toggle_current_favorite(self) -> None:
        self.toggle_favorite(self.folder_index, self.track_index)

    @property
    def favorite_count(self) -> int:
        return len(self._favorite_positions())

    @property
    def favorites_in_mode(self) -> int:
        """How many hearts the favourites shuffle has to pick from, in the mode that's set."""
        return len(self._favorite_positions(self.settings["play_mode"] == "shuffle_favorites_ticked"))

    def _track_key(self, folder_index: int, track_index: int) -> str | None:
        """ "folder/file" - where a track sits, used for the queue and as a history fallback."""
        if not 0 <= folder_index < len(self.folders):
            return None
        folder = self.folders[folder_index]
        if not 0 <= track_index < len(folder.tracks):
            return None
        return f"{folder.name}/{folder.tracks[track_index].display_name}"

    def _id_key(self, folder_index: int, track_index: int) -> str | None:
        """How the id cache keys this track: its path, or the disk and name on a floppy."""
        name = self._track_key(folder_index, track_index)
        if name is None:
            return None
        track = self.folders[folder_index].tracks[track_index]
        return str(track.path) if track.path is not None else f"{self._scope()}/{name}"

    def _history_ref(self, folder_index: int, track_index: int) -> tuple[str, str] | None:
        """Where this track's history lives: (TRACK_HISTORY, content id) once the file has been
        identified - the same everywhere a copy of it turns up - else (source, "folder/file")."""
        return self._refs().get((folder_index, track_index))

    def _refs(self) -> dict:
        # One pass per playlist, since each path track costs a stat() to check the id is current.
        if self._ref_cache[0] is not self.folders or self._ref_cache[1] != self._ids_version:
            refs = {}
            for f, folder in enumerate(self.folders):
                for t, track in enumerate(folder.tracks):
                    name = f"{folder.name}/{track.display_name}"
                    key = str(track.path) if track.path is not None else f"{self._scope()}/{name}"
                    found = self._ids.known(key, track.path)
                    refs[(f, t)] = (TRACK_HISTORY, found) if found else (self._scope(), name)
            self._ref_cache = (self.folders, self._ids_version, refs)
        return self._ref_cache[2]

    def _bucket(self, bucket: str, setting: str) -> set[str]:
        values = self.settings[setting].get(bucket, [])
        return set(values) if isinstance(values, list) else set()

    def _toggle_in_bucket(self, setting: str, ref: tuple[str, str]) -> None:
        bucket, key = ref
        values = self._bucket(bucket, setting) ^ {key}
        table = dict(self.settings[setting])
        if values:
            table[bucket] = sorted(values)
        else:
            table.pop(bucket, None)
        self.settings[setting] = table
        self._tasks.put(self._prefetch_next)  # the next track may have changed

    def play_count(self, folder_index: int, track_index: int) -> int:
        ref = self._history_ref(folder_index, track_index)
        if ref is None:
            return 0
        counts = self.settings["play_counts"].get(ref[0], {})
        return counts.get(ref[1], 0) if isinstance(counts, dict) else 0

    def _start_listening(self) -> None:
        ref = self._history_ref(self.folder_index, self.track_index)
        with self._listen_lock:
            self._listen = [ref[0], ref[1], 0.0, False] if ref else None

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
            bucket, key = state[0], state[1]
        table = dict(self.settings["play_counts"])
        counts = dict(table.get(bucket) or {})
        counts[key] = counts.get(key, 0) + 1
        table[bucket] = counts
        self.settings.set_deferred("play_counts", table)
        recent = dict(self.settings["recent"])
        earlier = [k for k in (recent.get(bucket) or []) if k != key]
        recent[bucket] = [key] + earlier[: RECENT_LIMIT - 1]
        self.settings.set_deferred("recent", recent)
        self._play_log.add(bucket, key)
        log.info("Play %d of %s", counts[key], key)

    # -- track identity ----------------------------------------------------

    @property
    def ids_version(self) -> int:
        """Bumped whenever more tracks are identified, so the screen picks their history up."""
        return self._ids_version

    def _learn_id(self, folder_index: int, track_index: int, data: bytes | None = None) -> None:
        """Identify a track by its contents and move any history kept under its name onto it."""
        key, name = self._id_key(folder_index, track_index), self._track_key(folder_index, track_index)
        if key is None or self._ids.known(key, self.folders[folder_index].tracks[track_index].path):
            return
        track = self.folders[folder_index].tracks[track_index]
        if data:
            content = identity.id_of_bytes(data)
            self._ids.remember(key, content, name, track.path)
        elif track.path is not None:
            content = self._ids.of_file(key, track.path, name)
        else:
            return  # a floppy track is identified from the bytes when it's read
        if content:
            self._ids_version += 1
            self._merge_history((self._scope(), name), (TRACK_HISTORY, content))

    def _merge_history(self, old: tuple[str, str], new: tuple[str, str]) -> None:
        """Fold history kept under a file name into the track's own, so a rename keeps it."""
        counts = self.settings["play_counts"]
        old_count = (counts.get(old[0]) or {}).get(old[1], 0)
        if old_count:
            table = {bucket: dict(entries) for bucket, entries in counts.items()}
            entries = table.setdefault(new[0], {})
            entries[new[1]] = entries.get(new[1], 0) + old_count
            table[old[0]].pop(old[1], None)
            if not table[old[0]]:
                table.pop(old[0])  # the name-keyed bucket empties out as tracks are identified
            self.settings.set_deferred("play_counts", table)
        if old[1] in self._bucket(old[0], "favorites"):
            self._toggle_in_bucket("favorites", old)  # off the name
            if new[1] not in self._bucket(new[0], "favorites"):
                self._toggle_in_bucket("favorites", new)  # onto the track
        recent = self.settings["recent"]
        if old[1] in (recent.get(old[0]) or []):
            table = {bucket: list(keys) for bucket, keys in recent.items()}
            table[old[0]] = [k for k in table[old[0]] if k != old[1]]
            if not table[old[0]]:
                table.pop(old[0])
            table.setdefault(new[0], [])
            if new[1] not in table[new[0]]:
                table[new[0]] = [new[1]] + table[new[0]][: RECENT_LIMIT - 1]
            self.settings.set_deferred("recent", table)

    def _fill_ids(self) -> None:
        """Identify the playlist's files in the background, a chunk at a time, so Browse shows
        the right hearts and counts even for tracks that haven't been played on this name."""
        generation = self._playlist_gen
        done = 0
        for f, folder in enumerate(self.folders):
            for t, track in enumerate(folder.tracks):
                if self._playlist_gen != generation or not self._running:
                    return
                if track.path is None:
                    continue  # floppies are identified when read, not by re-reading the disk
                key = str(track.path)
                if self._ids.known(key, track.path):
                    continue
                self._learn_id(f, t)
                done += 1
                if done >= ID_FILL_CHUNK and not self._tasks.empty():
                    self._tasks.put(self._fill_ids)  # let real work through, then carry on
                    self._ids.save()
                    return
        self._ids.save()

    def _save_listening(self) -> None:
        """Add the listening time so far to today's total."""
        with self._listen_lock:
            seconds, self._listened_unsaved = self._listened_unsaved, 0.0
        if seconds <= 0:
            return
        days = dict(self.settings["listen_days"])
        today = datetime.date.today().isoformat()
        days[today] = round(days.get(today, 0.0) + seconds, 1)
        self.settings.set_deferred("listen_days", days)

    def history_name(self, bucket: str, key: str) -> str:
        """ "folder/file" for a history entry, even for a track that isn't in the playlist."""
        return (self._ids.name_of(key) or key) if bucket == TRACK_HISTORY else key

    def stats(self, days: int | None) -> dict:
        """Listening time, plays and top tracks for the Stats page; days=None is all time."""
        self._save_listening()
        return stats.summary(self._play_log, self.settings["play_counts"],
                             self.settings["listen_days"], days)

    def _favorite_positions(self, ticked_only: bool = False) -> list[tuple[int, int]]:
        """Hearted tracks; ticked_only leaves out the folders unticked for Shuffle all."""
        by_id = self._bucket(TRACK_HISTORY, "favorites")
        by_name = self._bucket(self._scope(), "favorites")
        excluded = self._scoped("shuffle_excluded") if ticked_only else set()
        return [position for position, (bucket, key) in self._refs().items()
                if key in (by_id if bucket == TRACK_HISTORY else by_name)
                and self.folders[position[0]].name not in excluded]

    def _scope(self) -> str:
        # Every floppy has a ROOT, so floppies are keyed by label; USB folder names carry the drive's.
        return f"{self.source}:{self.disk_label}" if self.source == SOURCE_FLOPPY else self.source

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
        elif key == "synth_polyphony":
            nxt = _next(POLYPHONY_CHOICES, current)
            self.settings[key] = nxt
            if self.playback:
                self.playback.midi.set_polyphony(nxt)
        elif key == "brightness":
            self.settings[key] = _next(BRIGHTNESS_CHOICES, current)
            self.set_dimmed(False)
        elif key == "dim_after":
            self.settings[key] = _next(DIM_CHOICES, current)
        elif key == "dim_level":
            self.settings[key] = _next(DIM_LEVEL_CHOICES, current)
            self.set_dimmed(self.ui._dimmed if self.ui else False)  # show it straight away
        elif key == "loudness_match":
            self.settings[key] = not current
            if self.playback:
                self.playback.set_loudness_match(not current)
        elif key == "back_limit":
            limit = _next(BACK_CHOICES, current)
            self.settings[key] = limit
            while len(self._played) > limit:
                self._played.popleft()
            if not limit:
                self._ahead.clear()
        elif key == "alarm_snooze":
            self.settings[key] = _next(SNOOZE_CHOICES, current)
        elif key == "auto_source":
            self.settings[key] = not current
            self._usb_seen = None  # drives already plugged in don't count as new
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
            self.playback.set_eq_boost_db(eq.max_boost_db(gains) if self.eq_available else 0.0)
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
            self._bt_waiting = False
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
        if not self._route_to(self._bt_audio_sink(address)):
            connected = bt.is_connected(address)
            self.bt_status = f"{name} has no audio output" if connected else f"Could not connect to {name}"
            return
        self.bt_status = ""
        self._bt_waiting = False
        self.settings["output"] = address
        self.settings["output_name"] = name
        self.bt_devices = bt.devices()

    def _restore_output(self) -> None:
        try:
            # At boot the app can start before WirePlumber has created the sinks.
            deadline = time.monotonic() + BOOT_SINK_WAIT_S
            while self._running and self._speaker_sink() is None and time.monotonic() < deadline:
                time.sleep(1.0)
            address = self.settings["output"]
            if address != SPEAKER and self.bt_available:
                name = self.settings["output_name"]
                self.bt_status = f"Connecting to {name}..."
                for attempt in range(BOOT_BT_ATTEMPTS):
                    if attempt:
                        time.sleep(BT_RECONNECT_PAUSE_S)
                    if self._route_to(self._bt_audio_sink(address)):
                        self.bt_status = ""
                        return
                self.bt_status = f"{name} unavailable - playing on the speaker jack"
                self._bt_waiting = True
                log.info("%s not reachable - using the speaker jack until it is", name)
            self._route_to(self._speaker_sink())
        finally:
            self._output_ready.set()

    def _bt_idle(self) -> None:
        """Switch to the chosen speaker when it turns up, and to the jack when it goes."""
        address = self.settings["output"]
        if address == SPEAKER or not self.bt_available or self._bt_busy:
            return
        if time.monotonic() - self._last_bt_check < BT_CHECK_S:
            return
        self._last_bt_check = time.monotonic()
        name = self.settings["output_name"]
        if self._bt_waiting:
            back = bt.is_connected(address) or bt.reconnect(address)
            if back and self._route_to(self._bt_audio_sink(address)):
                self._bt_waiting = False
                self.bt_status = ""
                log.info("%s is back", name)
                if self._bt_paused:  # carry on where the speaker cut out
                    self._bt_paused = False
                    self.playback.play()
            return
        nodes = pw.nodes()  # empty when PipeWire itself is down - not the speaker's fault
        prefix = bt.sink_name(address)
        if nodes and not any(s["name"].startswith(prefix) for s in pw.sinks(nodes)):
            self._bt_waiting = True
            # Pause rather than carry on out of the jack, which nobody is listening to.
            if self.playback and self.playback.is_playing:
                self.playback.pause()
                self._bt_paused = True
            self.bt_status = f"{name} disconnected" + (" - paused" if self._bt_paused else "")
            log.info("%s went away%s", name, " - paused" if self._bt_paused else "")
            self._route_to(self._speaker_sink())

    def _reroute(self) -> None:
        sink = None
        if self.settings["output"] != SPEAKER:
            sink = self._bt_sink(self.settings["output"], wait=0)
        self._route_to(sink or self._speaker_sink())

    def _speaker_sink(self) -> dict | None:
        sinks = [s for s in pw.sinks() if s["name"].startswith("alsa_output.")]
        if not sinks:
            return None
        # The Pi also lists HDMI outputs; the headphone jack is the one wanted.
        return min(sinks, key=lambda s: ("hdmi" in (s["name"] + s["description"]).lower(), s["name"]))

    def _bt_audio_sink(self, address: str) -> dict | None:
        """Connect a Bluetooth speaker and return its PipeWire sink."""
        if not (bt.is_connected(address) or bt.connect(address)):
            return None
        sink = self._bt_sink(address)
        if sink is None:
            # It reconnected at boot before PipeWire could take audio. Ask for audio on the
            # existing link first; dropping it can leave the speaker refusing for a while.
            log.info("%s is connected without audio - asking for it", address)
            bt.reconnect(address)
            sink = self._bt_sink(address)
        if sink is None:
            log.info("%s still has no audio - reconnecting", address)
            bt.disconnect(address)
            time.sleep(BT_RECONNECT_PAUSE_S)
            if bt.connect(address):
                sink = self._bt_sink(address)
        return sink

    def _bt_sink(self, address: str, wait: float | None = None) -> dict | None:
        # The sink appears a few seconds after the connection does.
        prefix = bt.sink_name(address)
        deadline = time.monotonic() + (BT_SINK_WAIT_S if wait is None else wait)
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
        if sink["name"].startswith(bt.SINK_PREFIX):
            pw.set_volume(sink["id"], 1.0)  # speakers join at 40%; the app's slider sets the level
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

    def alarm_enabled(self, kind: str) -> bool:
        return self.settings[_alarm_keys(kind)[0]]

    def alarm_minutes(self, kind: str) -> int:
        return self.settings[_alarm_keys(kind)[1]]

    def alarm_text(self, kind: str) -> str:
        minutes = self.alarm_minutes(kind)
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    @property
    def alarm_summary(self) -> str:
        """For the Settings row: "07:30 Mon-Fri, 09:00 Sat-Sun", or Off."""
        parts = [f"{self.alarm_text(kind)} {days}" for kind, days in ALARM_KINDS
                 if self.alarm_enabled(kind)]
        return ", ".join(parts) or "Off"

    @property
    def next_alarm_text(self) -> str | None:
        """The next alarm, for the header: "07:30", or "Sat 09:00" when it's more than a day off."""
        now = datetime.datetime.now()
        minute_now = now.hour * 60 + now.minute
        for days in range(8):
            day = now + datetime.timedelta(days=days)
            kind = _alarm_kind(day.weekday())
            minutes = self.alarm_minutes(kind)
            if not self.alarm_enabled(kind) or (days == 0 and minutes <= minute_now):
                continue
            if days == 0 or (days == 1 and minutes <= minute_now):
                return self.alarm_text(kind)
            return f"{day.strftime('%a')} {self.alarm_text(kind)}"
        return None

    def toggle_alarm(self, kind: str) -> None:
        key = _alarm_keys(kind)[0]
        self.settings[key] = not self.settings[key]
        self._alarm_last = None

    def set_alarm_volume(self, value: float) -> None:
        self.settings.set_deferred("alarm_volume", max(0.0, min(value, 1.0)))

    def change_alarm(self, minutes: int, kind: str) -> None:
        key = _alarm_keys(kind)[1]
        self.settings[key] = (self.settings[key] + minutes) % (24 * 60)
        self._alarm_last = None

    def _check_alarm(self) -> None:
        if self._snooze_until is not None and time.monotonic() >= self._snooze_until:
            self._snooze_until = None
            self._tasks.put(self._alarm_fire)
            return
        now = time.localtime()
        kind = _alarm_kind(now.tm_wday)
        if not self.alarm_enabled(kind):
            return
        stamp = (now.tm_yday, now.tm_hour * 60 + now.tm_min)
        if stamp[1] != self.alarm_minutes(kind) or stamp == self._alarm_last:
            return
        self._alarm_last = stamp
        self._tasks.put(self._alarm_fire)

    def _everything(self) -> list[tuple[int, int]]:
        return [(f, t) for f, folder in enumerate(self.folders) for t in range(len(folder.tracks))]

    def _alarm_positions(self) -> list[tuple[int, int]]:
        """What the alarm may wake you on: a chosen folder, anything, or your favourites."""
        pick = self.settings["alarm_pick"]
        if pick == ALARM_ANY:
            return self._everything()
        if pick:
            folder = next((i for i, f in enumerate(self.folders) if f.name == pick), None)
            if folder is not None and self.folders[folder].tracks:
                return [(folder, t) for t in range(len(self.folders[folder].tracks))]
        return self._favorite_positions() or self._everything()

    @property
    def alarm_pick_label(self) -> str:
        pick = self.settings["alarm_pick"]
        if pick == ALARM_ANY:
            return "Anything"
        if not pick:
            return "Favorites"
        here = any(f.name == pick for f in self.folders)
        return pick if here else f"{pick} (not on this source)"

    def alarm_pick_options(self) -> list[tuple[str, str]]:
        """What the Wake to page offers: favourites, anything, then this source's folders."""
        return ([("", "Favorites"), (ALARM_ANY, "Anything")]
                + [(f.name, f.name) for f in self.folders])

    def set_alarm_pick(self, pick: str) -> None:
        self.settings["alarm_pick"] = pick

    @property
    def snooze_minutes(self) -> int:
        return self.settings["alarm_snooze"]

    def snooze(self) -> None:
        """Quiet until the alarm comes back, a few minutes from now."""
        minutes = self.snooze_minutes
        self.alarm_ringing = False
        if not minutes:
            return
        self._alarm_fade_end = None
        if self.playback:
            self.playback.pause()
            self.playback.set_volume(self.settings["volume"])
        self._snooze_until = time.monotonic() + minutes * 60
        self.status_text = f"Snoozed {minutes} min"
        self._remember()

    def _alarm_fire(self) -> None:
        """Wake up on a favourite, quietly at first."""
        self.sleep_minutes = 0
        self._sleep_deadline = None
        self._snooze_until = None
        positions = self._alarm_positions()
        if not positions or not self.playback:
            self.status_text = "Alarm - nothing to play"
            return
        self.folder_index, self.track_index = random.choice(positions)
        self._shuffle_key = None
        self._return_to = None
        self.status_text = "Alarm"
        self.alarm_ringing = True
        # The alarm's own volume becomes the volume, so the slider shows what's playing.
        self.settings.set_deferred("volume", self.settings["alarm_volume"])
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
            with self._listen_lock:
                self._listened_unsaved += min(elapsed, 1.0)
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

    @property
    def dim_level_label(self) -> str:
        level = self.settings["dim_level"]
        return f"{round(level * 100)}%" if level > 0 else "Off - backlight off"

    def set_dimmed(self, dimmed: bool) -> None:
        brightness = self.settings["brightness"]
        self.backlight.set(min(brightness, self.settings["dim_level"]) if dimmed else brightness)

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
                    try:
                        idle()
                    except Exception:
                        log.exception("Idle check failed")
                        time.sleep(5.0)  # don't spin on a lasting fault
                continue
            try:
                task()
            except Exception:
                log.exception("Task failed")
                self.status_text = "Error - see log"

    def want_health(self) -> None:
        """The Health page is open: the worker keeps the readings fresh while it's up."""
        self._health_wanted = time.monotonic()

    def _poll_health(self) -> None:
        now = time.monotonic()
        if now - self._health_wanted > HEALTH_STALE_S or now - self._last_health < HEALTH_EVERY_S:
            return
        self._last_health = now
        self.health = health.snapshot(self.settings["music_dir"])

    def _idle_poll(self) -> None:
        """Save state, and watch for disks and drives coming and going or an RP2040 reboot."""
        self._poll_health()
        if self.is_playing and time.monotonic() - self._last_remember > RESUME_SAVE_INTERVAL:
            self._remember()
        self.settings.flush()
        if time.monotonic() - self._last_poll < DISK_POLL_INTERVAL:
            return
        self._last_poll = time.monotonic()
        if self.settings["auto_source"] and self._switch_to_new_source():
            return
        if self.source == SOURCE_USB:
            if storage.usb_drives() != self._usb_drives:
                self._rebuild_playlist(keep_current=True)
            return
        if not self.link or not self.needs_link:
            return
        try:
            if self.link.take_peer_changed():
                self.status_text = "Storage board rebooted - reconnecting"
                self.link.hello()
                self._rebuild_playlist()
                return
            state = self.link.disk_state()
            if state == DISK_NEW:
                self._disk_missing = False
                self.status_text = "Disk changed - reloading"
                self.link.remount(BACKEND_FLOPPY)
                self._rebuild_playlist()
            elif state == DISK_EMPTY and not self._disk_missing:
                self._disk_missing = True
                self.status_text = "No disk in the drive"
        except LinkError as exc:
            log.warning("Poll failed: %s", exc)

    def _switch_to_new_source(self) -> bool:
        """Auto-switch: to a USB drive with music just plugged in, or a floppy just put in."""
        drives = set(storage.usb_drives())
        seen, self._usb_seen = self._usb_seen, drives
        new = drives - seen if seen is not None else set()
        types = self.settings["file_types"]
        if self.source != SOURCE_USB and any(storage.has_music(d, types) for d in sorted(new)):
            log.info("USB drive plugged in - switching to it")
            return self._switch_source(SOURCE_USB)
        if self.source != SOURCE_FLOPPY and self._floppy_inserted():
            log.info("Floppy put in - switching to it")
            return self._switch_source(SOURCE_FLOPPY)
        return False

    def _switch_source(self, source: str) -> bool:
        self.settings["source"] = source
        self._rebuild_playlist()
        return True

    def _floppy_inserted(self) -> bool:
        """A new disk in the drive, ready to read. Needs the board, and DSKCHG wired."""
        now = time.monotonic()
        if now < self._floppy_quiet_until:
            return False
        if self.link is None:
            if now - self._last_link_try < LINK_RETRY_S:
                return False
            self._last_link_try = now
            link = None
            try:
                link = LinkClient(self.settings["serial_port"])
                link.hello()
            except (LinkError, OSError) as exc:
                log.debug("No storage board for auto-switch: %s", exc)
                if link is not None:
                    link.close()
                return False
            self.link = link
        try:
            if self.link.take_peer_changed():
                self.link.hello()
            if self.link.disk_state() != DISK_NEW:
                return False
            if self.link.remount(BACKEND_FLOPPY):
                return True
        except LinkError as exc:
            log.warning("Floppy check failed: %s", exc)
        self._floppy_quiet_until = now + 30.0  # an unreadable disk: don't retry every poll
        return False

    def _remember(self) -> None:
        """Note where playback is, for resuming after a restart - and the listening time."""
        self._save_listening()
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
            "played": list(self._played),
        })

    # -- playlist / playback ---------------------------------------------

    def _reconnect_and_rebuild(self) -> None:
        if self.needs_link and self.link is None:
            self._connect()
        self._rebuild_playlist()

    def _rebuild_playlist(self, keep_current: bool = False) -> None:
        """Rescan the source; keep_current leaves the loaded track playing if it's still there."""
        folder, track = self.current_folder, self.current_track
        keep = keep_current and track is not None and self.playback is not None and self.playback.track == track
        if self.playback and not keep:
            self.playback.stop()
        if self.source == SOURCE_USB:
            self._usb_drives = storage.usb_drives()
        if not keep:
            self.status_text = {
                SOURCE_FLOPPY: "Reading...", SOURCE_USB: "Scanning USB drives...",
            }.get(self.source, "Scanning music folder...")
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
        self._playlist_gen += 1
        self._shuffle_key = None
        if keep:
            spot = next(
                ((f, new.tracks.index(track)) for f, new in enumerate(folders)
                 if new.name == folder.name and track in new.tracks),
                None,
            )
            if spot:
                self.folder_index, self.track_index = spot
                self._sync_shuffle()
                self._tasks.put(self._prefetch_next)
                return
            self.playback.stop()  # its drive was unplugged
        else:
            with self._queue_lock:
                self._queue.clear()  # a different playlist
            self._return_to = None
            self._played.clear()
            self._ahead.clear()
        self.folder_index = 0
        self.track_index = 0
        if not folders:
            no_drive = self.source == SOURCE_USB and not self._usb_drives
            self.status_text = "Plug in a USB drive" if no_drive else "Nothing playable found"
            return
        total = sum(len(f.tracks) for f in folders)
        self.status_text = ""
        log.info("Playlist: %d folder(s), %d track(s)", len(folders), total)
        start_at = self._choose_start()
        self._load_current(autoplay=self.settings["autoplay"], start_at=start_at)
        self._tasks.put(self._fill_ids)  # identify the rest in the background

    def _choose_start(self) -> float:
        """Pick the first track of a new playlist; returns where to start in it.
        The first playlist after boot resumes the last session."""
        resume, self._resume_pending = self._resume_pending, None
        position = 0.0
        if (
            isinstance(resume, dict)
            and resume.get("source") == self.source
            and (self.source != SOURCE_FLOPPY or resume.get("label", "") == self.disk_label)
        ):
            limit = self.settings["back_limit"]
            trail = resume.get("played")
            if limit and isinstance(trail, list):  # Prev can go back past the restart
                self._played.extend(str(k) for k in trail[-limit:])
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
        """Where the next Next or Prev lands, without moving the cursor."""
        if not self.folders:
            return None
        if direction > 0:
            queued = self.queue
            if queued:
                return queued[0]
        if direction > 0 and self._ahead:
            ahead = self._position_of(self._ahead[-1])
            if ahead is not None:
                return ahead
        base = self._position_of(self._return_to) or (self.folder_index, self.track_index)
        if self.settings["play_mode"] in SHUFFLE_MODES:
            self._sync_shuffle()
            return self._shuffle.peek(direction)
        folder_index, track_index = base[0], base[1] + direction
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
        if self.settings["play_mode"] == "repeat" and not self._queue:
            return
        target = self._peek(+1)
        if target is None:
            return
        track = self.folders[target[0]].tracks[target[1]]
        if track.kind == "audio" and track.path is not None and self.settings["show_audio_bars"]:
            self._want_bars(track, str(track.path), 0.0, first=False)  # ready when it starts
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
        data = storage.fetch(self.link, self.source, track, attempts=attempts)
        if data and track.path is None:  # read off a floppy: identify it while it's in memory
            self._learn_id(folder_index, self.folders[folder_index].tracks.index(track), data)
        song = parse(data, track.display_name)
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
                    data = storage.fetch(self.link, self.source, track)
                    self.playback.load(track, data)
                    if data:  # audio off a floppy: the bytes are only here
                        self._learn_id(self.folder_index, self.track_index, data)
            except Exception as exc:
                log.warning(
                    "Skipping %s: %s: %s",
                    track.display_name, type(exc).__name__, exc,
                    exc_info=True,
                )
                self.status_text = f"Skipped {track.display_name}"
                if not self.settings["skip_bad_tracks"]:
                    return False
                if not self._queue_next():
                    self._leave_queue()
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
        self._note_played()
        self._learn_id(self.folder_index, self.track_index)  # before counting the play
        self._start_listening()
        if start_at > 0:
            self.playback.seek(start_at)
        self._art = (track, decode_art(self.playback.info.get("art")))
        if autoplay:
            # At boot, hold the first track until the speaker has connected or given up.
            self._output_ready.wait(OUTPUT_WAIT_S)
            self.playback.play()
        self._remember()
        self._tasks.put(self._prefetch_next)

    def _note_played(self) -> None:
        """Add the track that just started to the play history, unless Prev put us here."""
        limit = self.settings["back_limit"]
        key = self._track_key(self.folder_index, self.track_index)
        if self._retracing or not limit or key is None:
            return
        if self._played and self._played[-1] == key:
            return  # a repeat of the same track isn't somewhere else to go back to
        self._ahead.clear()  # playing something new ends the trail forward
        self._played.append(key)
        while len(self._played) > limit:
            self._played.popleft()

    def _retrace(self, direction: int) -> bool:
        """Prev walks back through what was actually played; Next walks forward again."""
        if not self.settings["back_limit"]:
            return False
        here = self._track_key(self.folder_index, self.track_index)
        while True:
            if direction < 0:
                # The last entry is where we are, so the one before it is the way back.
                if len(self._played) < 2:
                    return False
                current = self._played.pop()
                key = self._played[-1]
            else:
                if not self._ahead:
                    return False
                key = self._ahead.pop()
                current = None
            position = self._position_of(key)
            if position is None:
                continue  # that track has gone from the playlist: keep looking
            if direction < 0:
                self._ahead.append(current)
            elif here is not None and (not self._played or self._played[-1] != key):
                self._played.append(key)
            self.folder_index, self.track_index = position
            if self.settings["play_mode"] in SHUFFLE_MODES:
                self._sync_shuffle()
                self._shuffle.go_to(position)  # so what follows carries on from here
            self._retracing = True
            try:
                self._load_current(autoplay=True)
            finally:
                self._retracing = False
            return True

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
        # While the queue plays, the shuffle belongs to where it cut in.
        base = self._position_of(self._return_to) or (self.folder_index, self.track_index)
        positions = None
        if mode == "shuffle_folder":
            key = (mode, self._playlist_gen, base[0])
            positions = [(base[0], t) for t in range(len(self.folders[base[0]].tracks))]
        elif mode in FAVORITE_MODES:
            ticked = mode == "shuffle_favorites_ticked"
            favorites = self._favorite_positions(ticked)
            key = (mode, self._playlist_gen, self._ids_version,
                   frozenset(favorites), frozenset(excluded))
            positions = favorites or None  # none yet - shuffle the folders instead
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
        first = None if random_start else base
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
        """Next plays the queue, then obeys the play mode like a track ending; Prev always moves."""
        if direction > 0 and self._queue_next():
            self._load_current(autoplay=True)
            return
        if self._leave_queue() and (direction < 0 or self.settings["play_mode"] == "repeat"):
            # Prev from the queue goes back to where you were; Repeat to its track.
            self._load_current(autoplay=True)
            return
        if direction > 0 and self._ahead and self._retrace(+1):
            return  # back where Prev came from
        if direction > 0 and self.settings["play_mode"] == "repeat":
            self.playback.restart()  # replays from memory, no disk read
            self._start_listening()  # each repeat is a play of its own
            return
        if direction < 0 and self._retrace(-1):
            return
        self._move(direction)
        self._load_current(autoplay=True)

    def _on_song_finished(self) -> None:
        # Runs on a playback thread - hand the work to the worker. Queued tracks play regardless.
        if self.settings["autoplay"] or self.settings["play_mode"] == "repeat" or self._queue:
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
                    self.status_text = "No storage board - pick another source in Settings"
                    return
                time.sleep(2.0)

    def _startup(self) -> None:
        if self.needs_link:
            self._connect()
        if self._running:
            self._rebuild_playlist()

    # -- soundfonts ------------------------------------------------------

    def available_soundfonts(self) -> list[Path]:
        """.sf2 files, and folders of them - each folder plays as one soundfont."""
        return find_soundfonts(self.settings["soundfont_dirs"])

    def soundfont_label(self, path: Path) -> str:
        if path.is_dir():
            return f"{path.name}  ({instrument_count(path)} instruments)"
        return path.name

    def _backups(self, primary: str) -> list[str]:
        """Where instruments the playing soundfont lacks come from: the other slot, then the rest."""
        other = self.slot_soundfont(_other_slot(self.soundfont_slot))
        rest = [str(p) for p in self.available_soundfonts()]
        return [p for p in dict.fromkeys([other, *rest]) if p and p != primary and Path(p).exists()]

    def _resolve_soundfont(self) -> str:
        """The active slot's soundfont, else the other slot's, else the first found."""
        slot = self.soundfont_slot
        for candidate in (slot, _other_slot(slot)):
            path = self.settings[f"soundfont_{candidate}"]
            if path and Path(path).exists():
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
        """Put a soundfont in a slot: loaded if that slot is playing, else first in line as backup."""
        self.settings[f"soundfont_{slot}"] = path
        if slot == self.soundfont_slot:
            self._tasks.put(lambda: self._switch_soundfont(path))
        elif self.playback:
            playing = self.playback.soundfont
            self._tasks.put(lambda: self.playback.midi.set_backups(self._backups(playing)))

    def use_soundfont_slot(self, slot: str) -> None:
        path = self.slot_soundfont(slot)
        if not path or not Path(path).exists():
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
            self.playback.set_soundfont(chosen, self._backups(chosen))
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
                backups=self._backups(soundfont),
            )
        except Exception as exc:
            log.error("Could not start the synth with %s: %s", soundfont, exc)
            return 1
        self.playback.on_finished = self._on_song_finished
        self.playback.loop_repeats = self.settings["loop_repeats"]
        self.playback.set_loudness_match(self.settings["loudness_match"])

        for tasks, idle in (
            (self._tasks, self._idle_poll), (self._eq_tasks, self._eq_idle), (self._bt_tasks, self._bt_idle)
        ):
            threading.Thread(target=self._serve, args=(tasks, idle), daemon=True).start()
        if self.settings["output"] != SPEAKER and self.bt_available:
            self._output_ready.clear()
        self._tasks.put(self._startup)
        self._bt_tasks.put(self._restore_output)
        self.set_dimmed(False)

        self.ui = Ui(self, fullscreen="--windowed" not in sys.argv)
        try:
            self.ui.run()
        finally:
            self._running = False
            self._remember()
            self._ids.save()
            self.settings.flush()
            self.set_dimmed(False)
            self.playback.shutdown()
            if self.link:
                self.link.close()
            self.ui.close()  # last - pygame.quit() takes the mixer with it
        return self._exit_code


def _other_slot(slot: str) -> str:
    return "b" if slot == "a" else "a"


def _alarm_kind(weekday: int) -> str:
    return "weekend" if weekday >= 5 else "weekday"  # Monday is 0


def _alarm_keys(kind: str) -> tuple[str, str]:
    """The (enabled, time) settings for weekday or weekend alarms."""
    if kind == "weekend":
        return "alarm_weekend_enabled", "alarm_weekend_time"
    return "alarm_enabled", "alarm_time"


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
