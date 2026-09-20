// floppy_fat12.cpp - see floppy_fat12.h. __not_in_flash_func() keeps the
// capture/decode hot path in RAM, so it never waits on flash.
#include "config.h"
#include "floppy_fat12.h"
#include <Arduino.h>
#include <hardware/clocks.h>
#include <hardware/gpio.h>
#include <hardware/pio.h>
#include <pico/platform.h>
#include <string.h>

const int pinDriveSelect = 0;
const int pinMotorEnable = 1;
const int pinDirection   = 4;
const int pinStep        = 5;
const int pinSideSelect  = 8;
const int pinTrack00     = 9;
const int pinIndex       = 10;
const int pinReadData    = 11;
const int pinDiskChange  = 27; // DSKCHG, ribbon pin 34 - open-collector like the inputs above

const int NUM_CYLINDERS = 80;

static const uint16_t fluxread[] = {
    0x0041, 0x00c3, 0x0000, 0x0044, 0x01c3, 0x4001, 0x402f, 0x0040,
};
static const pio_program_t fluxread_struct = {
    .instructions = fluxread,
    .length = sizeof(fluxread) / sizeof(fluxread[0]),
    .origin = -1,
};

static PIO capturePio;
static uint captureSm;
static uint captureOffset;

static void setupFluxPio() {
  pio_gpio_init(pio0, pinReadData);
  pio_gpio_init(pio0, pinIndex);
  gpio_pull_up(pinIndex);

  capturePio = pio0;
  captureOffset = pio_add_program(capturePio, &fluxread_struct);
  captureSm = pio_claim_unused_sm(capturePio, true);

  pio_sm_config c = pio_get_default_sm_config();
  sm_config_set_wrap(&c, captureOffset, captureOffset + fluxread_struct.length - 1);
  sm_config_set_jmp_pin(&c, pinReadData);
  sm_config_set_in_pins(&c, pinIndex);
  sm_config_set_in_shift(&c, true, true, 32);
  sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);

  float div = (float)clock_get_hz(clk_sys) / (3 * 24e6);
  sm_config_set_clkdiv(&c, div);

  pio_sm_init(capturePio, captureSm, captureOffset, &c);
}

static uint16_t fifoHalf = 0;
static bool fifoHalfValid = false;

static inline bool __not_in_flash_func(fifoDataAvailable)() {
  return fifoHalfValid || !pio_sm_is_rx_fifo_empty(capturePio, captureSm);
}

static inline uint16_t __not_in_flash_func(fifoRead)() {
  if (fifoHalfValid) {
    fifoHalfValid = false;
    return fifoHalf;
  }
  uint32_t value = pio_sm_get_blocking(capturePio, captureSm);
  fifoHalf = value >> 16;
  fifoHalfValid = true;
  return value & 0xffff;
}

static void stepOnce(bool outward) {
  digitalWrite(pinDirection, outward ? HIGH : LOW);
  delayMicroseconds(10);
  digitalWrite(pinStep, LOW);
  delayMicroseconds(10);
  digitalWrite(pinStep, HIGH);
  delay(6);
}

// True if Track00 actually went low within the step budget.
static bool homeToTrack0() {
  digitalWrite(pinDirection, HIGH);
  delayMicroseconds(10);
  int steps = 0;
  while (digitalRead(pinTrack00) == HIGH && steps < 100) {
    stepOnce(true);
    steps++;
  }
  return digitalRead(pinTrack00) == LOW;
}

// HIGH = head 0, LOW = head 1 (inverted relative to the signal name).
static void selectHead(int head) {
  digitalWrite(pinSideSelect, head == 0 ? HIGH : LOW);
  delay(2);
}

static int currentCyl = 0;

// Diagnostics on DBG_SERIAL; marginal disks trigger them constantly.
#define FLOPPY_DIAGNOSTIC_LOG 0

static void seekToCylinder(int target) {
  if (target == currentCyl) return;
#if FLOPPY_DIAGNOSTIC_LOG
  // A back-and-forth pattern here is the file's own fragmentation.
  DBG_SERIAL.print("floppy: seek ");
  DBG_SERIAL.print(currentCyl);
  DBG_SERIAL.print(" -> ");
  DBG_SERIAL.println(target);
#endif
  if (target > currentCyl) {
    for (int i = 0; i < target - currentCyl; i++) stepOnce(false); // inward = higher cylinder
  } else {
    for (int i = 0; i < currentCyl - target; i++) stepOnce(true);
  }
  currentCyl = target;
}

// -- MFM decode, streamed as transitions arrive (raw deltas wouldn't fit in RAM) --

#define MAX_CELLBITS 300000u // headroom above one revolution's worth of cell-bits
static uint8_t cellBits[MAX_CELLBITS / 8];
static uint32_t cellBitCount;

static inline int __not_in_flash_func(getBit)(uint32_t pos) {
  return (cellBits[pos >> 3] >> (7 - (pos & 7))) & 1;
}

static inline void __not_in_flash_func(pushBit)(int bit) {
  if (cellBitCount >= MAX_CELLBITS) return; // defensive cap, shouldn't happen in practice
  uint32_t byteIdx = cellBitCount >> 3;
  uint8_t bitIdx = 7 - (cellBitCount & 7);
  if (bit) cellBits[byteIdx] |= (1 << bitIdx);
  else cellBits[byteIdx] &= ~(1 << bitIdx);
  cellBitCount++;
}

// Sync mark 0x4489 at the MFM cell-bit level, 3x for the standard IBM sync field.
static const uint64_t SINGLE_PATTERN_48 = 0x4489ULL;
static const uint64_t SYNC_PATTERN_48   = 0x448944894489ULL;
static const int SYNC_TOLERANCE = 3;

#define MAX_CANDIDATES 350 // headroom above one track's worth of sync-mark candidates
static uint32_t candidatePositions[MAX_CANDIDATES];
// Only one 0x4489 mark matched: the IDAM starts 16 cell-bits on, not 48.
static bool candidateSinglePattern[MAX_CANDIDATES];
static int candidateCount;

static inline void __not_in_flash_func(emitBit)(int bit) {
  pushBit(bit);
}

// Two 32-bit popcounts: the inputs are 48-bit, so a 64-bit one wastes cycles.
static inline int __not_in_flash_func(hammingDistance48)(uint64_t a, uint64_t b) {
  uint64_t x = a ^ b;
  return __builtin_popcount((uint32_t)x) + __builtin_popcount((uint32_t)(x >> 32));
}

// A post-pass: the capture loop can't afford a software popcount per bit.
static void __not_in_flash_func(findSyncCandidates)() {
  candidateCount = 0;
  uint64_t window = 0;
  for (uint32_t pos = 0; pos < cellBitCount; pos++) {
    window = (window << 1) | (uint64_t)getBit(pos);
    if (pos + 1 >= 48) {
      uint64_t window48 = window & 0xFFFFFFFFFFFFULL;
      int mismatches = hammingDistance48(window48, SYNC_PATTERN_48);
      if (mismatches <= SYNC_TOLERANCE && candidateCount < MAX_CANDIDATES) {
        candidateSinglePattern[candidateCount] = false;
        candidatePositions[candidateCount++] = pos + 1 - 48;
      }
    }
    if (pos + 1 >= 16) {
      uint64_t window16 = window & 0xFFFFULL;
      if (window16 == SINGLE_PATTERN_48 && candidateCount < MAX_CANDIDATES) {
        candidateSinglePattern[candidateCount] = true;
        candidatePositions[candidateCount++] = pos + 1 - 16;
      }
    }
  }
}

// Gap of n half-cells is (n-1) zero-bits then a 1.
static inline void __not_in_flash_func(emitUnit)(int n) {
  for (int i = 0; i < n - 1; i++) emitBit(0);
  emitBit(1);
}

static const int MAX_PLAUSIBLE_DELTA = 200;

// Splits a gap >4 units into valid 2/3/4 parts (a trailing 1 turns 4+1 into 2+3).
static void __not_in_flash_func(decomposeAndEmit)(int delta) {
  if (delta > MAX_PLAUSIBLE_DELTA || delta <= 0) return; // skip silently
  int parts[64];
  int n = 0;
  int remaining = delta;
  while (remaining > 4 && n < 62) {
    parts[n++] = 4;
    remaining -= 4;
  }
  if (remaining == 1) {
    if (n > 0) {
      n--;
      parts[n++] = 2;
      parts[n++] = 3;
    } else {
      parts[n++] = 2;
    }
  } else if (remaining > 0) {
    parts[n++] = remaining;
  }
  for (int i = 0; i < n; i++) emitUnit(parts[i]);
}

static inline void __not_in_flash_func(emitMergedUnit)(int unit) {
  if (unit == 2 || unit == 3 || unit == 4) emitUnit(unit);
  else decomposeAndEmit(unit);
}

// A unit <2 merges into the next one; flushPendingUnit() emits the tail.
static bool havePendingUnit;
static int pendingUnit;

// Banker's rounding, not (raw+12)/24: one wrong unit misaligns everything after it.
static inline int __not_in_flash_func(roundHalfToEven24)(int32_t raw) {
  int32_t q = raw / 24;
  int32_t r = raw % 24;
  if (r < 12) return q;
  if (r > 12) return q + 1;
  return (q % 2 == 0) ? q : q + 1;
}

static inline void __not_in_flash_func(feedRawDelta)(int32_t rawDelta) {
  int unit = roundHalfToEven24(rawDelta);
  if (!havePendingUnit) {
    pendingUnit = unit;
    havePendingUnit = true;
    return;
  }
  if (pendingUnit < 2) {
    emitMergedUnit(pendingUnit + unit);
    havePendingUnit = false;
  } else {
    emitMergedUnit(pendingUnit);
    pendingUnit = unit;
    havePendingUnit = true;
  }
}

static inline void __not_in_flash_func(flushPendingUnit)() {
  if (havePendingUnit) {
    emitMergedUnit(pendingUnit);
    havePendingUnit = false;
  }
}

// -- byte and CRC decode over a captured revolution (no timing constraint) --

static bool __not_in_flash_func(decodeByteAt)(uint32_t pos, uint8_t *outByte) {
  if (pos + 16 > cellBitCount) return false;
  uint8_t v = 0;
  for (int i = 0; i < 8; i++) {
    v = (v << 1) | getBit(pos + 1 + i * 2);
  }
  *outByte = v;
  return true;
}

static bool __not_in_flash_func(decodeBytesAt)(uint32_t pos, int n, uint8_t *out) {
  for (int b = 0; b < n; b++) {
    if (!decodeByteAt(pos + (uint32_t)b * 16, &out[b])) return false;
  }
  return true;
}

// Table-driven CRC16-CCITT - it runs on every IDAM candidate and sector.
static uint16_t crc16Table[256];

static void initCrc16Table() {
  for (int i = 0; i < 256; i++) {
    uint16_t crc = (uint16_t)i << 8;
    for (int b = 0; b < 8; b++) {
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : (crc << 1);
    }
    crc16Table[i] = crc;
  }
}

static uint16_t __not_in_flash_func(crc16_ccitt)(const uint8_t *data, int len) {
  uint16_t crc = 0xFFFF;
  for (int i = 0; i < len; i++) {
    crc = (crc << 8) ^ crc16Table[((crc >> 8) ^ data[i]) & 0xFF];
  }
  return crc;
}

struct IdamResult {
  uint8_t cyl, head, sector, size;
  uint32_t bitAfterIdam;
};

static bool __not_in_flash_func(tryDecodeIdam)(uint32_t syncPos, bool singlePattern, IdamResult *out) {
  uint32_t after = syncPos + (singlePattern ? 16 : 48);
  uint8_t fields[7];
  if (!decodeBytesAt(after, 7, fields)) return false;
  if (fields[0] != 0xFE) return false;
  uint8_t crcBuf[8] = {0xA1, 0xA1, 0xA1, fields[0], fields[1], fields[2], fields[3], fields[4]};
  uint16_t computed = crc16_ccitt(crcBuf, 8);
  uint16_t expected = ((uint16_t)fields[5] << 8) | fields[6];
  if (computed != expected) return false;
  out->cyl = fields[1];
  out->head = fields[2];
  out->sector = fields[3];
  out->size = fields[4];
  out->bitAfterIdam = after + 7 * 16;
  return true;
}

static const int DAM_SEARCH_RANGE = 600;
static const int DAM_SYNC_TOLERANCE = 3;

static bool __not_in_flash_func(findDam)(uint32_t searchStart, uint8_t *outMark, uint32_t *outDataStart) {
  int best = -1;
  uint32_t bestPos = 0;
  uint32_t searchEnd = searchStart + DAM_SEARCH_RANGE;
  if (searchEnd + 48 > cellBitCount) {
    if (cellBitCount < 48) return false;
    searchEnd = cellBitCount - 48;
  }
  uint64_t window = 0;
  for (uint32_t i = searchStart; i < searchStart + 48 && i < cellBitCount; i++) {
    window = (window << 1) | (uint64_t)getBit(i);
  }
  for (uint32_t pos = searchStart; pos <= searchEnd; pos++) {
    if (pos != searchStart) {
      window = ((window << 1) | (uint64_t)getBit(pos + 47)) & 0xFFFFFFFFFFFFULL;
    } else {
      window &= 0xFFFFFFFFFFFFULL;
    }
    int mismatches = hammingDistance48(window, SYNC_PATTERN_48);
    if (mismatches <= DAM_SYNC_TOLERANCE && (best == -1 || mismatches < best)) {
      best = mismatches;
      bestPos = pos;
    }
  }
  if (best == -1) return false;
  uint32_t after = bestPos + 48;
  uint8_t markByte;
  if (!decodeByteAt(after, &markByte)) return false;
  if (markByte != 0xFB && markByte != 0xF8) return false;
  *outMark = markByte;
  *outDataStart = after + 16;
  return true;
}

static bool __not_in_flash_func(decodeDamPayload)(uint8_t mark, uint32_t dataStart, uint8_t *outData512) {
  static uint8_t payload[514]; // static: deep, non-reentrant call chain
  if (!decodeBytesAt(dataStart, 514, payload)) return false;
  static uint8_t crcBuf[4 + 512];
  crcBuf[0] = 0xA1; crcBuf[1] = 0xA1; crcBuf[2] = 0xA1; crcBuf[3] = mark;
  memcpy(crcBuf + 4, payload, 512);
  uint16_t computed = crc16_ccitt(crcBuf, 4 + 512);
  uint16_t expected = ((uint16_t)payload[512] << 8) | payload[513];
  if (computed != expected) return false;
  memcpy(outData512, payload, 512);
  return true;
}

// -- per-cylinder sector cache (LRU) --

static const int SECTORS_PER_TRACK = 18;
static const int NUM_HEADS = 2;

static uint8_t sectorCache[NUM_CACHE_SLOTS][NUM_HEADS][SECTORS_PER_TRACK][512];
static bool sectorPresent[NUM_CACHE_SLOTS][NUM_HEADS][SECTORS_PER_TRACK];
static int cachedCyl[NUM_CACHE_SLOTS];
static uint32_t slotLastUsed[NUM_CACHE_SLOTS];
static uint32_t cacheUseCounter;

// Visits in a row that captured nothing; at the cap it's skipped until remount.
static uint8_t cylinderTotalFailStreak[NUM_CYLINDERS];

// Recaptures in a row still missing a sector - separate, so one bad sector
// doesn't get a mostly good cylinder abandoned.
static uint8_t cylinderPartialMissStreak[NUM_CYLINDERS];

static bool cylinderCaptureGiveUp[NUM_CYLINDERS];

// Only a fully clean capture resets the partial-miss streak.
static bool cylinderSlotFullyGood[NUM_CACHE_SLOTS];

// IDAMs agreeing on an unexpected cylinder (a lost step). Logged only.
static int g_seekMismatchCyl = -1;
static int g_seekMismatchAgreeCount = 0;
#define SEEK_MISMATCH_CONFIRM_COUNT 6

static void invalidateSectorCache() {
  for (int s = 0; s < NUM_CACHE_SLOTS; s++) { cachedCyl[s] = -1; slotLastUsed[s] = 0; cylinderSlotFullyGood[s] = false; }
  cacheUseCounter = 0;
  for (int c = 0; c < NUM_CYLINDERS; c++) { cylinderTotalFailStreak[c] = 0; cylinderPartialMissStreak[c] = 0; cylinderCaptureGiveUp[c] = false; }
}

static FloppyError g_lastError = FLOPPY_OK;

FloppyError floppy_last_error() { return g_lastError; }

static uint32_t lastTransitionCount;

// Captures one revolution. *outNoIndexAtAll means no disk, not worth retrying.
static bool __not_in_flash_func(captureOneRevolutionToCellbits)(bool *outNoIndexAtAll = nullptr) {
  if (outNoIndexAtAll) *outNoIndexAtAll = false;
  cellBitCount = 0;
  havePendingUnit = false;
  lastTransitionCount = 0;

  pio_sm_clear_fifos(capturePio, captureSm);
  pio_sm_exec(capturePio, captureSm, captureOffset);
  pio_sm_restart(capturePio, captureSm);
  fifoHalfValid = false;

  uint32_t waitStart = millis();
  while (gpio_get(pinIndex) == 0) { // wait for idle-high
    if (millis() - waitStart > INDEX_WAIT_TIMEOUT_MS) {
      if (outNoIndexAtAll) *outNoIndexAtAll = true;
#if FLOPPY_DIAGNOSTIC_LOG
      DBG_SERIAL.println("floppy: INDEX never went idle-high - drive reports no rotation at all (motor/clamping/INDEX sensor or wiring - not a media/read problem).");
#endif
      return false;
    }
  }
  waitStart = millis();
  while (gpio_get(pinIndex) != 0) { // wait for falling edge
    if (millis() - waitStart > INDEX_WAIT_TIMEOUT_MS) {
      if (outNoIndexAtAll) *outNoIndexAtAll = true;
#if FLOPPY_DIAGNOSTIC_LOG
      DBG_SERIAL.println("floppy: INDEX went idle-high but never pulsed - drive reports no rotation at all (motor/clamping/INDEX sensor or wiring - not a media/read problem).");
#endif
      return false;
    }
  }

  pio_sm_set_enabled(capturePio, captureSm, true);

  // pio_sm_get_blocking() has no timeout - a pulled disk would hang here.
  uint32_t firstWordStart = millis();
  while (!fifoDataAvailable()) {
    if (millis() - firstWordStart > INDEX_WAIT_TIMEOUT_MS) {
      pio_sm_set_enabled(capturePio, captureSm, false);
      return false;
    }
  }

  uint16_t last = fifoRead();
  bool lastIndex = gpio_get(pinIndex);
  uint32_t revStart = millis(); // catches INDEX stopping mid-revolution

  while (true) {
    bool nowIndex = gpio_get(pinIndex);
    if (!nowIndex && lastIndex) break;
    lastIndex = nowIndex;

    if (!fifoDataAvailable()) {
      if (millis() - revStart > INDEX_WAIT_TIMEOUT_MS) {
        pio_sm_set_enabled(capturePio, captureSm, false);
        return false;
      }
      continue;
    }
    uint16_t data = fifoRead();
    int32_t delta = (uint16_t)(last - data) >> 1; // wraps mod 65536, then halves - no branch needed
    last = data;

    feedRawDelta(delta);
    lastTransitionCount++;

    if (cellBitCount >= MAX_CELLBITS) break; // defensive - shouldn't happen, see header budget
  }

  pio_sm_set_enabled(capturePio, captureSm, false);
  flushPendingUnit();
  findSyncCandidates();
  return true;
}

// Per-capture trace on DBG_SERIAL. Off: serial I/O in this hot path disturbs capture.
#define FLOPPY_VERBOSE_CAPTURE_LOG 0
static int diagIdamCrcOk, diagDamFound, diagDamCrcOk;

// Adds good sectors to the slot; earlier attempts' sectors are kept.
static void __not_in_flash_func(decodeCurrentTrackIntoCache)(int slot, int cyl, int head) {
  diagIdamCrcOk = 0;
  diagDamFound = 0;
  diagDamCrcOk = 0;

  for (int c = 0; c < candidateCount; c++) {
    IdamResult idam;
    if (!tryDecodeIdam(candidatePositions[c], candidateSinglePattern[c], &idam)) continue;
    diagIdamCrcOk++;
    if (idam.sector < 1 || idam.sector > SECTORS_PER_TRACK) continue;
    if (idam.head != head) continue;
    if (idam.cyl != cyl) {
      if (idam.cyl == g_seekMismatchCyl) g_seekMismatchAgreeCount++;
      else { g_seekMismatchCyl = idam.cyl; g_seekMismatchAgreeCount = 1; }
#if FLOPPY_DIAGNOSTIC_LOG
      DBG_SERIAL.print("floppy: asked for cyl=");
      DBG_SERIAL.print(cyl);
      DBG_SERIAL.print(" head=");
      DBG_SERIAL.print(head);
      DBG_SERIAL.print(" but a CRC-valid IDAM says cyl=");
      DBG_SERIAL.print(idam.cyl);
      DBG_SERIAL.print(" (agree count for that value: ");
      DBG_SERIAL.print(g_seekMismatchAgreeCount);
      DBG_SERIAL.println(")");
#endif
      continue; // head is on a different cylinder than expected
    }

    uint8_t mark;
    uint32_t dataStart;
    if (!findDam(idam.bitAfterIdam, &mark, &dataStart)) continue;
    diagDamFound++;

    static uint8_t data[512];
    if (!decodeDamPayload(mark, dataStart, data)) continue;
    diagDamCrcOk++;

    memcpy(sectorCache[slot][head][idam.sector - 1], data, 512);
    sectorPresent[slot][head][idam.sector - 1] = true;
  }

#if FLOPPY_VERBOSE_CAPTURE_LOG
  int presentCount = 0;
  for (int s = 0; s < SECTORS_PER_TRACK; s++) if (sectorPresent[slot][head][s]) presentCount++;
  DBG_SERIAL.print("    head=");
  DBG_SERIAL.print(head);
  DBG_SERIAL.print(": ");
  DBG_SERIAL.print(lastTransitionCount);
  DBG_SERIAL.print(" transitions, ");
  DBG_SERIAL.print(cellBitCount);
  DBG_SERIAL.print(" cellbits, ");
  DBG_SERIAL.print(candidateCount);
  DBG_SERIAL.print(" sync candidates, ");
  DBG_SERIAL.print(diagIdamCrcOk);
  DBG_SERIAL.print(" IDAM CRC-ok, ");
  DBG_SERIAL.print(diagDamFound);
  DBG_SERIAL.print(" DAM found, ");
  DBG_SERIAL.print(diagDamCrcOk);
  DBG_SERIAL.print(" DAM CRC-ok, ");
  DBG_SERIAL.print(presentCount);
  DBG_SERIAL.println("/18 sectors recovered");
#endif
}

// Drive Select stays on when the motor stops, so DSKCHG stays readable.
static bool motorOn = true; // floppy_init() starts it
static uint32_t lastActivityMs = 0;

static void spinUp() {
  lastActivityMs = millis();
  if (motorOn) return;
  digitalWrite(pinMotorEnable, LOW);
  delay(600); // spindle up to speed
  motorOn = true;
}

void floppy_idle() {
  if (motorOn && millis() - lastActivityMs > FLOPPY_MOTOR_IDLE_MS) {
    digitalWrite(pinMotorEnable, HIGH);
    motorOn = false;
  }
}

// Slot holding cyl's sectors, capturing them if needed; -1 on failure.
static int ensureCylinderCached(int cyl) {
  lastActivityMs = millis();
  if (cyl < 0 || cyl >= NUM_CYLINDERS) {
    g_lastError = FLOPPY_ERR_SECTOR_UNRECOVERABLE;
    return -1;
  }

  for (int slot = 0; slot < NUM_CACHE_SLOTS; slot++) {
    if (cachedCyl[slot] == cyl) {
      slotLastUsed[slot] = ++cacheUseCounter;
      return slot;
    }
  }

  if (cylinderCaptureGiveUp[cyl]) {
    g_lastError = FLOPPY_ERR_SECTOR_UNRECOVERABLE;
    return -1;
  }

  int slot = -1;
  for (int s = 0; s < NUM_CACHE_SLOTS; s++) {
    if (cachedCyl[s] == -1) { slot = s; break; }
  }
  if (slot == -1) {
    slot = 0;
    for (int s = 1; s < NUM_CACHE_SLOTS; s++)
      if (slotLastUsed[s] < slotLastUsed[slot]) slot = s;
  }


  spinUp();
  seekToCylinder(cyl);
  g_seekMismatchCyl = -1;
  g_seekMismatchAgreeCount = 0;
  // Reset once per cylinder visit, not per attempt - see decodeCurrentTrackIntoCache().
  for (int h = 0; h < NUM_HEADS; h++)
    for (int s = 0; s < SECTORS_PER_TRACK; s++)
      sectorPresent[slot][h][s] = false;

  bool everCaptured = false; // any attempt completed a capture on both heads
  bool allGood = false;
  for (int attempt = 0; attempt < 3; attempt++) {
#if FLOPPY_VERBOSE_CAPTURE_LOG
    DBG_SERIAL.print("  cyl=");
    DBG_SERIAL.print(cyl);
    DBG_SERIAL.print(" slot=");
    DBG_SERIAL.print(slot);
    DBG_SERIAL.print(" attempt=");
    DBG_SERIAL.println(attempt);
#endif
    bool noIndexAtAll = false;
    selectHead(0);
    if (!captureOneRevolutionToCellbits(&noIndexAtAll)) {
      // No INDEX at all means no disk - retrying the same wait can't help.
      if (noIndexAtAll) break;
      continue;
    }
    decodeCurrentTrackIntoCache(slot, cyl, 0);

    selectHead(1);
    if (!captureOneRevolutionToCellbits(&noIndexAtAll)) {
      if (noIndexAtAll) break;
      continue;
    }
    decodeCurrentTrackIntoCache(slot, cyl, 1);
    everCaptured = true;

    allGood = true;
    for (int h = 0; h < NUM_HEADS && allGood; h++)
      for (int s = 0; s < SECTORS_PER_TRACK; s++)
        if (!sectorPresent[slot][h][s]) { allGood = false; break; }
    if (allGood) break;
  }

#if FLOPPY_DIAGNOSTIC_LOG
  if (!allGood && g_seekMismatchAgreeCount >= SEEK_MISMATCH_CONFIRM_COUNT) {
    DBG_SERIAL.print("floppy: seek-mismatch signal at assumed cyl=");
    DBG_SERIAL.print(cyl);
    DBG_SERIAL.print(", disk reports cyl=");
    DBG_SERIAL.println(g_seekMismatchCyl);
  }
#endif

  lastActivityMs = millis(); // the idle timer starts after the capture, not before
  if (!everCaptured) {
#if FLOPPY_DIAGNOSTIC_LOG
    DBG_SERIAL.print("floppy: cyl=");
    DBG_SERIAL.print(cyl);
    DBG_SERIAL.println(": no complete capture on either head - no usable signal here.");
#endif
    cachedCyl[slot] = -1; // the slot's previous cylinder was already overwritten
    g_lastError = FLOPPY_ERR_SECTOR_UNRECOVERABLE;
    if (cylinderTotalFailStreak[cyl] < MAX_CYLINDER_REFETCH_STREAK) {
      cylinderTotalFailStreak[cyl]++;
    } else {
      cylinderCaptureGiveUp[cyl] = true;
    }
    return -1;
  }

  cachedCyl[slot] = cyl;
  slotLastUsed[slot] = ++cacheUseCounter;
  cylinderSlotFullyGood[slot] = allGood;
  cylinderTotalFailStreak[cyl] = 0;
  return slot;
}

static bool readSector(int cyl, int head, int sector, uint8_t *out512) {
  int slot = ensureCylinderCached(cyl);
  if (slot == -1) return false;
  if (!sectorPresent[slot][head][sector - 1]) {
#if FLOPPY_DIAGNOSTIC_LOG
    int presentCount = 0;
    for (int s = 0; s < SECTORS_PER_TRACK; s++) if (sectorPresent[slot][head][s]) presentCount++;
    DBG_SERIAL.print("floppy: cyl=");
    DBG_SERIAL.print(cyl);
    DBG_SERIAL.print(" head=");
    DBG_SERIAL.print(head);
    DBG_SERIAL.print(" sector=");
    DBG_SERIAL.print(sector);
    DBG_SERIAL.print(": capture succeeded (");
    DBG_SERIAL.print(presentCount);
    DBG_SERIAL.print("/");
    DBG_SERIAL.print(SECTORS_PER_TRACK);
    DBG_SERIAL.println(" sectors on this head) but not this one - a marginal sector.");
#endif
    g_lastError = FLOPPY_ERR_SECTOR_UNRECOVERABLE;
    // Recapture on the next read (up to the cap) - it may come back.
    if (cyl >= 0 && cyl < NUM_CYLINDERS && cylinderPartialMissStreak[cyl] < MAX_CYLINDER_REFETCH_STREAK) {
      cylinderPartialMissStreak[cyl]++;
      cachedCyl[slot] = -1;
    }
    return false;
  }
  if (cyl >= 0 && cyl < NUM_CYLINDERS && cylinderSlotFullyGood[slot]) cylinderPartialMissStreak[cyl] = 0;
  memcpy(out512, sectorCache[slot][head][sector - 1], 512);
  return true;
}

// -- FAT12 --

struct Bpb {
  uint16_t bytesPerSector;
  uint8_t sectorsPerCluster;
  uint16_t reservedSectors;
  uint8_t numFats;
  uint16_t rootEntries;
  uint16_t sectorsPerFat;
  uint16_t sectorsPerTrack;
  uint16_t numHeads;
};

static Bpb bpb;
static uint32_t fatStartLba, rootStartLba, dataStartLba;
static uint32_t rootSectorCount;

#define MAX_FAT_BYTES 7168 // FAT12's own format ceiling is ~4084 clusters (~6126 bytes)
static uint8_t fatBytes[MAX_FAT_BYTES];

static FloppyDirEntry rootEntries[MAX_DIR_ENTRIES];
static int rootEntryCount;

static void lbaToChs(uint32_t lba, int *cyl, int *head, int *sector) {
  *sector = (lba % bpb.sectorsPerTrack) + 1;
  uint32_t tmp = lba / bpb.sectorsPerTrack;
  *head = tmp % bpb.numHeads;
  *cyl = tmp / bpb.numHeads;
}

static bool readLba(uint32_t lba, uint8_t *out512) {
  int cyl, head, sector;
  lbaToChs(lba, &cyl, &head, &sector);
  return readSector(cyl, head, sector, out512);
}

static bool fatChainSeen[4096]; // static, not stack-allocated, to avoid stack pressure

static int readFatChain(uint16_t startCluster, uint16_t *outClusters, int maxClusters) {
  int n = 0;
  uint16_t cluster = startCluster;
  memset(fatChainSeen, 0, sizeof(fatChainSeen));
  while (cluster >= 2 && cluster < 0xFF8 && n < maxClusters) {
    if (cluster < 4096) {
      if (fatChainSeen[cluster]) break;
      fatChainSeen[cluster] = true;
    }
    outClusters[n++] = cluster;
    uint32_t offset = cluster + (cluster / 2);
    if (offset + 1 >= MAX_FAT_BYTES) break;
    uint16_t raw = fatBytes[offset] | ((uint16_t)fatBytes[offset + 1] << 8);
    cluster = (cluster % 2 == 0) ? (raw & 0xFFF) : (raw >> 4);
  }
  return n;
}

// Root and subdirectories share the 32-byte entry format.
static int parseDirectoryBytes(const uint8_t *buf, uint32_t len, FloppyDirEntry *out, int maxOut, bool skipDotEntries) {
  int n = 0;
  for (uint32_t i = 0; i + 32 <= len && n < maxOut; i += 32) {
    const uint8_t *entry = buf + i;
    uint8_t firstByte = entry[0];
    if (firstByte == 0x00) break;
    if (firstByte == 0xE5) continue;
    uint8_t attr = entry[11];
    if (attr == 0x0F) continue; // LFN entry

    // Names end at the padding; a volume label keeps its inner spaces.
    bool label = (attr & 0x08) && !(attr & 0x10);
    char name[9], ext[4];
    int nl = 0;
    for (int k = 0; k < 8 && (label || entry[k] != ' '); k++) name[nl++] = entry[k];
    while (nl > 0 && name[nl - 1] == ' ') nl--;
    name[nl] = 0;
    int el = 0;
    for (int k = 0; k < 3 && (label || entry[8 + k] != ' '); k++) ext[el++] = entry[8 + k];
    while (el > 0 && ext[el - 1] == ' ') el--;
    ext[el] = 0;

    if (skipDotEntries && name[0] == '.') continue;

    FloppyDirEntry &e = out[n++];
    strncpy(e.name, name, 9);
    strncpy(e.ext, ext, 4);
    e.attr = attr;
    e.startCluster = entry[26] | ((uint16_t)entry[27] << 8);
    e.size = (uint32_t)entry[28] | ((uint32_t)entry[29] << 8) | ((uint32_t)entry[30] << 16) | ((uint32_t)entry[31] << 24);
  }
  return n;
}

bool floppy_init() {
  initCrc16Table();
  pinMode(pinDriveSelect, OUTPUT);
  pinMode(pinMotorEnable, OUTPUT);
  pinMode(pinDirection, OUTPUT);
  pinMode(pinStep, OUTPUT);
  pinMode(pinSideSelect, OUTPUT);
  pinMode(pinTrack00, INPUT);
  pinMode(pinIndex, INPUT);
  pinMode(pinReadData, INPUT);
  // Pulled up so an unwired DSKCHG doesn't float and read as a disk change.
  pinMode(pinDiskChange, INPUT_PULLUP);

  digitalWrite(pinDriveSelect, HIGH);
  digitalWrite(pinMotorEnable, HIGH);
  digitalWrite(pinDirection, HIGH);
  digitalWrite(pinStep, HIGH);
  digitalWrite(pinSideSelect, HIGH);

  digitalWrite(pinMotorEnable, LOW);
  digitalWrite(pinDriveSelect, LOW);
  delay(600);

  bool homed = homeToTrack0();
  if (!homed) {
    DBG_SERIAL.println("floppy_init(): Track00 never went low while homing - check the sensor, cable and disk.");
  }
  currentCyl = 0;
  invalidateSectorCache();
  setupFluxPio();
  lastActivityMs = millis();
  return homed;
}

bool floppy_disk_change_asserted() {
  // Debounced: stepper noise can pull one sample low and cause a false remount.
  for (int i = 0; i < 4; i++) {
    if (digitalRead(pinDiskChange) != LOW) return false;
    if (i < 3) delayMicroseconds(200);
  }
  return true;
}

static bool g_newDisk = false; // seen by a probe, not yet remounted
static uint32_t g_lastProbeMs = 0;

uint8_t floppy_poll_disk_change() {
  if (g_newDisk) return 1;
  if (!floppy_disk_change_asserted()) return 0;
  if (millis() - g_lastProbeMs < FLOPPY_PROBE_INTERVAL_MS) return 2;
  g_lastProbeMs = millis();
  // DSKCHG only clears on a step with a disk in, so step in and back out.
  stepOnce(false);
  stepOnce(true);
  if (floppy_disk_change_asserted()) return 2;
  g_newDisk = true;
  return 1;
}

static uint32_t fnv1a(uint32_t hash, const void *data, uint32_t len) {
  const uint8_t *bytes = (const uint8_t *)data;
  for (uint32_t i = 0; i < len; i++) {
    hash ^= bytes[i];
    hash *= 16777619u;
  }
  return hash;
}

static uint32_t g_diskId = 0;

uint32_t floppy_disk_id() { return g_diskId; }

uint32_t floppy_file_key(const FloppyDirEntry &entry) {
  uint32_t hash = fnv1a(2166136261u, entry.name, strnlen(entry.name, sizeof(entry.name)));
  hash = fnv1a(hash, entry.ext, strnlen(entry.ext, sizeof(entry.ext)));
  hash = fnv1a(hash, &entry.startCluster, sizeof(entry.startCluster));
  return fnv1a(hash, &entry.size, sizeof(entry.size));
}

bool floppy_remount() {
  DBG_SERIAL.println("floppy_remount() called."); // repeated lines = a false DSKCHG storm
  g_newDisk = false;
  spinUp();
  // A fresh disk needs time to clamp and reach speed.
  delay(600);
  stepOnce(true); // DSKCHG only clears on a real step, even at cylinder 0
  if (!homeToTrack0()) {
    DBG_SERIAL.println("floppy_remount(): Track00 never went low while homing.");
  }
  currentCyl = 0;
  invalidateSectorCache();
  return floppy_mount();
}

bool floppy_mount() {
  static uint8_t boot[512]; // static: deep, non-reentrant call chain
  if (!readSector(0, 0, 1, boot)) return false;

  bpb.bytesPerSector = boot[11] | ((uint16_t)boot[12] << 8);
  bpb.sectorsPerCluster = boot[13];
  bpb.reservedSectors = boot[14] | ((uint16_t)boot[15] << 8);
  bpb.numFats = boot[16];
  bpb.rootEntries = boot[17] | ((uint16_t)boot[18] << 8);
  bpb.sectorsPerFat = boot[22] | ((uint16_t)boot[23] << 8);
  bpb.sectorsPerTrack = boot[24] | ((uint16_t)boot[25] << 8);
  bpb.numHeads = boot[26] | ((uint16_t)boot[27] << 8);

  // Bounds matter: these size the cache arrays, and sectorsPerCluster divides.
  if (bpb.bytesPerSector != 512 || bpb.sectorsPerTrack == 0 || bpb.numHeads == 0 ||
      bpb.sectorsPerTrack > SECTORS_PER_TRACK || bpb.numHeads > NUM_HEADS ||
      bpb.sectorsPerCluster == 0) {
    g_lastError = FLOPPY_ERR_BAD_BOOT_SECTOR;
    return false;
  }

  fatStartLba = bpb.reservedSectors;
  rootStartLba = fatStartLba + (uint32_t)bpb.numFats * bpb.sectorsPerFat;
  rootSectorCount = ((uint32_t)bpb.rootEntries * 32 + bpb.bytesPerSector - 1) / bpb.bytesPerSector;
  dataStartLba = rootStartLba + rootSectorCount;

  uint32_t fatBytesNeeded = (uint32_t)bpb.sectorsPerFat * bpb.bytesPerSector;
  if (fatBytesNeeded > MAX_FAT_BYTES) {
    g_lastError = FLOPPY_ERR_BAD_BOOT_SECTOR;
    return false;
  }
  for (uint32_t i = 0; i < bpb.sectorsPerFat; i++) {
    if (!readLba(fatStartLba + i, fatBytes + i * 512)) return false;
  }

  static uint8_t rootBytes[16 * 512];
  if (rootSectorCount * 512 > sizeof(rootBytes)) {
    g_lastError = FLOPPY_ERR_BAD_BOOT_SECTOR;
    return false;
  }
  for (uint32_t i = 0; i < rootSectorCount; i++) {
    if (!readLba(rootStartLba + i, rootBytes + i * 512)) return false;
  }
  rootEntryCount = parseDirectoryBytes(rootBytes, rootSectorCount * 512, rootEntries, MAX_DIR_ENTRIES, false);

  // Any change to the disk's files changes the FAT or the root's entries (sizes, dates).
  g_diskId = fnv1a(fnv1a(fnv1a(2166136261u, boot, 512), fatBytes, fatBytesNeeded),
                   rootBytes, rootSectorCount * 512);
  return true;
}

int floppy_root_entry_count() { return rootEntryCount; }
const FloppyDirEntry &floppy_root_entry(int index) { return rootEntries[index]; }

int floppy_read_subdirectory(const FloppyDirEntry &dirEntry, FloppyDirEntry *outEntries, int maxEntries) {
  uint16_t clusters[32];
  int n = readFatChain(dirEntry.startCluster, clusters, 32);
  if (n == 0) {
    g_lastError = FLOPPY_ERR_NO_CLUSTERS;
    return -1;
  }

  static uint8_t buf[16 * 512]; // 256 entries, room for long-filename entries too
  uint32_t total = 0;
  for (int i = 0; i < n; i++) {
    uint32_t lba = dataStartLba + (clusters[i] - 2) * bpb.sectorsPerCluster;
    for (int s = 0; s < bpb.sectorsPerCluster; s++) {
      if (total + 512 > sizeof(buf)) break;
      if (!readLba(lba + s, buf + total)) return -1;
      total += 512;
    }
  }
  return parseDirectoryBytes(buf, total, outEntries, maxEntries, true);
}

bool floppy_open_file_handle(const FloppyDirEntry &entry, FloppyFileHandle *handle) {
  handle->fileSize = entry.size;
  handle->clusterCount = 0;

  if (entry.size == 0) return true; // reads just report out of range

  if (entry.startCluster == 0) {
    g_lastError = FLOPPY_ERR_NO_CLUSTERS;
    return false;
  }
  handle->clusterCount = readFatChain(entry.startCluster, handle->clusters, FLOPPY_MAX_FILE_CLUSTERS);
  if (handle->clusterCount == 0) {
    g_lastError = FLOPPY_ERR_NO_CLUSTERS;
    return false;
  }
  // A chain that fills the array may have been cut short.
  uint32_t neededClusters = (entry.size + (uint32_t)bpb.sectorsPerCluster * 512 - 1) / ((uint32_t)bpb.sectorsPerCluster * 512);
  if (handle->clusterCount >= FLOPPY_MAX_FILE_CLUSTERS && neededClusters > (uint32_t)handle->clusterCount) {
    g_lastError = FLOPPY_ERR_TOO_MANY_CLUSTERS;
    return false;
  }
  return true;
}

bool floppy_read_file_sector(const FloppyFileHandle *handle, uint32_t fileOffset, uint8_t *out512) {
  if (fileOffset >= handle->fileSize) {
    g_lastError = FLOPPY_ERR_OUT_OF_RANGE;
    return false;
  }
  uint32_t bytesPerCluster = (uint32_t)bpb.sectorsPerCluster * 512;
  uint32_t clusterIndex = fileOffset / bytesPerCluster;
  uint32_t offsetInCluster = fileOffset % bytesPerCluster;
  if ((int)clusterIndex >= handle->clusterCount) {
    g_lastError = FLOPPY_ERR_OUT_OF_RANGE;
    return false;
  }
  uint32_t lba = dataStartLba + (handle->clusters[clusterIndex] - 2) * bpb.sectorsPerCluster + offsetInCluster / 512;
  return readLba(lba, out512);
}
