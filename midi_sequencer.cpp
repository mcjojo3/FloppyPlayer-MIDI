// midi_sequencer.cpp - see midi_sequencer.h.
#include "midi_sequencer.h"
#include <Arduino.h>
#include <string.h>

// Internal-only pending-event markers, never surfaced via midi_seq_peek().
static const uint8_t MIDI_EVT_OTHER_INTERNAL = 2;
static const uint8_t MIDI_EVT_CC_INTERNAL = 201; // channel in pendingChannel, controller# in pendingA, value in pendingB
static const uint8_t MIDI_EVT_TEMPO_INTERNAL = 200;
static const uint8_t MIDI_EVT_PROGRAM_INTERNAL = 202; // channel in pendingChannel, program# in pendingA

// Loads up to MIDI_SEQ_HALF_WINDOW_BYTES starting at the sector containing
// requestedStart (window starts are always sector-aligned).
static bool loadWindowAt(MidiSequencer *seq, uint32_t requestedStart, uint8_t *buf, uint32_t *outBase, uint32_t *outValidBytes) {
  uint32_t alignedStart = (requestedStart / 512) * 512;
  *outBase = alignedStart;
  uint32_t fileSize = seq->fileHandle.fileSize;
  if (alignedStart >= fileSize) {
    *outValidBytes = 0;
    return true;
  }
  uint32_t remaining = fileSize - alignedStart;
  uint32_t toRead = remaining < MIDI_SEQ_HALF_WINDOW_BYTES ? remaining : MIDI_SEQ_HALF_WINDOW_BYTES;
  uint32_t offset = 0;
  while (offset < toRead) {
    static uint8_t sector[512]; // static: main-loop-only, never reentrant
    if (!floppy_read_file_sector(&seq->fileHandle, alignedStart + offset, sector)) return false;
    uint32_t chunk = toRead - offset < 512 ? toRead - offset : 512;
    memcpy(buf + offset, sector, chunk);
    offset += 512;
  }
  *outValidBytes = toRead;
  return true;
}

// Reads one byte at tc->pos, swapping to the preloaded half or (if
// allowBlockingRead) doing a synchronous load when pos moves past the
// active window. allowBlockingRead=false never touches the floppy: sets
// *stalled=true and returns false instead, so the caller retries later
// rather than treating this as end-of-track.
static bool nextByte(MidiSequencer *seq, MidiTrackCursor *tc, uint8_t *out, bool allowBlockingRead, bool *stalled) {
  if (tc->pos >= seq->fileHandle.fileSize) return false; // real end, not a stall

  // Snapshot the volatile active-window fields once - nothing but this
  // function ever writes them, so they can't change mid-call; re-reading
  // each on every use just forces redundant memory loads.
  uint32_t activeBase = tc->activeBase;
  uint32_t activeValidBytes = tc->activeValidBytes;
  int activeHalf = tc->activeHalf;

  if (tc->pos < activeBase || tc->pos >= activeBase + activeValidBytes) {
    if (tc->preloadValid && tc->pos >= tc->preloadBase && tc->pos < tc->preloadBase + tc->preloadValidBytes) {
      activeHalf = 1 - activeHalf;
      activeBase = tc->preloadBase;
      activeValidBytes = tc->preloadValidBytes;
      tc->activeHalf = activeHalf;
      tc->activeBase = activeBase;
      tc->activeValidBytes = activeValidBytes;
      tc->preloadValid = false;
    } else if (allowBlockingRead) {
      uint32_t base, validBytes;
      if (!loadWindowAt(seq, tc->pos, tc->half[activeHalf], &base, &validBytes)) return false; // real disk error, not a stall
      activeBase = base;
      activeValidBytes = validBytes;
      tc->activeBase = base;
      tc->activeValidBytes = validBytes;
      tc->preloadValid = false; // any old preload is now for the wrong position - discard it
    } else {
      *stalled = true;
      return false;
    }
    if (tc->pos >= activeBase + activeValidBytes) return false; // truly out of data - real end, not a stall
  }

  *out = tc->half[activeHalf][tc->pos - activeBase];
  tc->pos++;
  return true;
}

static bool readVarlen(MidiSequencer *seq, MidiTrackCursor *tc, uint32_t *outValue, bool allowBlockingRead, bool *stalled) {
  uint32_t value = 0;
  while (tc->pos < tc->end) {
    uint8_t byte;
    if (!nextByte(seq, tc, &byte, allowBlockingRead, stalled)) return false;
    value = (value << 7) | (byte & 0x7F);
    if (!(byte & 0x80)) {
      *outValue = value;
      return true;
    }
  }
  return false;
}

// Decodes this track's next event into tc->pending* (pendingValid=true), or
// sets tc->finished=true once exhausted. Surfaces NOTE_ON/NOTE_OFF/CC/OTHER/
// TEMPO_INTERNAL alike - advanceSequencer() decides what to do with each.
//
// A stall partway through decoding one event must not leave pos/absTick/
// runningStatus (or the double-buffer bookkeeping, which can swap mid-event)
// half-updated, so all of it is snapshotted at entry and restored on a stall -
// the decode looks like it never started, safe to retry.
static void advanceTrack(MidiSequencer *seq, MidiTrackCursor *tc, bool allowBlockingRead) {
  uint32_t savedPos = tc->pos;
  uint32_t savedAbsTick = tc->absTick;
  uint8_t savedRunningStatus = tc->runningStatus;
  bool savedHaveRunningStatus = tc->haveRunningStatus;
  int savedActiveHalf = tc->activeHalf;
  uint32_t savedActiveBase = tc->activeBase;
  uint32_t savedActiveValidBytes = tc->activeValidBytes;
  bool savedPreloadValid = tc->preloadValid;
  uint32_t savedPreloadBase = tc->preloadBase;
  uint32_t savedPreloadValidBytes = tc->preloadValidBytes;
  bool stalled = false;

  auto onFailure = [&]() {
    if (stalled) {
      tc->pos = savedPos;
      tc->absTick = savedAbsTick;
      tc->runningStatus = savedRunningStatus;
      tc->haveRunningStatus = savedHaveRunningStatus;
      tc->activeHalf = savedActiveHalf;
      tc->activeBase = savedActiveBase;
      tc->activeValidBytes = savedActiveValidBytes;
      tc->preloadValid = savedPreloadValid;
      tc->preloadBase = savedPreloadBase;
      tc->preloadValidBytes = savedPreloadValidBytes;
    } else {
      tc->finished = true;
    }
  };

  while (tc->pos < tc->end) {
    uint32_t delta;
    if (!readVarlen(seq, tc, &delta, allowBlockingRead, &stalled)) { onFailure(); return; }
    tc->absTick += delta;
    if (tc->pos >= tc->end) break;

    uint8_t statusOrData;
    if (!nextByte(seq, tc, &statusOrData, allowBlockingRead, &stalled)) { onFailure(); return; }
    uint8_t status;
    bool haveFirst = false;
    uint8_t firstData = 0;
    if (statusOrData < 0x80) {
      if (!tc->haveRunningStatus) { tc->finished = true; return; } // malformed data, not a stall
      status = tc->runningStatus;
      haveFirst = true;
      firstData = statusOrData;
    } else {
      status = statusOrData;
      if (status < 0xF0) {
        tc->runningStatus = status;
        tc->haveRunningStatus = true;
      }
    }

    if (status == 0xFF) {
      uint8_t metaType;
      if (!nextByte(seq, tc, &metaType, allowBlockingRead, &stalled)) { onFailure(); return; }
      uint32_t metaLen;
      if (!readVarlen(seq, tc, &metaLen, allowBlockingRead, &stalled)) { onFailure(); return; }
      if (metaType == 0x51 && metaLen == 3) {
        uint8_t b0, b1, b2;
        if (!nextByte(seq, tc, &b0, allowBlockingRead, &stalled) ||
            !nextByte(seq, tc, &b1, allowBlockingRead, &stalled) ||
            !nextByte(seq, tc, &b2, allowBlockingRead, &stalled)) { onFailure(); return; }
        tc->pendingTick = tc->absTick;
        tc->pendingType = MIDI_EVT_TEMPO_INTERNAL;
        tc->pendingChannel = b0;
        tc->pendingA = b1;
        tc->pendingB = b2;
        tc->pendingValid = true;
        return;
      }
      for (uint32_t i = 0; i < metaLen; i++) {
        uint8_t dummy;
        if (!nextByte(seq, tc, &dummy, allowBlockingRead, &stalled)) { onFailure(); return; }
      }
      tc->haveRunningStatus = false;
    } else if (status == 0xF0 || status == 0xF7) {
      uint32_t sysexLen;
      if (!readVarlen(seq, tc, &sysexLen, allowBlockingRead, &stalled)) { onFailure(); return; }
      for (uint32_t i = 0; i < sysexLen; i++) {
        uint8_t dummy;
        if (!nextByte(seq, tc, &dummy, allowBlockingRead, &stalled)) { onFailure(); return; }
      }
      tc->haveRunningStatus = false;
    } else {
      uint8_t eventType = status & 0xF0;
      uint8_t channel = status & 0x0F;
      if (eventType == 0xC0 || eventType == 0xD0) {
        uint8_t d1;
        if (haveFirst) {
          d1 = firstData;
        } else {
          if (!nextByte(seq, tc, &d1, allowBlockingRead, &stalled)) { onFailure(); return; }
        }
        tc->pendingTick = tc->absTick;
        tc->pendingType = (eventType == 0xC0) ? MIDI_EVT_PROGRAM_INTERNAL : MIDI_EVT_OTHER_INTERNAL;
        tc->pendingChannel = channel;
        tc->pendingA = d1;
        tc->pendingB = 0;
        tc->pendingValid = true;
        return;
      } else {
        uint8_t d1, d2;
        if (haveFirst) {
          d1 = firstData;
          if (!nextByte(seq, tc, &d2, allowBlockingRead, &stalled)) { onFailure(); return; }
        } else {
          if (!nextByte(seq, tc, &d1, allowBlockingRead, &stalled) ||
              !nextByte(seq, tc, &d2, allowBlockingRead, &stalled)) { onFailure(); return; }
        }
        uint8_t evType;
        if (eventType == 0x90 && d2 > 0) evType = MIDI_EVT_NOTE_ON;
        else if (eventType == 0x90 || eventType == 0x80) evType = MIDI_EVT_NOTE_OFF;
        else if (eventType == 0xB0) evType = MIDI_EVT_CC_INTERNAL; // d1=controller#, d2=value
        else evType = MIDI_EVT_OTHER_INTERNAL;
        tc->pendingTick = tc->absTick;
        tc->pendingType = evType;
        tc->pendingChannel = channel;
        tc->pendingA = d1;
        tc->pendingB = d2;
        tc->pendingValid = true;
        return;
      }
    }
  }
  tc->finished = true;
}

// K-way merge: finds the non-finished track with the smallest pending tick,
// applies the tick->microsecond conversion (updating tempo in strict global
// chronological order), and either surfaces it as the sequencer's next
// event (NOTE_ON/NOTE_OFF) or silently applies its effect (TEMPO_INTERNAL/CC)
// before looping to find the next candidate.
static void advanceSequencer(MidiSequencer *seq, bool allowBlockingRead) {
  while (true) {
    // Re-prime any track without a pending event (either never primed, or a
    // previous advanceTrack() stalled - safe to retry every pass since it
    // fully rolls back on a stall) and fold straight into the tick-minimum
    // search - one pass instead of two, since priming track i never affects
    // track j's tick.
    int bestIndex = -1;
    uint32_t bestTick = 0;
    for (int i = 0; i < seq->trackCount; i++) {
      MidiTrackCursor &tc = seq->tracks[i];
      if (!tc.finished && !tc.pendingValid) advanceTrack(seq, &tc, allowBlockingRead);
      if (tc.finished || !tc.pendingValid) continue;
      if (bestIndex == -1 || tc.pendingTick < bestTick) {
        bestIndex = i;
        bestTick = tc.pendingTick;
      }
    }
    if (bestIndex == -1) {
      seq->pendingValid = false;
      return;
    }

    MidiTrackCursor &tc = seq->tracks[bestIndex];
    uint32_t tick = tc.pendingTick;
    // A track that just un-stalled can report a tick behind seq->lastTick
    // (other tracks kept advancing while it was stuck) - clamp instead of
    // underflowing the uint32_t subtraction, which would corrupt lastTimeUs
    // with a multi-billion-microsecond jump.
    uint32_t deltaTicks = (tick > seq->lastTick) ? (tick - seq->lastTick) : 0;
    uint64_t deltaUs = (uint64_t)deltaTicks * seq->tempo / seq->ticksPerQuarter;
    seq->lastTimeUs += deltaUs;
    if (tick > seq->lastTick) seq->lastTick = tick;

    uint8_t type = tc.pendingType;
    uint8_t channel = tc.pendingChannel, a = tc.pendingA, b = tc.pendingB;

    if (type == MIDI_EVT_TEMPO_INTERNAL) {
      seq->tempo = ((uint32_t)channel << 16) | ((uint32_t)a << 8) | b;
      tc.pendingValid = false;
      advanceTrack(seq, &tc, allowBlockingRead);
      continue;
    }
    if (type == MIDI_EVT_CC_INTERNAL) {
      if (a == 7 && channel < 16) seq->channelVolume[channel] = b; // controller 7 = Channel Volume
      tc.pendingValid = false;
      advanceTrack(seq, &tc, allowBlockingRead);
      continue;
    }
    if (type == MIDI_EVT_PROGRAM_INTERNAL) {
      if (channel < 16) seq->channelProgram[channel] = a;
      tc.pendingValid = false;
      advanceTrack(seq, &tc, allowBlockingRead);
      continue;
    }
    if (type == MIDI_EVT_OTHER_INTERNAL) {
      tc.pendingValid = false;
      advanceTrack(seq, &tc, allowBlockingRead);
      continue;
    }

    seq->pending.time_us = (uint32_t)seq->lastTimeUs;
    seq->pending.type = type; // always NOTE_ON/NOTE_OFF here - internal markers already handled above
    seq->pending.channel = channel;
    seq->pending.note = a;
    // Scale by this channel's CC7 (Channel Volume) - GM files commonly set
    // this per channel to balance the mix. >>7 (divide by 128) instead of
    // /127 - off by at most 1/127 at the very top of the range, inaudible,
    // and avoids a divide with no hardware divider on this chip.
    seq->pending.velocity = (uint8_t)(((uint32_t)b * seq->channelVolume[channel < 16 ? channel : 0]) >> 7);
    seq->pending.program = seq->channelProgram[channel < 16 ? channel : 0];
    seq->pendingValid = true;
    tc.pendingValid = false;
    advanceTrack(seq, &tc, allowBlockingRead);
    return;
  }
}

bool midi_seq_init(MidiSequencer *seq, const FloppyDirEntry &entry) {
  seq->pendingValid = false;
  if (!floppy_open_file_handle(entry, &seq->fileHandle)) return false;
  uint32_t fileLen = seq->fileHandle.fileSize;
  if (fileLen < 14) return false;

  // Throwaway cursor for walking the file structure (MThd header, then each
  // MTrk chunk's 8-byte header) - never used for event bytes, so its own
  // preload/maintain logic is never invoked. Static: its window buffers are
  // too big for a plain stack local on this embedded target.
  static MidiTrackCursor scan;
  scan.pos = 0;
  scan.end = fileLen;
  scan.activeHalf = 0;
  scan.activeBase = 0;
  scan.activeValidBytes = 0;
  scan.preloadValid = false;

  uint8_t header[14];
  for (int i = 0; i < 14; i++) {
    if (!nextByte(seq, &scan, &header[i], true, nullptr)) return false;
  }
  if (header[0] != 'M' || header[1] != 'T' || header[2] != 'h' || header[3] != 'd') return false;
  uint32_t headerLen = ((uint32_t)header[4] << 24) | (header[5] << 16) | (header[6] << 8) | header[7];
  uint16_t ntrks = (header[10] << 8) | header[11];
  uint16_t division = (header[12] << 8) | header[13];
  if (division & 0x8000) return false;
  seq->ticksPerQuarter = division;
  if (ntrks == 0 || ntrks > MIDI_SEQ_MAX_TRACKS) return false;

  for (uint32_t i = 6; i < headerLen; i++) { // headerLen is normally exactly 6 (already consumed above)
    uint8_t dummy;
    if (!nextByte(seq, &scan, &dummy, true, nullptr)) return false;
  }

  seq->trackCount = ntrks;
  for (int t = 0; t < ntrks; t++) {
    uint8_t trackHeader[8];
    for (int i = 0; i < 8; i++) {
      if (!nextByte(seq, &scan, &trackHeader[i], true, nullptr)) return false;
    }
    if (!(trackHeader[0] == 'M' && trackHeader[1] == 'T' && trackHeader[2] == 'r' && trackHeader[3] == 'k')) return false;
    uint32_t trackLen = ((uint32_t)trackHeader[4] << 24) | (trackHeader[5] << 16) | (trackHeader[6] << 8) | trackHeader[7];
    uint32_t trackStart = scan.pos;
    uint32_t trackEnd = trackStart + trackLen;
    if (trackEnd > fileLen) return false;

    MidiTrackCursor &tc = seq->tracks[t];
    tc.pos = trackStart;
    tc.end = trackEnd;
    tc.absTick = 0;
    tc.runningStatus = 0;
    tc.haveRunningStatus = false;
    tc.finished = false;
    tc.pendingValid = false;
    tc.activeHalf = 0;
    tc.preloadValid = false;
    tc.stallWatchPos = trackStart;
    tc.stallStartMs = 0;
    uint32_t base, validBytes;
    if (!loadWindowAt(seq, trackStart, tc.half[0], &base, &validBytes)) return false;
    tc.activeBase = base;
    tc.activeValidBytes = validBytes;
    advanceTrack(seq, &tc, true);

    scan.pos = trackEnd; // jump the scan cursor past this track's event data to the next MTrk header
    scan.activeValidBytes = 0; // force scan's window to be reconsidered at the new position
    scan.preloadValid = false;
  }

  seq->tempo = 500000; // MIDI default: 120 BPM
  seq->lastTick = 0;
  seq->lastTimeUs = 0;
  for (int c = 0; c < 16; c++) seq->channelVolume[c] = 127; // full volume until a real CC7 says otherwise
  for (int c = 0; c < 16; c++) seq->channelProgram[c] = 0; // Acoustic Grand Piano until a real Program Change says otherwise
  advanceSequencer(seq, true);
  return true;
}

bool midi_seq_peek(MidiSequencer *seq, MidiEvent *out) {
  if (!seq->pendingValid) return false;
  *out = seq->pending;
  return true;
}

void midi_seq_advance(MidiSequencer *seq, bool allowBlockingRead) {
  advanceSequencer(seq, allowBlockingRead);
}

bool midi_seq_all_tracks_finished(MidiSequencer *seq) {
  for (int i = 0; i < seq->trackCount; i++) {
    if (!seq->tracks[i].finished) return false;
  }
  return true;
}

void midi_seq_debug_dump(MidiSequencer *seq) {
  Serial.print("midi_seq_debug_dump: trackCount=");
  Serial.print(seq->trackCount);
  Serial.print(" seq.pendingValid=");
  Serial.print(seq->pendingValid ? "true" : "false");
  Serial.print(" seq.pending.time_us=");
  Serial.print(seq->pendingValid ? seq->pending.time_us : 0);
  Serial.print(" tempo=");
  Serial.print(seq->tempo);
  Serial.print(" ticksPerQuarter=");
  Serial.print(seq->ticksPerQuarter);
  Serial.print(" lastTick=");
  Serial.print(seq->lastTick);
  Serial.print(" lastTimeUs=");
  Serial.println((uint32_t)seq->lastTimeUs);
  for (int i = 0; i < seq->trackCount; i++) {
    MidiTrackCursor &tc = seq->tracks[i];
    Serial.print("  track[");
    Serial.print(i);
    Serial.print("] finished=");
    Serial.print(tc.finished ? "Y" : "N");
    Serial.print(" pendingValid=");
    Serial.print(tc.pendingValid ? "Y" : "N");
    Serial.print(" pendingTick=");
    Serial.print(tc.pendingTick);
    Serial.print(" pos=");
    Serial.print(tc.pos);
    Serial.print(" end=");
    Serial.print(tc.end);
    Serial.print(" activeHalf=");
    Serial.print(tc.activeHalf);
    Serial.print(" activeBase=");
    Serial.print(tc.activeBase);
    Serial.print(" activeValidBytes=");
    Serial.print(tc.activeValidBytes);
    Serial.print(" preloadValid=");
    Serial.print(tc.preloadValid ? "Y" : "N");
    Serial.print(" preloadBase=");
    Serial.print(tc.preloadBase);
    Serial.print(" preloadValidBytes=");
    Serial.println(tc.preloadValidBytes);
  }
}

// 8s of no progress is roughly 4-8 real capture attempts (each already
// costs up to ~1-2s) - long enough to ride out a slow/fragmented read,
// short enough that a genuinely unrecoverable sector doesn't hang playback
// indefinitely.
static const uint32_t MIDI_SEQ_MAINTAIN_GIVE_UP_MS = 8000;

void midi_seq_maintain(MidiSequencer *seq) {
  for (int i = 0; i < seq->trackCount; i++) {
    MidiTrackCursor &tc = seq->tracks[i];
    if (tc.finished || tc.preloadValid) continue;

    uint32_t windowEnd = tc.activeBase + tc.activeValidBytes;
    if (windowEnd >= tc.end || windowEnd >= seq->fileHandle.fileSize) continue; // nothing more to preload

    // Starts preloading at just 10% consumed - some refills can take
    // several seconds (a fragmented cluster chain needing several
    // non-adjacent cylinder seeks), so an early trigger gives more of the
    // window's remaining playback time as lead time before the consumer
    // catches up and stalls.
    uint32_t consumedInActive = tc.pos - tc.activeBase;
    if (consumedInActive * 10 < tc.activeValidBytes) continue;

    // tc.pos can't move further while genuinely stuck here, so "unchanged
    // for a long time while we keep retrying" unambiguously means a stuck
    // read (as opposed to a long musical rest, which never reaches this
    // code path at all since the buffer stays topped up regardless of note
    // density).
    if (tc.stallWatchPos != tc.pos) {
      tc.stallWatchPos = tc.pos;
      tc.stallStartMs = millis();
    } else if (tc.stallStartMs != 0 && millis() - tc.stallStartMs > MIDI_SEQ_MAINTAIN_GIVE_UP_MS) {
      Serial.print("midi_seq_maintain: giving up on track stuck at pos=");
      Serial.print(tc.pos);
      Serial.print(" after ");
      Serial.print(MIDI_SEQ_MAINTAIN_GIVE_UP_MS);
      Serial.println("ms - marking finished so playback can move on");
      FloppyError err = floppy_last_error();
      if (err == FLOPPY_ERR_SECTOR_UNRECOVERABLE) {
        int cyl, head, sector;
        floppy_last_sector_failure(&cyl, &head, &sector);
        Serial.print("  last floppy error: unrecoverable sector cyl=");
        Serial.print(cyl);
        Serial.print(" head=");
        Serial.print(head);
        Serial.print(" sector=");
        Serial.println(sector);
      }
      tc.finished = true;
      continue;
    }

    int otherHalf = 1 - tc.activeHalf;
    uint32_t base, validBytes;
    if (!loadWindowAt(seq, windowEnd, tc.half[otherHalf], &base, &validBytes)) continue; // try again next call
    tc.preloadBase = base;
    tc.preloadValidBytes = validBytes;
    tc.preloadValid = true;
    tc.stallStartMs = 0; // succeeded - a future stall at a different pos starts its own fresh timer
  }
}

bool midi_seq_any_track_stalled(MidiSequencer *seq, uint32_t minMs) {
  for (int i = 0; i < seq->trackCount; i++) {
    MidiTrackCursor &tc = seq->tracks[i];
    if (tc.finished) continue;
    if (tc.stallStartMs != 0 && millis() - tc.stallStartMs >= minMs) return true;
  }
  return false;
}
