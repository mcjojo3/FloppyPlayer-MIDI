"""One transport over both engines: FluidSynth for MIDI, pygame.mixer for audio."""

from __future__ import annotations

import io
import logging
import threading
import time

import pygame

import metadata
import player as midi_engine
from storage import Track

log = logging.getLogger(__name__)


class AudioPlayer:
    """Compressed/PCM audio via pygame.mixer."""

    def __init__(self, rate: int = 48000):
        self._rate = rate
        self._ready = False
        self._pos_offset = 0.0
        self._start_at = 0.0
        self._duration = 0.0
        self.info: dict = {}
        self._playing = False
        self._paused = False
        self._loaded = False
        self.on_finished = None
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()

    def _ensure_mixer(self) -> None:
        # Lazy: MIDI-only sessions never open a second stream.
        if not self._ready:
            pygame.mixer.init(frequency=self._rate, size=-16, channels=2, buffer=4096)
            self._ready = True

    def load(self, track: Track, data: bytes) -> None:
        self.stop()
        self._ensure_mixer()
        # Local files stream off disk rather than sitting in RAM.
        source = str(track.path) if track.path is not None else io.BytesIO(data)
        pygame.mixer.music.load(source)
        self.info = metadata.audio_info(track.path, data)
        self._duration = self.info["duration"]
        self._loaded = True
        self._playing = False
        self._pos_offset = 0.0
        self._start_at = 0.0

    def play(self) -> None:
        if not self._loaded:
            return
        if self._paused:
            pygame.mixer.music.unpause()
            self._paused = False
        elif not self._playing:
            self._pos_offset = 0.0
            try:
                pygame.mixer.music.play(start=self._start_at)
                self._pos_offset = self._start_at
            except pygame.error as exc:
                log.warning("Cannot start mid-track here, playing from the top: %s", exc)
                pygame.mixer.music.play()
            self._start_at = 0.0
            self._playing = True
            self._start_watcher()

    def pause(self) -> None:
        if self._playing and not self._paused:
            pygame.mixer.music.pause()
            self._paused = True

    def restart(self) -> None:
        if self._loaded:
            pygame.mixer.music.play()
            self._playing = True
            self._paused = False
            self._pos_offset = 0.0
            self._start_at = 0.0
            self._start_watcher()

    def _usable(self) -> bool:
        # pygame.quit() at shutdown removes the mixer under the watcher thread.
        return self._ready and pygame.mixer.get_init() is not None

    def stop(self) -> None:
        self._stop.set()
        if self._usable():
            try:
                pygame.mixer.music.stop()
            except pygame.error:
                pass
        self._playing = False
        self._paused = False
        self._loaded = False

    @property
    def is_playing(self) -> bool:
        if not self._usable():
            return False
        try:
            return bool(pygame.mixer.music.get_busy())
        except pygame.error:
            return False

    @property
    def position(self) -> float:
        if not self._playing:
            return self._start_at if self._loaded else 0.0
        if not self._usable():
            return 0.0
        try:
            millis = pygame.mixer.music.get_pos()
        except pygame.error:
            return 0.0
        return max(0.0, millis / 1000.0 + self._pos_offset)

    def seek(self, seconds: float) -> None:
        if not self._loaded:
            return
        if not self._playing:
            self._start_at = max(0.0, seconds)  # applied by play()
            return
        if not self._usable():
            return
        try:
            pygame.mixer.music.set_pos(seconds)
        except pygame.error as exc:
            log.warning("Seek not supported for this file: %s", exc)
            return
        # get_pos() counts from play() and ignores seeks.
        self._pos_offset = seconds - max(0.0, pygame.mixer.music.get_pos() / 1000.0)

    @property
    def duration(self) -> float:
        return self._duration

    def set_volume(self, value: float) -> None:
        if self._ready:
            pygame.mixer.music.set_volume(max(0.0, min(value, 1.0)))

    def _start_watcher(self) -> None:
        # pygame has no completion callback, so poll for the end of the file.
        self._stop.clear()
        if self._watcher and self._watcher.is_alive():
            return
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        time.sleep(0.3)  # let playback start before trusting get_busy()
        while not self._stop.is_set():
            if not self._usable():
                return
            # get_busy() is also False while paused - that isn't the end.
            try:
                busy = pygame.mixer.music.get_busy()
            except pygame.error:
                return
            if not self._paused and not busy:
                self._playing = False
                if self.on_finished:
                    self.on_finished()
                return
            time.sleep(0.2)


class Playback:
    """Routes transport calls to whichever engine suits the current track."""

    def __init__(
        self,
        soundfont: str,
        volume: float = 1.0,
        midi_gain: float = 0.5,
        audio_gain: float = 0.5,
        driver: str = "auto",
        rate: int = 48000,
        polyphony: int = 128,
        reverb: bool = True,
        chorus: bool = False,
    ):
        # One 0-1 volume, trimmed per engine - their scales are unrelated.
        self._volume = max(0.0, min(volume, 1.0))
        self._midi_gain = midi_gain
        self._audio_gain = audio_gain
        self.midi = midi_engine.MidiPlayer(
            soundfont, gain=self._volume * midi_gain, driver=driver, rate=rate,
            polyphony=polyphony, reverb=reverb, chorus=chorus,
        )
        self.audio = AudioPlayer(rate=rate)
        self.audio.set_volume(self._volume * audio_gain)
        self.midi.on_finished = self._finished
        self.audio.on_finished = self._finished
        self.on_finished = None
        self._active = None
        self._headroom = 1.0
        self.track: Track | None = None

    def _finished(self) -> None:
        if self.on_finished:
            self.on_finished()

    @property
    def loop_repeats(self) -> int:
        return self.midi.loop_repeats

    @loop_repeats.setter
    def loop_repeats(self, value: int) -> None:
        self.midi.loop_repeats = value

    def load(self, track: Track, data: bytes) -> None:
        # Stop the other engine before handing the device over.
        if self._active is not None and self._active is not self._engine_for(track):
            self._active.stop()
        self.track = track
        self.midi.reset_mixer()
        if track.kind == "midi":
            self.midi.load(midi_engine.parse(data, track.display_name))
            self._active = self.midi
        else:
            self.audio.load(track, data)
            self._active = self.audio
        self.set_volume(self._volume)

    def load_song(self, track: Track, song) -> None:
        """Load an already-parsed MIDI song."""
        if self._active is self.audio:
            self.audio.stop()
        self.track = track
        self.midi.reset_mixer()
        self.midi.load(song)
        self._active = self.midi
        self.set_volume(self._volume)

    def _engine_for(self, track: Track):
        return self.midi if track.kind == "midi" else self.audio

    def play(self) -> None:
        if self._active:
            self._active.play()

    def pause(self) -> None:
        if self._active:
            self._active.pause()

    def toggle(self) -> None:
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        if self._active:
            self._active.stop()

    @property
    def is_midi(self) -> bool:
        return self._active is self.midi

    @property
    def loop_status(self) -> str:
        """Human-readable ZUN loop state, or "" when it doesn't apply."""
        if not self.is_midi or not self.midi.has_loop:
            return ""
        left, done = self.midi.loops_left, self.midi.loops_done
        if left < 0:
            return f"Looping (x{done})" if done else "Loop point"
        if left == 0:
            return "Final pass" if done else "Loop off"
        return f"{left} loop{'s' if left != 1 else ''} left"

    @property
    def info(self) -> dict:
        """title/artist/album/copyright/art for the loaded track."""
        if self._active is self.midi and self.midi.song is not None:
            song = self.midi.song
            return {"title": song.title, "copyright": song.copyright}
        if self._active is self.audio:
            return self.audio.info
        return {}

    @property
    def soundfont(self) -> str:
        return self.midi.soundfont

    def set_soundfont(self, path) -> None:
        """Swap soundfonts mid-song, carrying on from the same spot."""
        if self._active is not self.midi:
            self.midi.load_soundfont(path)
            return
        was_playing = self.midi.is_playing
        position = self.midi.position
        self.midi.pause()
        self.midi.load_soundfont(path)
        # Re-applies programs and controllers at this point with the new font.
        self.midi.seek(position)
        if was_playing:
            self.midi.play()

    def set_headroom_db(self, db: float) -> None:
        """Drop the output so EQ boosts can't clip."""
        self._headroom = 10 ** (-max(0.0, db) / 20)
        self.set_volume(self._volume)

    def seek(self, seconds: float) -> None:
        if self._active:
            self._active.seek(seconds)

    def restart(self) -> None:
        """Replay the loaded track from the top without re-reading it."""
        if self._active is self.midi and self.midi.song is not None:
            self.midi.load(self.midi.song)
            self.midi.play()
        elif self._active is self.audio:
            self.audio.restart()

    @property
    def is_playing(self) -> bool:
        return bool(self._active and self._active.is_playing)

    @property
    def has_track(self) -> bool:
        return self._active is not None and self.track is not None

    @property
    def position(self) -> float:
        return self._active.position if self._active else 0.0

    @property
    def duration(self) -> float:
        return self._active.duration if self._active else 0.0

    def set_volume(self, volume: float) -> None:
        """volume is 0-1; each engine applies its own trim."""
        self._volume = max(0.0, min(volume, 1.0))
        self.midi.set_gain(self._volume * self._midi_gain * self._headroom)
        self.audio.set_volume(min(1.0, self._volume * self._audio_gain) * self._headroom)

    def shutdown(self) -> None:
        self.audio.stop()
        self.midi.shutdown()
