// config.h - centralized tuning: pins, retry/give-up patience, feature
// sizes. Adapting this to different hardware, a different disk/drive, or
// just retuning behavior should mostly mean editing this file, not hunting
// through every .cpp.
#pragma once

// ============================================================================
// PINS
// ============================================================================
// Floppy drive control pins live in floppy_fat12.cpp - internal to the
// driver, not exposed here. Everything below is player/UI hardware.

// -- Buttons (momentary, to GND, internal pull-up) --
#define PIN_NEXT          12
#define PIN_PREV          13
#define PIN_PLAYPAUSE     14
#define PIN_VOLUP         15
#define PIN_VOLDOWN       17
#define PIN_SCREEN_BUTTON 6   // swaps the OLED between user/technical screens

// Loop/Autoplay/Shuffle used to be dedicated grounded switches here. They're
// on-screen config now (long-press the screen button - see FloppyPlayer-MIDI.ino's
// cfgLoop/cfgAutoplay/cfgShuffle), persisted in EEPROM instead of a switch
// position, so GPIO16/2/3 are free again for whatever's next (the ESP32 link?).

// -- Volume pot (continuous, overrides the buttons whenever it's turned) --
#define PIN_VOL_POT 26

// -- Audio out (PWM + RC filter per channel, see README) --
#define PIN_AUDIO_LEFT  22
#define PIN_AUDIO_RIGHT 23

// -- OLED (I2C0, SSD1306/SH1106-compatible, optional) --
#define OLED_SDA_PIN 24
#define OLED_SCL_PIN 25
#define OLED_WIDTH   128
#define OLED_HEIGHT  64

// ============================================================================
// BUTTON TIMING
// ============================================================================
#define DEBOUNCE_MS   30
#define LONG_PRESS_MS 600

// ============================================================================
// RETRY / PATIENCE - how hard to try before giving up.
// Real hardware logs showed even 20 attempts doesn't always reach a clean
// read, and dense multi-track files can need far more cylinders active at
// once than the cache has slots for - so past a reasonable number of tries,
// skipping is the better default over a long, possibly-futile stall.
// ============================================================================

// floppy_fat12.cpp: how many times a single marginal cylinder can be
// force-refetched from scratch before accepting whatever it gave us and
// no longer retrying. ~2.5s/cycle -> ~7.5s worst case for one cylinder.
#define MAX_CYLINDER_REFETCH_STREAK 3

// floppy_fat12.cpp: how many cylinders' worth of sectors to cache at once
// (LRU eviction) - trades RAM for fewer reseeks when multiple tracks sit
// on different cylinders simultaneously. See project notes for the
// RAM-vs-slots table this was chosen from.
#define NUM_CACHE_SLOTS 3

// floppy_fat12.cpp: how long to wait for an INDEX pulse (before, and
// during, a capture) before concluding no disk is present/spinning.
#define INDEX_WAIT_TIMEOUT_MS 1000

// midi_sequencer.cpp: how long a track's read position can be stuck before
// giving up on it entirely and marking it finished (so playback skips to
// the next track instead of hanging). ~4-8 real capture attempts at this
// value - long enough to ride out one bad cylinder, short enough that a
// genuinely troubled track gets skipped quickly rather than stalling.
// Whether this give-up path is armed at all is runtime-configurable (the
// on-screen "Skip Bad Tracks" setting, on by default) - see
// midi_seq_set_skip_bad_tracks() in midi_sequencer.h.
#define MIDI_SEQ_MAINTAIN_GIVE_UP_MS 8000

// FloppyPlayer-MIDI.ino: diagnostic only - dumps sequencer state to Serial
// if no note dispatches for this long while playing. Doesn't affect
// playback either way, just visibility.
#define DISPATCH_STALL_TIMEOUT_MS 3000

// FloppyPlayer-MIDI.ino: mutes (fades to silence) as soon as a genuine
// buffer-starvation stall is detected - well before the give-up above -
// so a long retry is silent instead of a droning stuck note.
#define STALL_MUTE_MS 500

// FloppyPlayer-MIDI.ino: freezes the song clock the instant any track
// stalls - far more sensitive than STALL_MUTE_MS above - so wall-clock
// time doesn't keep accruing during a stall and cause a rushed catch-up
// burst once it resolves.
#define CLOCK_STALL_THRESHOLD_MS 20

// FloppyPlayer-MIDI.ino: how often to retry mounting after a disk-change
// event, or after finding no playable tracks.
#define REMOUNT_RETRY_INTERVAL_MS 1000
#define EMPTY_PLAYLIST_RETRY_INTERVAL_MS 1000

// ============================================================================
// AUDIO
// ============================================================================
#define AUDIO_SAMPLE_RATE 11025
#define MAX_VOICES 24
#define VOLUME_FLOOR_DB (-50.0f) // quietest non-off step - true silence (button 0 / pot dead zone) is separate

// ADSR envelope (seconds), shared by every voice regardless of waveform.
#define ATTACK  0.01f
#define DECAY   0.1f
#define SUSTAIN 0.7f
#define RELEASE 0.15f

// Brass unison's second oscillator detune (~10 cents sharp) - see synth.cpp.
#define DETUNE_RATIO 1.006f

// ============================================================================
// DISPLAY
// ============================================================================
// How often the throttled live view redraws while playing. A full OLED
// redraw blocks Core 0 for ~20-30ms even at Fast Mode I2C, so this needs
// to stay well above "every loop() iteration" - a few times a second, not continuously.
#define LIVE_DISPLAY_INTERVAL_MS 300

// ============================================================================
// PLAYLIST
// ============================================================================
#define MAX_TRACKS_PER_FOLDER 32
#define MAX_FOLDERS 4

// ============================================================================
// MISC
// ============================================================================
// Software ring buffer for Core0->Core1 note commands - see
// FloppyPlayer-MIDI.ino. Far bigger than the 8-entry hardware FIFO it
// replaced, since a dropped command under a dense chord means a stuck or
// missing note.
#define NOTE_CMD_QUEUE_SIZE 128

// ============================================================================
// NOT YET IMPLEMENTED - reserved for the "remember faulty tracks" idea
// being considered after the current long-playtime test session. Once
// decided: a per-track fault flag/threshold would go here.
// ============================================================================
