// midi_sequencer.h - streaming SMF parser/scheduler. See README.md for the
// double-buffering and timer-dispatch design; the short version:
//
// Each track cursor keeps two half-size window buffers and double-buffers
// between them (read from the active half; once past 10% consumed,
// midi_seq_maintain() proactively loads the next chunk into the inactive
// half, so the swap is instant when playback reaches it). A synchronous
// fallback exists for the rare case preloading didn't finish in time.
//
// Multi-track (SMF format 1) files are handled via a live K-way merge, one
// cursor per track, always dispatching whichever track's next event has the
// smallest tick. Single-track (format 0) is just the ntrks==1 case.
//
// midi_seq_advance()'s allowBlockingRead=false path (used from the dispatch
// timer interrupt) never touches the floppy: if a byte isn't already
// preloaded, nextByte() reports a "stall" instead of blocking, and the
// track is retried on the next call once midi_seq_maintain() (main loop
// only) catches up.
#pragma once
#include <stdint.h>
#include "floppy_fat12.h"

enum MidiEventType {
  MIDI_EVT_NOTE_ON,
  MIDI_EVT_NOTE_OFF,
};

struct MidiEvent {
  uint32_t time_us;
  uint8_t type; // MidiEventType - can't be inferred from velocity alone (note-off may carry nonzero release velocity)
  uint8_t channel;
  uint8_t note;
  uint8_t velocity;
};

#define MIDI_SEQ_MAX_TRACKS 24 // covers any SMF format-1 file with headroom
#define MIDI_SEQ_HALF_WINDOW_BYTES 2048 // each track keeps two of these, ping-ponging

struct MidiTrackCursor {
  uint32_t pos;    // current read position, absolute offset into the file
  uint32_t end;    // one past this track's last byte
  uint32_t absTick;
  uint8_t runningStatus;
  bool haveRunningStatus;
  bool finished;
  bool pendingValid;
  uint32_t pendingTick;
  uint8_t pendingType; // MidiEventType, or an internal marker (tempo/CC/other) - see .cpp
  uint8_t pendingChannel, pendingA, pendingB;

  // Double-buffered read-ahead window. preloadValid is the single-bool
  // guard that makes the producer(main loop)/consumer(dispatch interrupt)
  // handoff safe without a lock: the producer writes the buffer and
  // preloadBase/preloadValidBytes FIRST and sets preloadValid=true LAST;
  // the consumer only trusts them after observing preloadValid==true.
  // volatile so the compiler can't reorder/cache these across the
  // interrupt boundary.
  uint8_t half[2][MIDI_SEQ_HALF_WINDOW_BYTES];
  volatile int activeHalf;
  volatile uint32_t activeBase;
  volatile uint32_t activeValidBytes;
  volatile bool preloadValid;
  volatile uint32_t preloadBase;
  volatile uint32_t preloadValidBytes;

  // Give-up tracking (main-loop-only, no volatile needed) - see
  // midi_seq_maintain() in the .cpp.
  uint32_t stallWatchPos;
  uint32_t stallStartMs; // 0 means "not currently tracking a stall"
};

struct MidiSequencer {
  FloppyFileHandle fileHandle; // this file's cluster chain, shared read-only by every track cursor
  uint16_t ticksPerQuarter;
  int trackCount;
  MidiTrackCursor tracks[MIDI_SEQ_MAX_TRACKS];

  uint32_t tempo; // microseconds per quarter note
  uint32_t lastTick;
  uint64_t lastTimeUs;

  // Per-channel Control Change 7 (Channel Volume), applied to each
  // NOTE_ON's velocity. Defaults to 127 until a real CC7 says otherwise.
  uint8_t channelVolume[16];

  bool pendingValid;
  MidiEvent pending; // next NOTE_ON/NOTE_OFF event to fire, already tick->time_us converted
};

// Opens entry's file handle and primes every track's first pending event.
// Returns false on a parse error or a disk error (check floppy_last_error()).
bool midi_seq_init(MidiSequencer *seq, const FloppyDirEntry &entry);

// If there's a next event, fills *out and returns true (does not consume -
// call midi_seq_advance() to move past it once its time has come).
bool midi_seq_peek(MidiSequencer *seq, MidiEvent *out);

// Consumes the current pending event and computes the next one.
// allowBlockingRead=true permits a synchronous disk read (only safe from
// the main loop, never the dispatch timer interrupt). allowBlockingRead=false
// never touches the floppy - if data isn't ready, that track is left
// without a pending event (not marked finished) and retried next call.
void midi_seq_advance(MidiSequencer *seq, bool allowBlockingRead);

// True only once every track is genuinely exhausted - unlike
// !midi_seq_peek(), which is also (indistinguishably) true during a
// transient stall. Only call this (and act on the result) from the main
// loop - acting on "finished" means loading the next track, which blocks.
bool midi_seq_all_tracks_finished(MidiSequencer *seq);

// Diagnostic: Serial-prints every track's cursor state.
void midi_seq_debug_dump(MidiSequencer *seq);

// Call frequently (e.g. every loop() iteration), independent of dispatch
// timing - tops up any track whose window is running low, and gives up on
// (marks finished) a track stuck retrying the same position for too long
// (see MIDI_SEQ_MAINTAIN_GIVE_UP_MS in the .cpp) rather than blocking
// playback forever.
void midi_seq_maintain(MidiSequencer *seq);

// True if any non-finished track has been stuck retrying the same preload
// position for at least minMs - a genuine buffer-starvation stall, not a
// false positive from an ordinary long note/rest (those never touch this
// state, since the buffer stays topped up regardless of note density).
// Intended for a caller to proactively silence current notes while
// waiting, instead of leaving a stuck note audibly droning.
bool midi_seq_any_track_stalled(MidiSequencer *seq, uint32_t minMs);
