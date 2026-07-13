// midi_sequencer.h - streaming SMF parser/scheduler (see README.md for the
// full design). Each track double-buffers two window halves, proactively
// refilling the inactive one past 10% consumed. Multi-track files use a
// live K-way merge by tick across per-track cursors. allowBlockingRead=false
// (used from the dispatch ISR) never touches the floppy - a missing byte
// reports a "stall" instead of blocking, retried once midi_seq_maintain() catches up.
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
  uint8_t program; // this channel's current GM program number (0-127), for timbre selection
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

  // Double-buffered read-ahead window. preloadValid is the lock-free guard:
  // producer (main loop) writes preloadBase/preloadValidBytes then sets
  // preloadValid=true LAST; consumer (dispatch ISR) only trusts them after
  // seeing it true. volatile so the compiler can't reorder these.
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
  // Per-channel Program Change (GM instrument number), applied to each
  // NOTE_ON for timbre selection. Defaults to 0 (Acoustic Grand Piano).
  uint8_t channelProgram[16];

  bool pendingValid;
  MidiEvent pending; // next NOTE_ON/NOTE_OFF event to fire, already tick->time_us converted
};

// Opens entry's file handle and primes every track's first pending event.
// Returns false on a parse error or a disk error (check floppy_last_error()).
bool midi_seq_init(MidiSequencer *seq, const FloppyDirEntry &entry);

// If there's a next event, fills *out and returns true (does not consume -
// call midi_seq_advance() to move past it once its time has come).
bool midi_seq_peek(MidiSequencer *seq, MidiEvent *out);

// Consumes the pending event and computes the next one. allowBlockingRead=true
// permits a synchronous disk read (main loop only, never the dispatch ISR);
// =false never touches the floppy, leaving an unready track to retry next call.
void midi_seq_advance(MidiSequencer *seq, bool allowBlockingRead);

// True only once every track is genuinely exhausted - unlike !midi_seq_peek(),
// which is also true during a transient stall. Main-loop only: acting on
// "finished" means loading the next track, which blocks.
bool midi_seq_all_tracks_finished(MidiSequencer *seq);

// Diagnostic: Serial-prints every track's cursor state.
void midi_seq_debug_dump(MidiSequencer *seq);

// Call every loop() iteration: tops up any track running low, and gives up
// on (marks finished) a track stuck at the same position too long (see
// MIDI_SEQ_MAINTAIN_GIVE_UP_MS in the .cpp) rather than blocking forever.
void midi_seq_maintain(MidiSequencer *seq);

// True if any track has been stuck at the same preload position for at
// least minMs - a genuine buffer-starvation stall, never a false positive
// from an ordinary long note/rest. Meant for a caller to mute proactively.
bool midi_seq_any_track_stalled(MidiSequencer *seq, uint32_t minMs);
