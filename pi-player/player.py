"""MIDI playback through FluidSynth, with ZUN/Touhou loop points."""

from __future__ import annotations

import bisect
import io
import logging
import re
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import fluidsynth
import mido

from metadata import decode_midi_text

log = logging.getLogger(__name__)


def _make_mido_tolerant() -> None:
    """Treat malformed meta events (e.g. empty key_signature) as unknown, not fatal."""
    try:
        from mido.midifiles import meta as _meta
        from mido.midifiles import midifiles as _midifiles

        original = _meta.build_meta_message

        def tolerant(meta_type, data, delta=0):
            try:
                return original(meta_type, data, delta)
            except Exception:
                return _meta.UnknownMetaMessage(meta_type, data, delta)

        _meta.build_meta_message = tolerant
        _midifiles.build_meta_message = tolerant  # imported by name there
    except Exception as exc:
        log.warning("Could not relax mido's meta parsing: %s", exc)


_make_mido_tolerant()


def find_soundfonts(dirs) -> list[Path]:
    """Every .sf2 across the configured folders, deduped, sorted by name."""
    found: dict[str, Path] = {}
    for directory in dirs:
        path = Path(directory).expanduser()
        if not path.is_dir():
            continue
        for sf2 in path.glob("*.sf2"):
            if sf2.is_file():
                found.setdefault(sf2.name, sf2)
    return [found[name] for name in sorted(found, key=str.lower)]


DRUM_CHANNEL = 9  # GM channel 10, 0-based
DRUM_BANK = 128

LOOP_START_CC = 2  # ZUN's loop marker; the loop end is the end of the track
BANK_SELECT_CC = 0
DATA_ENTRY_MSB, DATA_ENTRY_LSB = 6, 38
NRPN_LSB, NRPN_MSB, RPN_LSB, RPN_MSB = 98, 99, 100, 101
ALL_SOUND_OFF_CC = 120
RESET_CONTROLLERS_CC = 121
ALL_NOTES_OFF_CC = 123
# What CC#121 leaves alone (GM RP-015).
KEPT_ON_RESET = {0, 7, 8, 10, 32, 39, 40, 42, 91, 92, 93, 94, 95, *range(70, 80)}

LOOP_FOREVER = -1
TEMPO_RANGE = (0.5, 2.0)
TRANSPOSE_RANGE = (-12, 12)

# Instruments stay selected on spare channels (pyfluidsynth creates 256), so
# dynamic sample loading keeps them in RAM instead of reloading from disk.
PIN_FIRST_CHANNEL = 16
PIN_LIMIT = 64

LATE_EVENT_S = 0.05  # scheduler lateness worth reporting
LATE_REPORT_S = 5.0

_GENERIC_TITLE = re.compile(
    r"^(untitled|track\s*\d*|tempo( track)?|conductor|setup|system setup|sequence\s*\d*)$", re.I
)


@dataclass
class _ChannelState:
    """Channel setup at one point in a song, restored on loops and seeks."""

    program: dict[int, tuple[int, int]] = field(default_factory=dict)  # channel -> (bank, program)
    controls: dict[tuple[int, int], int] = field(default_factory=dict)  # oldest change first
    params: dict[tuple[int, int, int, int], tuple[int, int | None]] = field(default_factory=dict)
    pitch: dict[int, int] = field(default_factory=dict)


@dataclass
class Song:
    name: str
    events: list[tuple[float, mido.Message]]
    duration: float
    loop_index: int | None
    loop_time: float
    loop_state: _ChannelState
    presets: set[tuple[int, int, int]]  # (channel, bank, program) the song selects
    start_presets: dict[int, tuple[int, int]] = field(default_factory=dict)
    channels: dict[int, int] = field(default_factory=dict)  # channel with notes -> first program
    title: str = ""
    copyright: str = ""


def _bank_for(channel: int, bank: int) -> int:
    return DRUM_BANK if channel == DRUM_CHANNEL else bank


def _unwrap_rmi(data: bytes) -> bytes:
    """RIFF RMID (.RMI) files carry a standard MIDI file in their data chunk."""
    if data[:4] != b"RIFF" or data[8:12] != b"RMID":
        return data
    pos = 12
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        if data[pos : pos + 4] == b"data":
            return data[pos + 8 : pos + 8 + size]
        pos += 8 + size + (size & 1)
    return data


def _salvage(data: bytes, name: str):
    """Rebuild a file from the tracks mido can read, dropping only bad ones."""
    if data[:4] != b"MThd":
        raise ValueError("not a MIDI file (no MThd)")
    header_len = int.from_bytes(data[4:8], "big")
    division = int.from_bytes(data[12:14], "big")
    pos = 8 + header_len

    tracks, dropped = [], 0
    while pos + 8 <= len(data):
        if data[pos : pos + 4] != b"MTrk":
            break
        length = int.from_bytes(data[pos + 4 : pos + 8], "big")
        chunk = data[pos : pos + 8 + length]
        pos += 8 + length
        single = struct.pack(">4sIHHH", b"MThd", 6, 0, 1, division) + chunk
        try:
            tracks.append(mido.MidiFile(file=io.BytesIO(single), clip=True).tracks[0])
        except Exception:
            dropped += 1

    if not tracks:
        raise ValueError("no parseable tracks")
    log.warning("%s: recovered %d track(s), dropped %d", name, len(tracks), dropped)
    salvaged = mido.MidiFile(type=1, ticks_per_beat=division or 480)
    salvaged.tracks = tracks
    return salvaged


def parse(data: bytes, name: str = "") -> Song:
    """Flatten a MIDI file into an absolute-time event list."""
    data = _unwrap_rmi(data)
    try:
        mid = mido.MidiFile(file=io.BytesIO(data), clip=True)
    except Exception as exc:
        log.debug("%s: %s - trying per-track recovery", name, exc)
        mid = _salvage(data, name)

    events: list[tuple[float, mido.Message]] = []
    presets: set[tuple[int, int, int]] = set()
    start_presets: dict[int, tuple[int, int]] = {}
    bank: dict[int, int] = {}
    channels_with_notes: set[int] = set()
    tempo = 500000  # MIDI default, 120bpm
    abs_time = 0.0
    loop_index: int | None = None
    loop_time = 0.0

    for msg in mido.merge_tracks(mid.tracks):
        # A set_tempo only affects the messages after it.
        abs_time += mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
        if msg.type == "set_tempo":
            tempo = msg.tempo
            continue
        if msg.is_meta:
            continue
        if msg.type == "control_change":
            if msg.control == BANK_SELECT_CC:
                bank[msg.channel] = msg.value
            elif msg.control == LOOP_START_CC and loop_index is None:
                loop_index = len(events)
                loop_time = abs_time
        elif msg.type == "program_change":
            preset = (_bank_for(msg.channel, bank.get(msg.channel, 0)), msg.program)
            presets.add((msg.channel, *preset))
            start_presets.setdefault(msg.channel, preset)
        elif msg.type == "note_on" and msg.velocity > 0:
            channels_with_notes.add(msg.channel)
        events.append((abs_time, msg))

    for channel in channels_with_notes:  # never sent a program change: program 0
        if channel not in start_presets:
            start_presets[channel] = (_bank_for(channel, 0), 0)
            presets.add((channel, *start_presets[channel]))

    if loop_index is not None and abs_time - loop_time < 0.01:
        loop_index = None  # a marker at the very end would loop on nothing

    title, copyright = _header_text(mid)
    return Song(
        name=name,
        events=events,
        duration=abs_time,
        loop_index=loop_index,
        loop_time=loop_time,
        loop_state=_snapshot_at(events, loop_index),
        presets=presets,
        start_presets=start_presets,
        channels={ch: start_presets[ch][1] for ch in sorted(channels_with_notes)},
        title=title,
        copyright=copyright,
    )


def _header_text(mid) -> tuple[str, str]:
    """Song title and copyright from the first track's meta events."""
    title = copyright = ""
    for msg in mid.tracks[0] if mid.tracks else []:
        if msg.type == "track_name" and not title:
            name = decode_midi_text(msg.name)
            if not _GENERIC_TITLE.match(name):
                title = name
        elif msg.type == "copyright" and not copyright:
            copyright = decode_midi_text(msg.text)
    return title, copyright


def _snapshot_at(events, index: int | None) -> _ChannelState:
    """Channel state as of events[index]."""
    state = _ChannelState()
    bank: dict[int, int] = {}
    selected: dict[int, list[int]] = {}  # channel -> [101 or 99, rpn msb, rpn lsb, nrpn msb, nrpn lsb]
    for _, msg in events[: index or 0]:
        if msg.type == "program_change":
            state.program[msg.channel] = (_bank_for(msg.channel, bank.get(msg.channel, 0)), msg.program)
        elif msg.type == "pitchwheel":
            state.pitch[msg.channel] = msg.pitch
        elif msg.type == "control_change":
            _record_control(state, bank, selected, msg.channel, msg.control, msg.value)
    return state


def _record_control(state, bank, selected, ch: int, cc: int, value: int) -> None:
    if cc in (ALL_SOUND_OFF_CC, ALL_NOTES_OFF_CC, LOOP_START_CC):
        return
    if cc == RESET_CONTROLLERS_CC:
        for key in [k for k in state.controls if k[0] == ch and k[1] not in KEPT_ON_RESET]:
            del state.controls[key]
        state.pitch.pop(ch, None)
        return
    if cc == BANK_SELECT_CC:
        bank[ch] = value

    sel = selected.setdefault(ch, [RPN_MSB, 127, 127, 127, 127])
    if cc in (RPN_MSB, RPN_LSB, NRPN_MSB, NRPN_LSB):
        sel[0] = RPN_MSB if cc in (RPN_MSB, RPN_LSB) else NRPN_MSB
        sel[{RPN_MSB: 1, RPN_LSB: 2, NRPN_MSB: 3, NRPN_LSB: 4}[cc]] = value
    elif cc in (DATA_ENTRY_MSB, DATA_ENTRY_LSB):
        # Data entry means nothing on its own - keep it per parameter.
        msb, lsb = (sel[1], sel[2]) if sel[0] == RPN_MSB else (sel[3], sel[4])
        if (msb, lsb) != (127, 127):
            key = (ch, sel[0], msb, lsb)
            data_msb, data_lsb = state.params.pop(key, (0, None))
            state.params[key] = (value, data_lsb) if cc == DATA_ENTRY_MSB else (data_msb, value)
        return

    state.controls.pop((ch, cc), None)  # re-insert so the order follows the latest change
    state.controls[(ch, cc)] = value


class MidiPlayer:
    """Plays one Song at a time on a background thread."""

    def __init__(
        self,
        soundfont: str,
        gain: float = 1.0,
        driver: str = "auto",
        rate: int = 48000,
        polyphony: int = 128,
        reverb: bool = True,
        chorus: bool = False,
    ):
        self.fs = fluidsynth.Synth(
            gain=gain,
            samplerate=float(rate),
            **{
                "synth.polyphony": int(polyphony),
                "synth.reverb.active": 1 if reverb else 0,
                "synth.chorus.active": 1 if chorus else 0,
                # Only presets in use are held in RAM.
                "synth.dynamic-sample-loading": 1,
                # Latency doesn't matter for playback; a deep buffer rides out CPU spikes.
                "audio.period-size": 1024,
                "audio.periods": 8,
            },
        )
        self.driver = self._start_audio(driver)

        self.song: Song | None = None
        self.on_finished = None
        self.loop_repeats = LOOP_FOREVER
        self._loops_left = LOOP_FOREVER
        self._loops_done = 0

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._position = 0.0
        self._clock: tuple[float, float] | None = None  # (origin, tempo) while running
        self._seek_to: float | None = None
        self._lock = threading.Lock()

        # Mixer, read by the playback thread on every event.
        self.tempo = 1.0
        self.transpose = 0
        self.muted: frozenset[int] = frozenset()
        self.solo: frozenset[int] = frozenset()

        self._closed = False
        self._bank = [0] * 16
        self._pins: OrderedDict[tuple[int, int], int] = OrderedDict()  # (bank, program) -> channel
        self._resolved: dict[tuple[bool, int, int], tuple[int, int] | None] = {}
        self.sfid = -1
        self.soundfont = ""
        self.load_soundfont(soundfont)

    def _start_audio(self, driver: str) -> str:
        """Start the audio driver; "auto" tries pulseaudio (PipeWire), then ALSA."""
        candidates = [driver] if driver != "auto" else ["pulseaudio", "alsa"]
        for name in candidates:
            try:
                self.fs.start(driver=name)
            except Exception as exc:
                log.warning("Audio driver %s unavailable: %s", name, exc)
                continue
            # pyfluidsynth doesn't raise when the device fails to open.
            if getattr(self.fs, "audio_driver", None):
                log.info("Audio driver: %s", name)
                return name
            log.warning("Audio driver %s failed to open the device", name)
        raise RuntimeError("no working audio driver (tried: " + ", ".join(candidates) + ")")

    def load_soundfont(self, path) -> None:
        """Swap soundfonts; the new one loads first, so a bad file changes nothing."""
        path = str(path)
        new_id = self.fs.sfload(path)
        if new_id == -1:
            raise RuntimeError(f"could not load soundfont: {path}")
        for channel in self._pins.values():
            self.fs.program_unset(channel)  # releases the old font's samples
        self._pins.clear()
        if self.sfid != -1:
            self.fs.sfunload(self.sfid)
        self.sfid = new_id
        self.soundfont = path
        self._resolved.clear()
        log.info("Soundfont: %s", path)
        if self.song is not None:
            self._preload(self.song)

    # -- transport -------------------------------------------------------

    def load(self, song: Song) -> None:
        self.stop()
        self.song = song
        with self._lock:
            self._position = 0.0
            self._clock = None
        self._seek_to = None
        self._preload(song)

    def _preload(self, song: Song) -> None:
        """Load every instrument the song uses up front, so none loads mid-song."""
        started = time.perf_counter()
        new = {(bank, program) for _, bank, program in song.presets} - self._pins.keys()
        for _, bank, program in sorted(song.presets):
            self._pin(bank, program)
        for channel, (bank, program) in song.start_presets.items():
            self._select(channel, bank, program)
        if new:
            log.info(
                "Loaded %d instrument(s) in %.0fms",
                len(new), (time.perf_counter() - started) * 1000,
            )

    def _pin(self, bank: int, program: int) -> None:
        key = (bank, program)
        if key in self._pins:
            self._pins.move_to_end(key)
            return
        if len(self._pins) < PIN_LIMIT:
            channel = PIN_FIRST_CHANNEL + len(self._pins)
        else:
            _, channel = self._pins.popitem(last=False)
        self._pins[key] = channel
        self._select(channel, bank, program)

    def _select(self, channel: int, bank: int, program: int) -> None:
        """Select a preset, falling back like a GS module: bank 0, or the standard kit."""
        drum = channel == DRUM_CHANNEL or bank == DRUM_BANK
        key = (drum, bank, program)
        if key in self._resolved:
            preset = self._resolved[key]
            if preset is not None:
                self.fs.program_select(channel, self.sfid, *preset)
            return
        candidates = [(bank, program)] + (
            [(DRUM_BANK, program), (DRUM_BANK, 0)] if drum else [(0, program)]
        )
        preset = None
        for candidate in dict.fromkeys(candidates):
            if self.fs.program_select(channel, self.sfid, *candidate) == 0:
                preset = candidate
                break
        self._resolved[key] = preset
        if preset is None:
            log.warning("No preset for bank %d program %d", bank, program)

    def play(self) -> None:
        if self.song is None:
            return
        if self._thread and self._thread.is_alive():
            self._resume.set()  # unpause
            return
        self._stop.clear()
        self._resume.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        with self._lock:
            self._position = self._clock_position()
            self._clock = None
        self._resume.clear()
        self._all_notes_off()

    def toggle(self) -> None:
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()  # let a paused thread notice the stop
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        self._all_notes_off()

    @property
    def is_playing(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._resume.is_set())

    @property
    def position(self) -> float:
        with self._lock:
            return self._clock_position()

    def _clock_position(self) -> float:
        # The clock, not the last event, so sparse passages still move.
        if self._clock is None or not self._resume.is_set():
            return self._position
        origin, tempo = self._clock
        return max(self._position, min((time.perf_counter() - origin) * tempo, self.duration))

    @property
    def duration(self) -> float:
        return self.song.duration if self.song else 0.0

    @property
    def has_loop(self) -> bool:
        return bool(self.song and self.song.loop_index is not None)

    @property
    def loops_left(self) -> int:
        """Remaining loop-backs; LOOP_FOREVER (-1) means unlimited."""
        return self._loops_left

    @property
    def loops_done(self) -> int:
        return self._loops_done

    def seek(self, seconds: float) -> None:
        """Ask the playback thread to jump; applied within one tick."""
        if self.song is None:
            return
        with self._lock:
            self._clock = None
            self._position = max(0.0, min(seconds, self.duration))
        self._seek_to = self._position

    def set_gain(self, gain: float) -> None:
        if not self._closed:  # other threads can still call this during shutdown
            self.fs.setting("synth.gain", max(0.0, min(gain, 10.0)))

    # -- mixer -----------------------------------------------------------

    def audible(self, channel: int) -> bool:
        if self.solo and channel not in self.solo:
            return False
        return channel not in self.muted

    def set_tempo(self, factor: float) -> None:
        self.tempo = max(TEMPO_RANGE[0], min(factor, TEMPO_RANGE[1]))

    def set_transpose(self, semitones: int) -> None:
        self.transpose = max(TRANSPOSE_RANGE[0], min(int(semitones), TRANSPOSE_RANGE[1]))

    def toggle_mute(self, channel: int) -> None:
        self.muted = self.muted ^ {channel}
        self._silence_inaudible()

    def toggle_solo(self, channel: int) -> None:
        self.solo = self.solo ^ {channel}
        self._silence_inaudible()

    def reset_mixer(self) -> None:
        self.tempo = 1.0
        self.transpose = 0
        self.muted = frozenset()
        self.solo = frozenset()

    def _silence_inaudible(self) -> None:
        for channel in range(16):
            if not self.audible(channel):
                self.fs.cc(channel, ALL_SOUND_OFF_CC, 0)

    # -- playback --------------------------------------------------------

    def _all_notes_off(self) -> None:
        for channel in range(16):
            self.fs.cc(channel, ALL_NOTES_OFF_CC, 0)

    def _reset_channels(self) -> None:
        """Controllers back to power-on defaults."""
        self._bank = [0] * 16
        for ch in range(16):
            self.fs.cc(ch, RESET_CONTROLLERS_CC, 0)
            for cc, value in ((0, 0), (32, 0), (7, 100), (8, 64), (10, 64), (91, 0), (93, 0)):
                self.fs.cc(ch, cc, value)
            # Pitch bend range back to 2 semitones, then deselect the RPN.
            for cc, value in ((RPN_MSB, 0), (RPN_LSB, 0), (DATA_ENTRY_MSB, 2), (DATA_ENTRY_LSB, 0),
                              (RPN_MSB, 127), (RPN_LSB, 127)):
                self.fs.cc(ch, cc, value)

    def _restore(self, state: _ChannelState) -> None:
        self._reset_channels()
        for (ch, kind, msb, lsb), (data_msb, data_lsb) in state.params.items():
            self.fs.cc(ch, kind, msb)
            self.fs.cc(ch, kind - 1, lsb)
            self.fs.cc(ch, DATA_ENTRY_MSB, data_msb)
            if data_lsb is not None:
                self.fs.cc(ch, DATA_ENTRY_LSB, data_lsb)
        for (ch, cc), value in state.controls.items():
            if cc == BANK_SELECT_CC:
                self._bank[ch] = value
            self.fs.cc(ch, cc, value)
        for ch, (bank, program) in state.program.items():
            self._select(ch, bank, program)
        for ch, pitch in state.pitch.items():
            self.fs.pitch_bend(ch, pitch)

    def _dispatch(self, msg: mido.Message, transpose: int) -> None:
        kind = msg.type
        if kind in ("note_on", "note_off"):
            note = msg.note
            if transpose and msg.channel != DRUM_CHANNEL:
                note = max(0, min(note + transpose, 127))
            if kind == "note_on" and msg.velocity > 0:
                if self.audible(msg.channel):
                    self.fs.noteon(msg.channel, note, msg.velocity)
            else:
                self.fs.noteoff(msg.channel, note)
        elif kind == "control_change":
            if msg.control == BANK_SELECT_CC:
                self._bank[msg.channel] = msg.value
            self.fs.cc(msg.channel, msg.control, msg.value)
        elif kind == "program_change":
            self._select(msg.channel, _bank_for(msg.channel, self._bank[msg.channel]), msg.program)
        elif kind == "pitchwheel":
            self.fs.pitch_bend(msg.channel, msg.pitch)

    def _run(self) -> None:
        song = self.song
        if song is None or not song.events:
            self._finish()
            return

        repeats_left = self.loop_repeats
        self._loops_left = repeats_left
        self._loops_done = 0
        index = 0
        # Song time t is due at origin + t / tempo.
        tempo = self.tempo
        transpose = self.transpose
        origin = time.perf_counter()
        self._restore(_ChannelState())
        with self._lock:
            self._position = 0.0
            self._clock = (origin, tempo)
        late_count, late_worst, late_since = 0, 0.0, origin

        while not self._stop.is_set():
            if self._seek_to is not None:
                target, self._seek_to = self._seek_to, None
                index = bisect.bisect_left(song.events, target, key=lambda e: e[0])
                self._all_notes_off()
                self._restore(_snapshot_at(song.events, index))
                origin = time.perf_counter() - target / tempo
                with self._lock:
                    self._position = target
                    self._clock = (origin, tempo)
                continue

            # After the last event, wait for the track's end: ZUN loops jump from there.
            at_end = index >= len(song.events)
            due = song.duration if at_end else song.events[index][0]

            # Short sleeps stay responsive; absolute deadlines stop jitter adding up.
            while not self._stop.is_set() and self._seek_to is None:
                if not self._resume.is_set():
                    paused_at = time.perf_counter()
                    self._resume.wait()
                    origin += time.perf_counter() - paused_at
                    with self._lock:
                        self._clock = (origin, tempo)
                    continue
                now = time.perf_counter()
                if self.tempo != tempo:
                    song_now = (now - origin) * tempo
                    tempo = self.tempo
                    origin = now - song_now / tempo
                    with self._lock:
                        self._clock = (origin, tempo)
                remaining = origin + due / tempo - now
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.05))

            if self._stop.is_set():
                break
            if self._seek_to is not None:
                continue

            if at_end:
                if song.loop_index is None or repeats_left == 0:
                    break
                if repeats_left > 0:
                    repeats_left -= 1
                self._loops_left = repeats_left
                self._loops_done += 1
                self._all_notes_off()
                self._restore(song.loop_state)
                index = song.loop_index
                origin += (song.duration - song.loop_time) / tempo  # stays on the beat
                with self._lock:
                    self._position = song.loop_time
                    self._clock = (origin, tempo)
                continue

            # Lateness shows Python-side stutter; device underruns don't show here.
            now = time.perf_counter()
            late = now - (origin + due / tempo)
            if late > LATE_EVENT_S:
                late_count += 1
                late_worst = max(late_worst, late)
            if now - late_since >= LATE_REPORT_S:
                if late_count:
                    log.warning(
                        "scheduler late on %d event(s) in %.0fs, worst %.0fms",
                        late_count, LATE_REPORT_S, late_worst * 1000,
                    )
                late_count, late_worst, late_since = 0, 0.0, now

            if self.transpose != transpose:
                self._all_notes_off()  # held notes' note-offs would miss at the new pitch
                transpose = self.transpose
            self._dispatch(song.events[index][1], transpose)
            with self._lock:
                self._position = due
            index += 1

        self._all_notes_off()
        with self._lock:
            self._clock = None
        if not self._stop.is_set():
            self._finish()

    def _finish(self) -> None:
        callback = self.on_finished
        if callback:
            callback()

    def shutdown(self) -> None:
        self.stop()
        self._closed = True
        try:
            self.fs.delete()
        except Exception:
            pass
