// RP2040 Floppy MIDI Player - see README.md for architecture/wiring.

#include "midi_sequencer.h"
#include "synth.h"
#include "floppy_fat12.h"
#include <PWMAudio.h>
#include <pico/time.h>
#include <string.h>
#include <math.h>

const int PIN_NEXT       = 12;
const int PIN_PREV       = 13;
const int PIN_PLAYPAUSE  = 14;
const int PIN_VOLUP      = 15;
const int PIN_VOLDOWN    = 17;

const int PIN_AUDIO_LEFT  = 22;
const int PIN_AUDIO_RIGHT = 23;

// Plain on/off switches, not momentary - no debounce needed.
const int PIN_LOOP_SWITCH = 16;     // grounded = repeat current track instead of auto-advance
const int PIN_AUTOPLAY_SWITCH = 2;  // grounded = start playing automatically after a disk scan

const unsigned long DEBOUNCE_MS = 30;
const unsigned long LONG_PRESS_MS = 600;

enum ButtonEvent { EVT_NONE, EVT_SHORT_PRESS, EVT_LONG_PRESS };

struct Button {
  int pin;
  bool lastRaw = HIGH;
  bool stableState = HIGH;
  unsigned long lastChangeTime = 0;
  unsigned long pressStartTime = 0;
  bool longPressFired = false;

  void begin(int p) {
    pin = p;
    pinMode(pin, INPUT_PULLUP);
  }

  ButtonEvent update() {
    bool raw = digitalRead(pin);
    unsigned long now = millis();

    if (raw != lastRaw) {
      lastChangeTime = now;
      lastRaw = raw;
    }

    ButtonEvent event = EVT_NONE;

    if (now - lastChangeTime > DEBOUNCE_MS && raw != stableState) {
      stableState = raw;
      if (stableState == LOW) {
        pressStartTime = now;
        longPressFired = false;
      } else if (!longPressFired) {
        event = EVT_SHORT_PRESS;
      }
    }

    if (stableState == LOW && !longPressFired && (now - pressStartTime > LONG_PRESS_MS)) {
      longPressFired = true;
      event = EVT_LONG_PRESS;
    }

    return event;
  }
};

Button btnNext, btnPrev, btnPlayPause, btnVolUp, btnVolDown;

#define MAX_TRACKS_PER_FOLDER 32
#define MAX_FOLDERS 4

struct Folder {
  char name[9];
  FloppyDirEntry tracks[MAX_TRACKS_PER_FOLDER];
  int trackCount;
};

Folder folders[MAX_FOLDERS];
int numFolders = 0;

int currentFolder = 0;
int currentTrack = 0; // 0-indexed within the current folder

// Cross-core scalars (Core 0 writes, Core 1's fillAudioBuffer() reads) - a
// plain read/write is atomic on this chip, so no locking needed; worst case
// Core 1 sees a one-iteration-stale value, which is inaudible.
volatile bool playing = false;
int volume = 1; // 0-10 scale

// Written by sendNoteCommand() (dispatch timer interrupt), read by
// checkDispatchWatchdog()/checkStallMute() on the main loop.
volatile uint32_t lastNoteCommandMs = 0;

// Root filenames encode play order as a numeric prefix in their original
// (pre-8.3-truncation) name, e.g. "10THEM~1" -> 10 - raw FAT directory
// order doesn't reflect this, so it's parsed explicitly.
int parseLeadingNumber(const char *name) {
  int n = 0;
  int i = 0;
  bool any = false;
  while (name[i] >= '0' && name[i] <= '9') {
    n = n * 10 + (name[i] - '0');
    any = true;
    i++;
  }
  return any ? n : 999999; // non-numeric names sort last
}

void sortTracksByLeadingNumber(FloppyDirEntry *tracks, int count) {
  for (int i = 1; i < count; i++) {
    FloppyDirEntry key = tracks[i];
    int keyNum = parseLeadingNumber(key.name);
    int j = i - 1;
    while (j >= 0 && parseLeadingNumber(tracks[j].name) > keyNum) {
      tracks[j + 1] = tracks[j];
      j--;
    }
    tracks[j + 1] = key;
  }
}

bool isMidFile(const FloppyDirEntry &e) {
  if (e.attr & 0x18) return false; // directory or volume label
  return strcmp(e.ext, "MID") == 0;
}

// Root becomes folder "ROOT"; any other root subdirectory containing at
// least one real .MID file becomes its own folder too (see README).
void buildPlaylist() {
  numFolders = 0;

  Folder &root = folders[numFolders];
  strncpy(root.name, "ROOT", sizeof(root.name));
  root.trackCount = 0;

  static FloppyDirEntry subdirs[MAX_FOLDERS]; // static: too big for the stack alongside subEntries/candidate below
  int subdirCount = 0;

  int rootCount = floppy_root_entry_count();
  for (int i = 0; i < rootCount; i++) {
    const FloppyDirEntry &e = floppy_root_entry(i);
    if (e.attr & 0x08) continue; // volume label
    if (e.attr & 0x10) {
      if (subdirCount < MAX_FOLDERS) subdirs[subdirCount++] = e;
      continue;
    }
    if (!isMidFile(e)) continue;
    if (root.trackCount < MAX_TRACKS_PER_FOLDER) {
      root.tracks[root.trackCount++] = e;
    }
  }
  sortTracksByLeadingNumber(root.tracks, root.trackCount);
  numFolders++;

  for (int s = 0; s < subdirCount && numFolders < MAX_FOLDERS; s++) {
    static FloppyDirEntry subEntries[MAX_TRACKS_PER_FOLDER]; // static: same stack-safety reasoning as subdirs above
    int subCount = floppy_read_subdirectory(subdirs[s], subEntries, MAX_TRACKS_PER_FOLDER);
    if (subCount <= 0) continue;

    static Folder candidate;
    candidate.trackCount = 0;
    for (int i = 0; i < subCount; i++) {
      if (!isMidFile(subEntries[i])) continue;
      if (candidate.trackCount < MAX_TRACKS_PER_FOLDER) {
        candidate.tracks[candidate.trackCount++] = subEntries[i];
      }
    }
    if (candidate.trackCount == 0) continue; // no music in here - skip silently

    sortTracksByLeadingNumber(candidate.tracks, candidate.trackCount);
    strncpy(candidate.name, subdirs[s].name, sizeof(candidate.name));
    folders[numFolders] = candidate;
    numFolders++;
  }
}

void printPlaylistSummary() {
  Serial.print("Playlist: ");
  for (int f = 0; f < numFolders; f++) {
    Serial.print(folders[f].name);
    Serial.print("(");
    Serial.print(folders[f].trackCount);
    Serial.print(") ");
  }
  Serial.println();
}

// Not folded into resetPlayback(): that's also called for ordinary track
// changes (next/prev/loop), where it must not override play/pause state.
void applyAutoplaySwitch() {
  if (digitalRead(PIN_AUTOPLAY_SWITCH) == LOW) playing = true;
}

// Separate from wall-clock time so pausing truly freezes song position
// (and voice envelopes) instead of the song racing ahead while paused.
uint32_t songElapsedBeforePauseUs = 0;
uint32_t playSegmentStartUs = 0;

// See checkStallClock() - lets a disk stall pause playSegmentStartUs's advance too.
const uint32_t CLOCK_STALL_THRESHOLD_MS = 20;
static bool clockPausedForStall = false;
static uint32_t stallPauseStartUs = 0;

MidiSequencer midiSeq;
// volatile: read every tick by the dispatch timer interrupt, written by
// loop() (loadCurrentTrack) - guards the ISR from touching midiSeq mid-rewrite
// during a track change.
volatile bool midiSeqValid = false;

uint32_t currentSongTimeUs() {
  if (playing) {
    return songElapsedBeforePauseUs + (micros() - playSegmentStartUs);
  }
  return songElapsedBeforePauseUs;
}

void printFloppyError() {
  switch (floppy_last_error()) {
    case FLOPPY_OK:
      Serial.println("  (no floppy error recorded - check the file's own format)");
      break;
    case FLOPPY_ERR_NOT_MOUNTED:
      Serial.println("  error: disk not mounted");
      break;
    case FLOPPY_ERR_BAD_BOOT_SECTOR:
      Serial.println("  error: boot sector unreadable, or not a recognizable FAT12 disk");
      break;
    case FLOPPY_ERR_NO_CLUSTERS:
      Serial.println("  error: this file/folder has no data (empty or corrupt directory entry)");
      break;
    case FLOPPY_ERR_TOO_MANY_CLUSTERS:
      Serial.println("  error: file is larger than this firmware's cluster-chain limit (FLOPPY_MAX_FILE_CLUSTERS)");
      break;
    case FLOPPY_ERR_SECTOR_UNRECOVERABLE: {
      int cyl, head, sector;
      floppy_last_sector_failure(&cyl, &head, &sector);
      if (head == -1) {
        Serial.print("  error: requested cylinder ");
        Serial.print(cyl);
        Serial.println(" is out of range for this disk - likely a corrupted cluster/LBA value upstream");
      } else {
        Serial.print("  error: unrecoverable sector at cylinder=");
        Serial.print(cyl);
        Serial.print(" head=");
        Serial.print(head);
        Serial.print(" sector=");
        Serial.println(sector);
      }
      break;
    }
    case FLOPPY_ERR_OUT_OF_RANGE:
      Serial.println("  error: read past the end of the file (corrupt size or cluster chain?)");
      break;
  }
}

void loadCurrentTrack() {
  midiSeqValid = false;
  if (numFolders == 0 || folders[currentFolder].trackCount == 0) {
    Serial.println("No track to load.");
    return;
  }
  const FloppyDirEntry &entry = folders[currentFolder].tracks[currentTrack];
  Serial.print("Loading ");
  Serial.print(entry.name);
  if (entry.ext[0]) { Serial.print("."); Serial.print(entry.ext); }
  Serial.print(" (");
  Serial.print(entry.size);
  Serial.println(" bytes)...");

  unsigned long start = millis();
  if (!midi_seq_init(&midiSeq, entry)) {
    Serial.println("  load failed:");
    if (floppy_last_error() == FLOPPY_OK) {
      Serial.println("  error: not a valid/supported MIDI file (bad MThd/MTrk header, unsupported SMPTE timing, or more tracks than MIDI_SEQ_MAX_TRACKS)");
    } else {
      printFloppyError();
    }
    return;
  }
  playSegmentStartUs = micros(); // before midiSeqValid, not after - avoids the ISR reading a stale clock baseline
  clockPausedForStall = false; // a stall from the previous track must not carry over onto this fresh baseline
  midiSeqValid = true;
  Serial.print("  loaded in ");
  Serial.print(millis() - start);
  Serial.print(" ms, ");
  Serial.print(midiSeq.trackCount);
  Serial.println(" track(s)");
}

// Core 1 owns all synth voice state - commands cross via a software ring
// buffer, since the hardware inter-core FIFO (only 8 entries) can overflow
// and silently drop commands during a dense chord.
// Encoding: bit31 set = control command (only ALL_NOTES_OFF exists);
// otherwise bit30=note-on/off, bits27-24=channel, bits23-16=note, bits15-8=velocity, bits7-0=GM program.
const uint32_t FIFO_CMD_ALL_NOTES_OFF = 0x80000000u;

#define NOTE_CMD_QUEUE_SIZE 128
static volatile uint32_t noteCmdQueue[NOTE_CMD_QUEUE_SIZE];
static volatile uint32_t noteCmdHead = 0; // producer-owned (Core 0 dispatch ISR)
static volatile uint32_t noteCmdTail = 0; // consumer-owned (Core 1 loop1())

// Lock-free SPSC push - safe on this chip's in-order cores without a memory barrier.
static inline bool noteCmdPush(uint32_t cmd) {
  uint32_t next = (noteCmdHead + 1) % NOTE_CMD_QUEUE_SIZE;
  if (next == noteCmdTail) return false; // full - vanishingly unlikely at this size
  noteCmdQueue[noteCmdHead] = cmd;
  noteCmdHead = next;
  return true;
}

void sendNoteCommand(bool isOn, uint8_t channel, uint8_t note, uint8_t velocity, uint8_t program) {
  uint32_t cmd = ((uint32_t)(isOn ? 1 : 0) << 30) | ((uint32_t)(channel & 0x0F) << 24) |
                 ((uint32_t)note << 16) | ((uint32_t)velocity << 8) | (uint32_t)program;
  noteCmdPush(cmd); // must not block - this runs from the dispatch ISR
}

void sendAllNotesOff() {
  while (!noteCmdPush(FIFO_CMD_ALL_NOTES_OFF)) {} // called from loop(), not the ISR - fine to spin briefly
}

void resetPlayback() {
  songElapsedBeforePauseUs = 0;
  sendAllNotesOff();
  loadCurrentTrack(); // sets playSegmentStartUs itself, after loading completes
  lastNoteCommandMs = millis(); // fresh baseline - a long intro rest shouldn't look like a stall
}

void printState() {
  Serial.print("Folder=");
  Serial.print(numFolders > 0 ? folders[currentFolder].name : "(none)");
  Serial.print(" Track=");
  Serial.print(currentTrack + 1);
  Serial.print("/");
  Serial.print(numFolders > 0 ? folders[currentFolder].trackCount : 0);
  Serial.print(" Playing=");
  Serial.print(playing ? "YES" : "NO");
  Serial.print(" Volume=");
  Serial.println(volume);
}

void nextTrack() {
  if (numFolders == 0) return;
  currentTrack++;
  if (currentTrack >= folders[currentFolder].trackCount) {
    currentTrack = 0;
    currentFolder = (currentFolder + 1) % numFolders;
  }
  resetPlayback();
  printState();
}

void prevTrack() {
  if (numFolders == 0) return;
  currentTrack--;
  if (currentTrack < 0) {
    currentFolder = (currentFolder - 1 + numFolders) % numFolders;
    currentTrack = folders[currentFolder].trackCount - 1;
  }
  resetPlayback();
  printState();
}

void nextFolder() {
  if (numFolders == 0) return;
  currentFolder = (currentFolder + 1) % numFolders;
  currentTrack = 0;
  resetPlayback();
  printState();
}

void prevFolder() {
  if (numFolders == 0) return;
  currentFolder = (currentFolder - 1 + numFolders) % numFolders;
  currentTrack = 0;
  resetPlayback();
  printState();
}

void togglePlayPause() {
  if (playing) {
    songElapsedBeforePauseUs += micros() - playSegmentStartUs;
    sendAllNotesOff(); // otherwise sustaining notes hang indefinitely with the clock frozen
  } else {
    playSegmentStartUs = micros();
  }
  playing = !playing;
  printState();
}

// -50..0dB logarithmic taper, precomputed once (no hardware FPU, powf() is
// too slow per-sample). Buttons and pot both write currentAmplitude - most recent wins.
const float VOLUME_FLOOR_DB = -50.0f;
float volumeTable[11];
volatile float currentAmplitude;

void buildVolumeTable() {
  volumeTable[0] = 0.0f;
  for (int v = 1; v <= 10; v++) {
    float dB = VOLUME_FLOOR_DB + (v / 10.0f) * -VOLUME_FLOOR_DB;
    volumeTable[v] = powf(10.0f, dB / 20.0f);
  }
  currentAmplitude = volumeTable[volume];
}

void volUp() {
  if (volume < 10) volume++;
  currentAmplitude = volumeTable[volume];
  printState();
}

void volDown() {
  if (volume > 0) volume--;
  currentAmplitude = volumeTable[volume];
  printState();
}

const int PIN_VOL_POT = 26;
int lastPotRaw = -1;

void setupVolumePot() {
  analogReadResolution(12); // RP2040's ADC is genuinely 12-bit, vs the classic-Arduino 10-bit default
}

// Same taper as the button table, driven by a continuous fraction.
// Only recomputes when the reading moves more than ADC noise would.
void updateVolumePot() {
  int raw = analogRead(PIN_VOL_POT);
  if (lastPotRaw != -1 && abs(raw - lastPotRaw) < 16) return;
  lastPotRaw = raw;
  if (raw < 16) {
    currentAmplitude = 0.0f;
    return;
  }
  float fraction = raw / 4095.0f;
  float dB = VOLUME_FLOOR_DB + fraction * -VOLUME_FLOOR_DB;
  currentAmplitude = powf(10.0f, dB / 20.0f);
}

const uint32_t AUDIO_SAMPLE_RATE = 11025;

PWMAudio pwmAudio(PIN_AUDIO_LEFT, true); // stereo: GPIO22=left, GPIO23=right

void setupAudio() {
  pwmAudio.setBuffers(8, 128); // slack for fillAudioBuffer() to catch up after a briefly-slow loop1() iteration
  pwmAudio.begin(AUDIO_SAMPLE_RATE);
}

// No `playing` gate here - that would itself pop to 0.0f on pause. Pausing
// relies on synth_all_notes_off()'s smooth release instead.
void fillAudioBuffer() {
  while (pwmAudio.availableForWrite() >= 2) {
    float s = synth_sample(micros());
    int32_t sample = (int32_t)(s * currentAmplitude * 32767.0f);
    if (sample < -32768) sample = -32768;
    if (sample > 32767) sample = 32767;
    pwmAudio.write((int16_t)sample); // left
    pwmAudio.write((int16_t)sample); // right - same signal, no panning yet
  }
}

// Runs on Core 1 - the only core allowed to touch synth's voice state.
void drainNoteCommands() {
  while (noteCmdTail != noteCmdHead) {
    uint32_t cmd = noteCmdQueue[noteCmdTail];
    noteCmdTail = (noteCmdTail + 1) % NOTE_CMD_QUEUE_SIZE;
    if (cmd == FIFO_CMD_ALL_NOTES_OFF) {
      synth_all_notes_off(micros());
      continue;
    }
    bool isOn = (cmd >> 30) & 1;
    uint8_t channel = (cmd >> 24) & 0x0F;
    uint8_t note = (cmd >> 16) & 0xFF;
    uint8_t velocity = (cmd >> 8) & 0xFF;
    uint8_t program = cmd & 0xFF;
    uint32_t now = micros();
    if (isOn) synth_note_on(channel, note, velocity, program, now);
    else synth_note_off(channel, note, now);
  }
}

// Runs from the dispatch timer interrupt - must never block, so always
// passes allowBlockingRead=false and never handles "song finished" (that's
// checkTrackFinished(), main-loop-only since it blocks on disk I/O).
void updateMidiDispatch() {
  if (!playing || !midiSeqValid) return;
  uint32_t songTimeUs = currentSongTimeUs();
  MidiEvent e;
  // Retry even with nothing pending - this is the only thing that re-primes a stalled track.
  if (!midi_seq_peek(&midiSeq, &e)) {
    midi_seq_advance(&midiSeq, false);
  }
  bool dispatchedAny = false;
  while (midi_seq_peek(&midiSeq, &e) && e.time_us <= songTimeUs) {
    if (e.channel != 9) { // GM percussion channel - no drum synthesis yet, so skip rather than mis-render as pitches
      sendNoteCommand(e.type == MIDI_EVT_NOTE_ON, e.channel, e.note, e.velocity, e.program);
      dispatchedAny = true;
    }
    midi_seq_advance(&midiSeq, false);
  }
  // Hoisted out of sendNoteCommand() - millis()/volatile write once per
  // tick instead of once per note, harmless since the watchdog only cares
  // about elapsed time at a 3-second granularity.
  if (dispatchedAny) lastNoteCommandMs = millis();
}

// midi_seq_all_tracks_finished() (not midi_seq_peek()) is the only
// unambiguous "song over" check - see midi_sequencer.h.
void checkTrackFinished() {
  if (!playing || !midiSeqValid) return;
  if (!midi_seq_all_tracks_finished(&midiSeq)) return;
  if (digitalRead(PIN_LOOP_SWITCH) == LOW) {
    resetPlayback();
  } else {
    nextTrack();
  }
}

// Diagnostic: if playing but no note has dispatched in a while, dump full
// sequencer state to help diagnose a stuck track.
const uint32_t DISPATCH_STALL_TIMEOUT_MS = 3000;
static bool dumpedForThisStall = false;

void checkDispatchWatchdog() {
  if (!playing || !midiSeqValid) { dumpedForThisStall = false; return; }
  if (millis() - lastNoteCommandMs < DISPATCH_STALL_TIMEOUT_MS) { dumpedForThisStall = false; return; }
  if (dumpedForThisStall) return;
  dumpedForThisStall = true;
  Serial.print("WATCHDOG: no note dispatched for ");
  Serial.print(DISPATCH_STALL_TIMEOUT_MS);
  Serial.print("ms while playing (songTimeUs=");
  Serial.print(currentSongTimeUs());
  Serial.println(") - dumping sequencer state:");
  midi_seq_debug_dump(&midiSeq);
}

// Mutes as soon as a genuine buffer-starvation stall is detected (see
// midi_seq_any_track_stalled()), so a stall is silent, not a droning note.
const uint32_t STALL_MUTE_MS = 500;
static bool mutedForStall = false;

void checkStallMute() {
  if (!playing || !midiSeqValid) { mutedForStall = false; return; }
  bool stalled = midi_seq_any_track_stalled(&midiSeq, STALL_MUTE_MS);
  if (stalled && !mutedForStall) {
    mutedForStall = true;
    sendAllNotesOff();
  } else if (!stalled) {
    mutedForStall = false;
  }
}

// Freezes the song clock the instant any track stalls, well before
// STALL_MUTE_MS - otherwise wall-clock time keeps accruing during a brief
// disk hiccup, and once it resolves, every now-overdue event dispatches in
// one rapid burst (an audible "speedup" right after the lag), even for
// stalls far too short to ever trigger the mute or watchdog.
void checkStallClock() {
  if (!playing || !midiSeqValid) { clockPausedForStall = false; return; }
  bool stalled = midi_seq_any_track_stalled(&midiSeq, CLOCK_STALL_THRESHOLD_MS);
  if (stalled && !clockPausedForStall) {
    clockPausedForStall = true;
    stallPauseStartUs = micros();
  } else if (!stalled && clockPausedForStall) {
    clockPausedForStall = false;
    playSegmentStartUs += micros() - stallPauseStartUs; // erase the paused span from the clock baseline
  }
}

// Polls DSKCHG for a disk swap - see floppy_fat12.h. Rate-limited so an
// empty drive isn't hammered with retries.
static unsigned long lastRemountAttemptMs = 0;
const unsigned long REMOUNT_RETRY_INTERVAL_MS = 1000;

void checkDiskChange() {
  if (!floppy_disk_change_asserted()) return;
  unsigned long now = millis();
  if (now - lastRemountAttemptMs < REMOUNT_RETRY_INTERVAL_MS) return;
  lastRemountAttemptMs = now;

  Serial.println("Disk change detected - remounting...");
  playing = false;
  midiSeqValid = false;
  numFolders = 0;
  sendAllNotesOff();

  floppy_motor_on();
  if (!floppy_remount()) {
    Serial.println("  no disk present, or remount failed:");
    printFloppyError();
    floppy_motor_off();
    return;
  }

  buildPlaylist();
  printPlaylistSummary();
  currentFolder = 0;
  currentTrack = 0;
  resetPlayback();
  applyAutoplaySwitch();
  printState();
}

// Retries mounting periodically if the disk never mounted, or mounted with
// nothing playable ("Track 1/0") - e.g. the drive motor not fully spun up
// yet on the very first read right after power-on.
static unsigned long lastEmptyPlaylistRetryMs = 0;
const unsigned long EMPTY_PLAYLIST_RETRY_INTERVAL_MS = 1000;

void checkEmptyPlaylist() {
  if (numFolders > 0) return;
  unsigned long now = millis();
  if (now - lastEmptyPlaylistRetryMs < EMPTY_PLAYLIST_RETRY_INTERVAL_MS) return;
  lastEmptyPlaylistRetryMs = now;

  Serial.println("No playable tracks - retrying mount...");
  floppy_motor_on();
  if (!floppy_remount()) {
    Serial.println("  still no disk, or mount failed:");
    printFloppyError();
    floppy_motor_off();
    return;
  }
  buildPlaylist();
  if (numFolders == 0) {
    Serial.println("  mounted, but still no playable .MID files found.");
    floppy_motor_off();
    return;
  }
  printPlaylistSummary();
  currentFolder = 0;
  currentTrack = 0;
  resetPlayback();
  applyAutoplaySwitch();
  printState();
}

// Runs on Core 0, same core as loop() - a same-core interrupt preempting
// loop(), not a cross-core handoff (see midi_sequencer.h).
static repeating_timer_t midiDispatchTimer;
static bool midiDispatchTick(repeating_timer_t *) {
  updateMidiDispatch();
  return true;
}

// ============================================================================
// Core 0: buttons, floppy disk I/O, playlist, MIDI dispatch.
// ============================================================================

void setup() {
  Serial.begin(115200);
  delay(500);

  btnNext.begin(PIN_NEXT);
  btnPrev.begin(PIN_PREV);
  btnPlayPause.begin(PIN_PLAYPAUSE);
  btnVolUp.begin(PIN_VOLUP);
  btnVolDown.begin(PIN_VOLDOWN);
  pinMode(PIN_LOOP_SWITCH, INPUT_PULLUP);
  pinMode(PIN_AUTOPLAY_SWITCH, INPUT_PULLUP);
  buildVolumeTable();
  setupVolumePot();

  Serial.println("Floppy MIDI Player");
  Serial.println("Initializing floppy interface...");
  if (!floppy_init()) {
    Serial.println("floppy_init() failed.");
  } else {
    Serial.println("Mounting disk...");
    if (!floppy_mount()) {
      Serial.println("floppy_mount() failed:");
      printFloppyError();
      floppy_motor_off();
    } else {
      buildPlaylist();
      printPlaylistSummary();
      resetPlayback();
      applyAutoplaySwitch();
    }
  }

  // 1ms period; negative interval times from the start of the previous
  // call so the period doesn't drift if a callback runs long.
  add_repeating_timer_us(-1000, midiDispatchTick, nullptr, &midiDispatchTimer);

  Serial.println("(short press = track/play-pause/volume, long press on NEXT/PREV = folder)");
  printState();
}

void loop() {
  ButtonEvent e;

  e = btnNext.update();
  if (e == EVT_SHORT_PRESS) nextTrack();
  else if (e == EVT_LONG_PRESS) nextFolder();

  e = btnPrev.update();
  if (e == EVT_SHORT_PRESS) prevTrack();
  else if (e == EVT_LONG_PRESS) prevFolder();

  e = btnPlayPause.update();
  if (e == EVT_SHORT_PRESS) togglePlayPause();

  e = btnVolUp.update();
  if (e == EVT_SHORT_PRESS) volUp();

  e = btnVolDown.update();
  if (e == EVT_SHORT_PRESS) volDown();

  updateVolumePot();
  checkDiskChange();
  checkEmptyPlaylist();
  if (midiSeqValid) midi_seq_maintain(&midiSeq);
  if (midiSeqValid) checkStallClock();
  if (midiSeqValid) checkStallMute();
  checkTrackFinished();
  checkDispatchWatchdog();
}

// ============================================================================
// Core 1: synth + audio output exclusively. Never touches the floppy or
// Serial, so a disk read on Core 0 can never cause an audio glitch.
// ============================================================================

void setup1() {
  synth_init(AUDIO_SAMPLE_RATE);
  setupAudio();
}

void loop1() {
  drainNoteCommands();
  fillAudioBuffer();
}
