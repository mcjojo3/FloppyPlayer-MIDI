// RP2040 Floppy MIDI Player - see README.md for architecture/wiring.

#include "config.h"
#include "midi_sequencer.h"
#include "synth.h"
#include "floppy_fat12.h"
#include <PWMAudio.h>
#include <pico/time.h>
#include <string.h>
#include <math.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <EEPROM.h>
#include <hardware/pwm.h>

// 0.96" I2C OLED (SSD1306, 128x64) for on-the-go status/error display when
// no Serial monitor is hooked up. GPIO24/25 - only shared with the unused
// DVI port, so no conflict with anything this firmware actually uses.
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);
bool displayOk = false;

void setupDisplay() {
  Wire.setSDA(OLED_SDA_PIN);
  Wire.setSCL(OLED_SCL_PIN);
  Wire.begin();
  Wire.setClock(400000); // Fast Mode - a full-buffer redraw blocks Core 0, keep it as short as possible
  // Short I2C timeout (default is 1000ms) - a full-screen redraw is many
  // small chunked transactions internally, so a wedged bus (e.g. floppy
  // motor/stepper noise glitching the OLED mid-transaction) could otherwise
  // stack up many 1-second blocks in a row before flushDisplay() below ever
  // gets a chance to notice and give up.
  Wire.setTimeout(50);
  displayOk = display.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  if (!displayOk) {
    Serial.println("OLED init failed (check wiring/I2C address)");
    return;
  }
  display.clearDisplay();
  flushDisplay();
}

// display.display() wrapper - if the I2C bus ever times out (wedged/glitched
// display), stop trying to write to it instead of hitting the same wall on
// every subsequent redraw and starving loop() of time to run everything else.
void flushDisplay() {
  uint32_t start = millis();
  display.display();
  uint32_t elapsed = millis() - start;
  // A clean Fast Mode I2C redraw should take ~20-30ms - anything far past
  // that means Wire.setTimeout(50) is firing repeatedly (one bad chunk
  // costs 50ms, and Adafruit_SSD1306 doesn't abort the rest of the buffer
  // on a single failed chunk) rather than the bus staying wedged forever.
  if (elapsed > 100) {
    Serial.print("flushDisplay() took ");
    Serial.print(elapsed);
    Serial.println("ms");
  }
  if (Wire.getTimeoutFlag()) {
    Wire.clearTimeoutFlag();
    displayOk = false;
    Serial.println("OLED I2C timeout - display disabled for the rest of this session");
  }
}

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

Button btnNext, btnPrev, btnPlayPause, btnVolUp, btnVolDown, btnScreen;

struct Folder {
  char name[9];
  FloppyDirEntry tracks[MAX_TRACKS_PER_FOLDER];
  int trackCount;
};

Folder folders[MAX_FOLDERS];
int numFolders = 0;

int currentFolder = 0;
int currentTrack = 0; // 0-indexed within the current folder

// Snapshot of midi_seq_give_up_count() at the moment the current track was
// loaded - lets checkTrackFinished() tell "song genuinely ended" (respect
// Loop) apart from "track was abandoned as unreadable" (always skip onward,
// even with Loop on - otherwise a bad track loops on itself forever).
uint32_t giveUpCountAtTrackLoad = 0;

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
  if (autoplaySwitchOn()) playing = true;
}

// Separate from wall-clock time so pausing truly freezes song position
// (and voice envelopes) instead of the song racing ahead while paused.
uint32_t songElapsedBeforePauseUs = 0;
uint32_t playSegmentStartUs = 0;

// See checkStallClock() - lets a disk stall pause playSegmentStartUs's advance too.
static bool clockPausedForStall = false;
static uint32_t stallPauseStartUs = 0;

// See checkDispatchWatchdog() - cumulative since boot, shown on the OLED status line.
uint32_t watchdogFireCount = 0;

// 0..1, whichever control (buttons or pot) was used most recently - shown
// on the OLED as a %, since the discrete button `volume` alone goes stale
// the moment the pot is touched (only Core 0 reads this, no volatile needed).
float currentVolumeFraction = 0.1f;

// Toggled by a spare button - see drawUserScreen()/drawTechScreen().
enum DisplayScreen { SCREEN_USER, SCREEN_TECH };
DisplayScreen currentScreen = SCREEN_USER;

// Loop/Autoplay/Shuffle used to be dedicated grounded switches (3 GPIOs).
// Now they're on-screen config, persisted in flash-emulated EEPROM so they
// survive a reboot the same way a physical switch's position would.
bool cfgLoop = false;
bool cfgAutoplay = false;
bool cfgShuffle = false;
bool cfgSkipBadTracks = true; // matches the give-up-and-skip behavior that was previously unconditional
bool cfgDimScreen = false;

// Adafruit_SSD1306::dim(true) sets contrast all the way to 0 - unreadably
// dark, not an actual dim - so send the contrast command directly instead
// with a moderate value. dim(false) is fine for "restore" since it already
// knows the panel's real full-brightness contrast.
void applyDimScreen() {
  if (!displayOk) return;
  if (cfgDimScreen) {
    display.ssd1306_command(SSD1306_SETCONTRAST);
    display.ssd1306_command(20);
  } else {
    display.dim(false);
  }
}

#define CONFIG_EEPROM_SIZE 2
#define CONFIG_EEPROM_MAGIC 0xA5 // distinguishes "saved before" from blank/erased flash

void loadConfig() {
  EEPROM.begin(CONFIG_EEPROM_SIZE);
  if (EEPROM.read(0) != CONFIG_EEPROM_MAGIC) return; // never saved - keep the defaults above
  uint8_t flags = EEPROM.read(1);
  cfgLoop = flags & 0x01;
  cfgAutoplay = flags & 0x02;
  cfgShuffle = flags & 0x04;
  cfgSkipBadTracks = !(flags & 0x08); // inverted: old saves never touched this bit, so it reads as enabled
  midi_seq_set_skip_bad_tracks(cfgSkipBadTracks);
  cfgDimScreen = flags & 0x10;
  applyDimScreen();
}

void saveConfig() {
  // EEPROM.commit() is a blocking flash erase+write, and arduino-pico's EEPROM
  // library has to halt Core 1 for it via rp2040.idleOtherCore() - both cores
  // execute from the same physical flash over XIP, which can't be read
  // mid-erase, so Core 1 gets parked with interrupts masked for the whole
  // operation. That's exactly where PWMAudio's DMA-complete IRQ lives - it's
  // the thing that reprograms each ping-pong buffer's read address once its
  // predecessor drains. With that IRQ unable to fire, the two DMA channels
  // just keep re-triggering each other from whatever they were last loaded,
  // audible as a loop for the whole freeze - confirmed by testing without
  // the mute below: the loop noise came right back. Disabling the PWM
  // slice's counter for the duration silences that (the DMA can't reach the
  // pin either way), at the cost of a small click on each edge from the
  // pin snapping to a fixed level - much smaller than the alternative, and
  // now only paid once per menu visit (see saveConfigIfNeeded()) rather
  // than once per toggle.
  int sliceL = pwm_gpio_to_slice_num(PIN_AUDIO_LEFT);
  int sliceR = pwm_gpio_to_slice_num(PIN_AUDIO_RIGHT);
  pwm_set_enabled(sliceL, false);
  if (sliceR != sliceL) pwm_set_enabled(sliceR, false);

  uint8_t flags = (cfgLoop ? 0x01 : 0) | (cfgAutoplay ? 0x02 : 0) | (cfgShuffle ? 0x04 : 0) |
                  (cfgSkipBadTracks ? 0 : 0x08) | // inverted so old saves (bit always 0) default to enabled
                  (cfgDimScreen ? 0x10 : 0);
  EEPROM.write(0, CONFIG_EEPROM_MAGIC);
  EEPROM.write(1, flags);
  EEPROM.commit();

  pwm_set_enabled(sliceL, true);
  if (sliceR != sliceL) pwm_set_enabled(sliceR, true);
}

bool configDirty = false; // set on any toggle, cleared once actually flushed to flash

void saveConfigIfNeeded() {
  if (!configDirty) return;
  saveConfig();
  configDirty = false;
}

// Long-press the screen button to enter/exit. While active, Next/Prev move
// the selection and Play/Pause toggles it, instead of their normal jobs.
bool inMenuMode = false;
int menuSelection = 0;
bool menuPausedPlayback = false; // so we only resume on exit if the menu is what paused it
#define MENU_ITEM_COUNT 6 // Loop, Autoplay, Shuffle, Skip Bad, Dim Screen, Exit

// Last floppy error's short type, persisted here so the technical screen
// can still show what it was after the transient showDisplayError() message
// has long since been overwritten by normal status updates.
char lastErrorSummary[16] = "none";

// Diagnostics for the "skip feels unresponsive/several presses stack up"
// investigation - longest gap ever seen between consecutive loop()
// iterations (directly shows how long Core 0 got stuck in some blocking
// call, instead of guessing), and how many Next/Prev short-presses the
// firmware actually registered (compare against how many times you
// physically pressed it - a fast tap during any blocking call, not just a
// track load, can land entirely inside the gap and never be seen by the
// debounce logic at all).
uint32_t maxLoopGapMs = 0;
static uint32_t lastLoopMs = 0;
uint32_t nextPressCount = 0;
uint32_t prevPressCount = 0;

// Bit per MIDI channel (0-15) that's had a NOTE_ON dispatched this track -
// written from the dispatch ISR (updateMidiDispatch()), so volatile.
// Reset when a new track loads.
volatile uint16_t channelsUsedMask = 0;

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

// Short, screen-sized version of the same error - shown on the OLED
// alongside the fuller Serial message below.
void showDisplayError(const char *line1, const char *line2 = "") {
  if (!displayOk) return;
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println("ERROR");
  display.println(line1);
  display.println(line2);
  flushDisplay();
}

void printFloppyError() {
  switch (floppy_last_error()) {
    case FLOPPY_OK:
      Serial.println("  (no floppy error recorded - check the file's own format)");
      showDisplayError("no error recorded", "check file format");
      snprintf(lastErrorSummary, sizeof(lastErrorSummary), "none/file fmt");
      break;
    case FLOPPY_ERR_NOT_MOUNTED:
      Serial.println("  error: disk not mounted");
      showDisplayError("disk not mounted");
      snprintf(lastErrorSummary, sizeof(lastErrorSummary), "not mounted");
      break;
    case FLOPPY_ERR_BAD_BOOT_SECTOR:
      Serial.println("  error: boot sector unreadable, or not a recognizable FAT12 disk");
      showDisplayError("bad boot sector", "not FAT12?");
      snprintf(lastErrorSummary, sizeof(lastErrorSummary), "bad boot sect");
      break;
    case FLOPPY_ERR_NO_CLUSTERS:
      Serial.println("  error: this file/folder has no data (empty or corrupt directory entry)");
      showDisplayError("empty/corrupt", "directory entry");
      snprintf(lastErrorSummary, sizeof(lastErrorSummary), "no clusters");
      break;
    case FLOPPY_ERR_TOO_MANY_CLUSTERS:
      Serial.println("  error: file is larger than this firmware's cluster-chain limit (FLOPPY_MAX_FILE_CLUSTERS)");
      showDisplayError("file too large", "(cluster limit)");
      snprintf(lastErrorSummary, sizeof(lastErrorSummary), "too many clus");
      break;
    case FLOPPY_ERR_SECTOR_UNRECOVERABLE: {
      int cyl, head, sector;
      floppy_last_sector_failure(&cyl, &head, &sector);
      char line1[22], line2[22];
      if (head == -1) {
        Serial.print("  error: requested cylinder ");
        Serial.print(cyl);
        Serial.println(" is out of range for this disk - likely a corrupted cluster/LBA value upstream");
        snprintf(line1, sizeof(line1), "cyl %d out of range", cyl);
        line2[0] = '\0';
        snprintf(lastErrorSummary, sizeof(lastErrorSummary), "cyl %d range", cyl);
      } else {
        Serial.print("  error: unrecoverable sector at cylinder=");
        Serial.print(cyl);
        Serial.print(" head=");
        Serial.print(head);
        Serial.print(" sector=");
        Serial.println(sector);
        snprintf(line1, sizeof(line1), "bad sector c%d h%d", cyl, head);
        snprintf(line2, sizeof(line2), "s%d", sector);
        snprintf(lastErrorSummary, sizeof(lastErrorSummary), "bad c%dh%ds%d", cyl, head, sector);
      }
      showDisplayError(line1, line2);
      break;
    }
    case FLOPPY_ERR_OUT_OF_RANGE:
      Serial.println("  error: read past the end of the file (corrupt size or cluster chain?)");
      showDisplayError("read past EOF", "(corrupt chain?)");
      snprintf(lastErrorSummary, sizeof(lastErrorSummary), "past EOF");
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

  if (displayOk) {
    // Shown only while this blocking mount runs - whichever printState() call
    // the caller makes once loadCurrentTrack() returns repaints normal status.
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);
    display.println("Reading disk...");
    display.print(folders[currentFolder].name);
    display.print(" ");
    display.print(currentTrack + 1);
    display.print("/");
    display.println(folders[currentFolder].trackCount);
    display.print(entry.name);
    if (entry.ext[0]) { display.print("."); display.print(entry.ext); }
    flushDisplay();
  }

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
  channelsUsedMask = 0;
  midiSeqValid = true;
  giveUpCountAtTrackLoad = midi_seq_give_up_count();
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

// Print works for both Serial and the OLED (Adafruit_GFX derives from it) -
// one implementation instead of duplicating the name+ext formatting.
void printTrackName(Print &out) {
  if (numFolders == 0) return;
  const FloppyDirEntry &entry = folders[currentFolder].tracks[currentTrack];
  out.print(entry.name);
  if (entry.ext[0]) { out.print("."); out.print(entry.ext); }
}

bool shuffleEnabled() { return cfgShuffle; }
bool autoplaySwitchOn() { return cfgAutoplay; }
bool loopSwitchOn() { return cfgLoop; }

// Big, unmissable state word filling the OLED's top ~16px - on the common
// two-tone 0.96" panels that strip is a different (usually yellow) color
// from the body, so this stays legible even with the body crowded with
// stats below it.
void drawStateHeader() {
  display.setTextSize(2);
  display.setCursor(0, 0);
  if (!playing) {
    display.print("PAUSED");
  } else if (midiSeqValid && midi_seq_any_track_stalled(&midiSeq, CLOCK_STALL_THRESHOLD_MS)) {
    display.print("STALLED");
  } else {
    display.print("PLAYING");
  }
  display.setTextSize(1);
}

// Casual "what's playing" view - identity, volume, elapsed time. Loop/
// Autoplay/Shuffle/Skip Bad status used to be shown here too, but they're all
// visible in the config menu now (long-press Screen), so this stays uncluttered.
void drawUserScreen() {
  display.setCursor(0, 16);
  display.print(numFolders > 0 ? folders[currentFolder].name : "(none)");
  display.print(" ");
  display.print(currentTrack + 1);
  display.print("/");
  display.println(numFolders > 0 ? folders[currentFolder].trackCount : 0);
  printTrackName(display);
  display.println();
  display.print("Vol "); display.print((int)(currentVolumeFraction * 100.0f)); display.println("%");
  if (midiSeqValid) {
    uint32_t elapsedS = (uint32_t)(currentSongTimeUs() / 1000000);
    display.print("Time "); display.print(elapsedS / 60); display.print(":");
    if (elapsedS % 60 < 10) display.print("0");
    display.println(elapsedS % 60);
  }
}

// Debug view - as much as fits: activity counters, file structure, channel
// usage, live memory/uptime, and the last error's short type (persists here
// after showDisplayError()'s transient message has been drawn over).
void drawTechScreen() {
  display.setCursor(0, 16);
  display.print(numFolders > 0 ? folders[currentFolder].name : "(none)");
  display.print(" ");
  display.print(currentTrack + 1);
  display.print("/");
  display.println(numFolders > 0 ? folders[currentFolder].trackCount : 0);
  display.print("R "); display.print(floppy_read_count());
  display.print(" WD "); display.print(watchdogFireCount);
  display.print(" Sk "); display.println(midi_seq_give_up_count());
  display.print("Nx "); display.print(nextPressCount);
  display.print(" Pv "); display.print(prevPressCount);
  display.print(" Gap "); display.print(maxLoopGapMs); display.println("ms");
  if (midiSeqValid) {
    display.print("BPM "); display.print(midiSeq.tempo > 0 ? (int)(60000000.0f / midiSeq.tempo) : 0);
    display.print(" Trk "); display.print(midiSeq.trackCount);
    display.print(" Ch0x"); display.println(channelsUsedMask, HEX);
  } else {
    display.println();
  }
  display.print("RAM "); display.print(rp2040.getFreeHeap());
  display.print(" Up "); display.print(millis() / 1000); display.println("s");
  display.print("Err:"); display.println(lastErrorSummary);
}

// Shared header, then whichever screen the spare button last selected.
// Shared by printState() (every discrete change) and updateLiveDisplay() (throttled).
void drawFullStatus() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  drawStateHeader();
  if (currentScreen == SCREEN_USER) drawUserScreen();
  else drawTechScreen();
  flushDisplay();
}

// On-screen config menu - see inMenuMode/menuSelection. Next/Prev move the
// selection, Play/Pause toggles it (or exits, on the last item).
void drawMenu() {
  if (!displayOk) return;
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.println("CONFIG");
  display.setTextSize(1);
  display.setCursor(0, 16);

  const char *labels[5] = {"Loop", "Autoplay", "Shuffle", "Skip Bad", "Dim Scrn"};
  bool values[5] = {cfgLoop, cfgAutoplay, cfgShuffle, cfgSkipBadTracks, cfgDimScreen};
  for (int i = 0; i < 5; i++) {
    display.print(menuSelection == i ? "> " : "  ");
    display.print(labels[i]);
    display.print(": ");
    display.println(values[i] ? "ON" : "off");
  }
  display.print(menuSelection == 5 ? "> " : "  ");
  display.println("Exit");
  flushDisplay();
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

  if (!displayOk) return;
  if (inMenuMode) drawMenu(); // don't fight the config menu - just keep it current instead
  else drawFullStatus();
}

void nextTrack() {
  if (numFolders == 0) return;
  if (shuffleEnabled() && folders[currentFolder].trackCount > 1) {
    int newTrack;
    do {
      newTrack = random(folders[currentFolder].trackCount);
    } while (newTrack == currentTrack); // avoid immediately repeating the same track
    currentTrack = newTrack;
  } else {
    currentTrack++;
    if (currentTrack >= folders[currentFolder].trackCount) {
      currentTrack = 0;
      currentFolder = (currentFolder + 1) % numFolders;
    }
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
float volumeTable[11];
volatile float currentAmplitude;

void buildVolumeTable() {
  volumeTable[0] = 0.0f;
  for (int v = 1; v <= 10; v++) {
    float dB = VOLUME_FLOOR_DB + (v / 10.0f) * -VOLUME_FLOOR_DB;
    volumeTable[v] = powf(10.0f, dB / 20.0f);
  }
  currentAmplitude = volumeTable[volume];
  currentVolumeFraction = volume / 10.0f;
}

// TODO(pending removal?): the pot alone already gives full continuous
// control - these buttons are redundant with it now. Not removed yet, just
// flagged, since PIN_VOLUP/PIN_VOLDOWN (GPIO15/17) would free up 2 pins.
void volUp() {
  if (volume < 10) volume++;
  currentAmplitude = volumeTable[volume];
  currentVolumeFraction = volume / 10.0f;
  printState();
}

void volDown() {
  if (volume > 0) volume--;
  currentAmplitude = volumeTable[volume];
  currentVolumeFraction = volume / 10.0f;
  printState();
}

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
    currentVolumeFraction = 0.0f;
    return;
  }
  float fraction = raw / 4095.0f;
  currentVolumeFraction = fraction;
  float dB = VOLUME_FLOOR_DB + fraction * -VOLUME_FLOOR_DB;
  currentAmplitude = powf(10.0f, dB / 20.0f);
}

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
      if (e.type == MIDI_EVT_NOTE_ON) channelsUsedMask |= (1u << e.channel);
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
  bool gaveUpOnThisTrack = midi_seq_give_up_count() != giveUpCountAtTrackLoad;
  if (loopSwitchOn() && !gaveUpOnThisTrack) {
    resetPlayback();
  } else {
    nextTrack();
  }
}

// Diagnostic: if playing but no note has dispatched in a while, dump full
// sequencer state to help diagnose a stuck track.
static bool dumpedForThisStall = false;

void checkDispatchWatchdog() {
  if (!playing || !midiSeqValid) { dumpedForThisStall = false; return; }
  if (millis() - lastNoteCommandMs < DISPATCH_STALL_TIMEOUT_MS) { dumpedForThisStall = false; return; }
  if (dumpedForThisStall) return;
  dumpedForThisStall = true;
  watchdogFireCount++;
  Serial.print("WATCHDOG: no note dispatched for ");
  Serial.print(DISPATCH_STALL_TIMEOUT_MS);
  Serial.print("ms while playing (songTimeUs=");
  Serial.print(currentSongTimeUs());
  Serial.println(") - dumping sequencer state:");
  midi_seq_debug_dump(&midiSeq);
}

// Throttled, not called every loop() iteration - a full OLED redraw blocks
// Core 0 for ~20-30ms even at Fast Mode I2C, so this only runs a few times
// a second, not continuously. Shows the same live stall state that drives
// checkStallClock()/checkStallMute(), plus counters that only make sense
// as a running total rather than a one-off printState() snapshot.
static uint32_t lastLiveDisplayMs = 0;

void updateLiveDisplay() {
  if (!displayOk || !playing || !midiSeqValid || inMenuMode) return; // don't fight the config menu
  uint32_t now = millis();
  if (now - lastLiveDisplayMs < LIVE_DISPLAY_INTERVAL_MS) return;
  lastLiveDisplayMs = now;
  drawFullStatus();
}

// Mutes as soon as a genuine buffer-starvation stall is detected (see
// midi_seq_any_track_stalled()), so a stall is silent, not a droning note.
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
  randomSeed(micros()); // for shuffle mode - no floating analog pin available for better entropy
  setupDisplay();

  btnNext.begin(PIN_NEXT);
  btnPrev.begin(PIN_PREV);
  btnPlayPause.begin(PIN_PLAYPAUSE);
  btnVolUp.begin(PIN_VOLUP);
  btnVolDown.begin(PIN_VOLDOWN);
  btnScreen.begin(PIN_SCREEN_BUTTON);
  floppy_set_skip_pins(PIN_NEXT, PIN_PREV); // let a stuck cylinder retry bail out early on a track-change press
  // Loop/Autoplay/Shuffle are on-screen config now (long-press the screen
  // button) instead of dedicated grounded switches - see loadConfig().
  loadConfig();
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
  uint32_t nowLoopMs = millis();
  if (lastLoopMs != 0) {
    uint32_t gap = nowLoopMs - lastLoopMs;
    if (gap > maxLoopGapMs) maxLoopGapMs = gap;
    // Logged (not just tracked as a max) so it's timestamped and can be
    // correlated against the "loaded in Xms" prints already there - lets
    // us tell whether a stall matches a single load, or several stacking up.
    if (gap > 200) {
      Serial.print("loop() stalled for ");
      Serial.print(gap);
      Serial.println("ms");
    }
  }
  lastLoopMs = nowLoopMs;

  // Buttons are always polled every iteration regardless of mode, so the
  // debounce state machine's timing stays consistent - only what an event
  // *does* changes between normal play and the config menu.
  ButtonEvent eNext = btnNext.update();
  ButtonEvent ePrev = btnPrev.update();
  ButtonEvent ePlayPause = btnPlayPause.update();
  ButtonEvent eScreen = btnScreen.update();

  if (eScreen == EVT_LONG_PRESS) {
    inMenuMode = !inMenuMode;
    if (inMenuMode) {
      menuSelection = 0;
      // Dispatch only checks `playing`, so without this the song keeps firing
      // new notes the whole time the menu's open, and a setting toggle's flash
      // write (see saveConfig()) freezes Core 1 mid-note instead of mid-silence.
      menuPausedPlayback = playing;
      if (playing) {
        togglePlayPause();
        // One-time cost, paid only when the menu actually interrupted playback:
        // gives fillAudioBuffer() time to push real silence all the way through
        // the DMA pipeline before any setting toggle can trigger saveConfig()'s
        // flash write - see the comment there for why that write needs the
        // buffers already silent rather than being muted itself.
        delay(250);
      }
    } else {
      saveConfigIfNeeded(); // flush any toggles from this visit before resuming
      if (menuPausedPlayback) {
        togglePlayPause(); // resume right where the menu paused it
        menuPausedPlayback = false;
      }
    }
    if (displayOk) { if (inMenuMode) drawMenu(); else drawFullStatus(); }
  } else if (eScreen == EVT_SHORT_PRESS && !inMenuMode) {
    currentScreen = (currentScreen == SCREEN_USER) ? SCREEN_TECH : SCREEN_USER;
    if (displayOk) drawFullStatus(); // redraw immediately - don't wait for the next throttled tick
  }

  if (inMenuMode) {
    // Prev moves the selection forward/down, Next moves it back/up - flipped
    // from track navigation, since that's the direction that matches this layout.
    if (ePrev == EVT_SHORT_PRESS) { menuSelection = (menuSelection + 1) % MENU_ITEM_COUNT; drawMenu(); }
    if (eNext == EVT_SHORT_PRESS) { menuSelection = (menuSelection - 1 + MENU_ITEM_COUNT) % MENU_ITEM_COUNT; drawMenu(); }
    if (ePlayPause == EVT_SHORT_PRESS) {
      if (menuSelection == 0) { cfgLoop = !cfgLoop; configDirty = true; }
      else if (menuSelection == 1) { cfgAutoplay = !cfgAutoplay; configDirty = true; }
      else if (menuSelection == 2) { cfgShuffle = !cfgShuffle; configDirty = true; }
      else if (menuSelection == 3) { cfgSkipBadTracks = !cfgSkipBadTracks; midi_seq_set_skip_bad_tracks(cfgSkipBadTracks); configDirty = true; }
      else if (menuSelection == 4) { cfgDimScreen = !cfgDimScreen; applyDimScreen(); configDirty = true; }
      else {
        inMenuMode = false;
        saveConfigIfNeeded(); // only actually hits flash once, here, however many settings changed this visit
        if (menuPausedPlayback) { togglePlayPause(); menuPausedPlayback = false; }
        if (displayOk) drawFullStatus();
      }
      if (inMenuMode) drawMenu();
    }
  } else {
    if (eNext == EVT_SHORT_PRESS) { nextPressCount++; nextTrack(); }
    else if (eNext == EVT_LONG_PRESS) nextFolder();

    if (ePrev == EVT_SHORT_PRESS) { prevPressCount++; prevTrack(); }
    else if (ePrev == EVT_LONG_PRESS) prevFolder();

    if (ePlayPause == EVT_SHORT_PRESS) togglePlayPause();
  }

  ButtonEvent e = btnVolUp.update();
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
  updateLiveDisplay();
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
