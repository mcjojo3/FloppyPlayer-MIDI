// floppy_fat12.cpp - see floppy_fat12.h and README.md for the design
// overview. PIO capture core (pins, program, clock derivation) targets this
// exact hardware's flux timing.
#include "floppy_fat12.h"
#include <Arduino.h>
#include <hardware/clocks.h>
#include <hardware/gpio.h>
#include <hardware/pio.h>
#include <pico/platform.h>
#include <string.h>

// __not_in_flash_func() places a function's code in RAM instead of flash.
// RP2040's two cores share one flash (XIP) bus; a long, tight, flash-executed
// loop on one core stalls the other core's instruction fetches whenever both
// need the bus at once. The per-revolution capture loop and its immediate
// decode pass run flat-out for most of a cylinder read's real time, all on
// Core 0, while Core 1 needs the flash bus too to keep the audio DMA buffer
// fed - so the whole hot capture/decode path below is marked RAM-resident.

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

static void homeToTrack0() {
  digitalWrite(pinDirection, HIGH);
  delayMicroseconds(10);
  int steps = 0;
  while (digitalRead(pinTrack00) == HIGH && steps < 100) {
    stepOnce(true);
    steps++;
  }
}

// HIGH = head 0, LOW = head 1 (inverted relative to the signal name).
static void selectHead(int head) {
  digitalWrite(pinSideSelect, head == 0 ? HIGH : LOW);
  delay(2);
}

static int currentCyl = 0;

static void seekToCylinder(int target) {
  if (target == currentCyl) return;
  if (target > currentCyl) {
    for (int i = 0; i < target - currentCyl; i++) stepOnce(false); // inward = higher cylinder
  } else {
    for (int i = 0; i < currentCyl - target; i++) stepOnce(true);
  }
  currentCyl = target;
}

// ============================================================================
// Streaming MFM cell-bit decode: delta-to-unit conversion and cell-bit
// packing fused into one pass as each transition arrives from the PIO FIFO,
// instead of buffering raw deltas separately - the raw-deltas array alone
// wouldn't fit in RAM alongside everything else.
// ============================================================================

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
static int candidateCount;

static inline void __not_in_flash_func(emitBit)(int bit) {
  pushBit(bit);
}

// Sync-candidate search runs as a post-pass over the completed revolution,
// not inline during capture - the timing-critical FIFO-draining loop can't
// afford a software popcount (no hardware POPCNT) on every bit without
// falling behind and corrupting samples.
static void __not_in_flash_func(findSyncCandidates)() {
  candidateCount = 0;
  uint64_t window = 0;
  for (uint32_t pos = 0; pos < cellBitCount; pos++) {
    window = (window << 1) | (uint64_t)getBit(pos);
    if (pos + 1 >= 48) {
      uint64_t window48 = window & 0xFFFFFFFFFFFFULL;
      int mismatches = __builtin_popcountll(window48 ^ SYNC_PATTERN_48);
      if (mismatches <= SYNC_TOLERANCE && candidateCount < MAX_CANDIDATES) {
        candidatePositions[candidateCount++] = pos + 1 - 48;
      }
    }
    if (pos + 1 >= 16) {
      uint64_t window16 = window & 0xFFFFULL;
      if (window16 == SINGLE_PATTERN_48 && candidateCount < MAX_CANDIDATES) {
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

// Splits an interval >4 units into valid {2,3,4} parts (a bare 1-unit gap
// isn't valid, so a trailing 1 folds into the previous 4 as a 2+3 split).
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

// A unit <2 gets added to the next unit before either is emitted; the tail
// element (no partner to merge into) is flushed as-is via flushPendingUnit().
static bool havePendingUnit;
static int pendingUnit;

// raw/24.0 needs round-HALF-TO-EVEN (banker's rounding), not round-half-up -
// a naive (raw+12)/24 disagrees on ties, and since MFM decoding is fully
// sequential, one wrong unit permanently misaligns everything after it.
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

// ============================================================================
// Byte/CRC decode over the completed cellBits buffer (random-access, run
// only after a full revolution has been captured - no real-time constraint
// here, the disk has already passed this data).
// ============================================================================

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

static uint16_t __not_in_flash_func(crc16_ccitt)(const uint8_t *data, int len) {
  uint16_t crc = 0xFFFF;
  for (int i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) {
      if (crc & 0x8000) crc = (crc << 1) ^ 0x1021;
      else crc = crc << 1;
    }
  }
  return crc;
}

struct IdamResult {
  uint8_t cyl, head, sector, size;
  uint32_t bitAfterIdam;
};

static bool __not_in_flash_func(tryDecodeIdam)(uint32_t syncPos, IdamResult *out) {
  uint32_t after = syncPos + 48;
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
    int mismatches = __builtin_popcountll(window ^ SYNC_PATTERN_48);
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
  static uint8_t payload[514]; // static: this sits several frames deep in a non-reentrant capture call chain
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

// ============================================================================
// Per-cylinder sector cache. Multiple cylinders cached at once (LRU
// eviction) since different tracks in a multi-track MIDI file are usually
// positioned on different cylinders simultaneously - a single-slot cache
// would evict and reseek constantly.
// ============================================================================

static const int SECTORS_PER_TRACK = 18;
static const int NUM_HEADS = 2;

#define NUM_CACHE_SLOTS 3
static uint8_t sectorCache[NUM_CACHE_SLOTS][NUM_HEADS][SECTORS_PER_TRACK][512];
static bool sectorPresent[NUM_CACHE_SLOTS][NUM_HEADS][SECTORS_PER_TRACK];
static int cachedCyl[NUM_CACHE_SLOTS];
static uint32_t slotLastUsed[NUM_CACHE_SLOTS];
static uint32_t cacheUseCounter;

static void invalidateSectorCache() {
  for (int s = 0; s < NUM_CACHE_SLOTS; s++) { cachedCyl[s] = -1; slotLastUsed[s] = 0; }
  cacheUseCounter = 0;
}

static FloppyError g_lastError = FLOPPY_OK;
static int g_lastFailCyl = -1, g_lastFailHead = -1, g_lastFailSector = -1;

FloppyError floppy_last_error() { return g_lastError; }
void floppy_last_sector_failure(int *cyl, int *head, int *sector) {
  *cyl = g_lastFailCyl;
  *head = g_lastFailHead;
  *sector = g_lastFailSector;
}

static uint32_t lastTransitionCount;

// Returns false if no disk is present/spinning (INDEX never pulsed within
// the timeout) instead of hanging forever - a real disk spins at
// ~200ms/revolution, so 1000ms per wait is generous margin.
static bool __not_in_flash_func(captureOneRevolutionToCellbits)() {
  cellBitCount = 0;
  havePendingUnit = false;
  lastTransitionCount = 0;

  pio_sm_clear_fifos(capturePio, captureSm);
  pio_sm_exec(capturePio, captureSm, captureOffset);
  pio_sm_restart(capturePio, captureSm);
  fifoHalfValid = false;

  static const uint32_t INDEX_WAIT_TIMEOUT_MS = 1000;
  uint32_t waitStart = millis();
  while (gpio_get(pinIndex) == 0) { // wait for idle-high
    if (millis() - waitStart > INDEX_WAIT_TIMEOUT_MS) return false;
  }
  waitStart = millis();
  while (gpio_get(pinIndex) != 0) { // wait for falling edge
    if (millis() - waitStart > INDEX_WAIT_TIMEOUT_MS) return false;
  }

  pio_sm_set_enabled(capturePio, captureSm, true);

  uint16_t last = fifoRead();
  bool lastIndex = gpio_get(pinIndex);
  uint32_t revStart = millis(); // covers a mid-revolution fault (e.g. INDEX stops pulsing) the pre-capture wait above can't catch

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
    int32_t delta = (int32_t)last - (int32_t)data;
    if (delta < 0) delta += 65536;
    delta /= 2;
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

// Gated behind FLOPPY_VERBOSE_CAPTURE_LOG (default off) - Serial output from
// inside this hot path caused real audio glitches (host-dependent blocking,
// and Print isn't RAM-resident like the rest of this file). Flip to 1 to
// debug a mount/recovery problem.
#define FLOPPY_VERBOSE_CAPTURE_LOG 0
static int diagIdamCrcOk, diagDamFound, diagDamCrcOk;

// Does NOT reset sectorPresent[slot][head][*] at the start - see
// ensureCylinderCached(), which resets it once per cylinder rather than
// once per attempt, so sectors found on an earlier attempt survive even if
// a later attempt's different revolution doesn't find that same sector again.
static void __not_in_flash_func(decodeCurrentTrackIntoCache)(int slot, int cyl, int head) {
  diagIdamCrcOk = 0;
  diagDamFound = 0;
  diagDamCrcOk = 0;

  for (int c = 0; c < candidateCount; c++) {
    IdamResult idam;
    if (!tryDecodeIdam(candidatePositions[c], &idam)) continue;
    diagIdamCrcOk++;
    if (idam.sector < 1 || idam.sector > SECTORS_PER_TRACK) continue;
    if (idam.cyl != cyl || idam.head != head) continue; // disk disagrees with where we think we seeked - a mechanical seek error, don't trust this sector

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
  Serial.print("    head=");
  Serial.print(head);
  Serial.print(": ");
  Serial.print(lastTransitionCount);
  Serial.print(" transitions, ");
  Serial.print(cellBitCount);
  Serial.print(" cellbits, ");
  Serial.print(candidateCount);
  Serial.print(" sync candidates, ");
  Serial.print(diagIdamCrcOk);
  Serial.print(" IDAM CRC-ok, ");
  Serial.print(diagDamFound);
  Serial.print(" DAM found, ");
  Serial.print(diagDamCrcOk);
  Serial.print(" DAM CRC-ok, ");
  Serial.print(presentCount);
  Serial.println("/18 sectors recovered");
#endif
}

// Returns the cache slot holding cyl's data (loading it first if needed),
// or -1 on failure. Picks an empty slot if one exists, else evicts the
// least-recently-used one.
static int ensureCylinderCached(int cyl) {
  // A real disk only has NUM_CYLINDERS tracks - reject anything outside
  // that range immediately rather than seeking somewhere physically
  // impossible (a garbage LBA/cluster value upstream would otherwise waste
  // time and read noise).
  if (cyl < 0 || cyl >= NUM_CYLINDERS) {
    g_lastError = FLOPPY_ERR_SECTOR_UNRECOVERABLE;
    g_lastFailCyl = cyl;
    g_lastFailHead = -1;
    g_lastFailSector = -1;
    return -1;
  }

  for (int slot = 0; slot < NUM_CACHE_SLOTS; slot++) {
    if (cachedCyl[slot] == cyl) {
      slotLastUsed[slot] = ++cacheUseCounter;
      return slot;
    }
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

  seekToCylinder(cyl);

  // Reset once per cylinder, not per attempt - see decodeCurrentTrackIntoCache().
  for (int h = 0; h < NUM_HEADS; h++)
    for (int s = 0; s < SECTORS_PER_TRACK; s++)
      sectorPresent[slot][h][s] = false;

  for (int attempt = 0; attempt < 3; attempt++) {
#if FLOPPY_VERBOSE_CAPTURE_LOG
    Serial.print("  cyl=");
    Serial.print(cyl);
    Serial.print(" slot=");
    Serial.print(slot);
    Serial.print(" attempt=");
    Serial.println(attempt);
#endif

    selectHead(0);
    if (!captureOneRevolutionToCellbits()) {
      cachedCyl[slot] = -1; // slot may have held a different, still-good cylinder before eviction - don't leave that stale
      g_lastError = FLOPPY_ERR_SECTOR_UNRECOVERABLE;
      g_lastFailCyl = cyl;
      g_lastFailHead = 0;
      g_lastFailSector = -1;
      return -1;
    }
    decodeCurrentTrackIntoCache(slot, cyl, 0);

    selectHead(1);
    if (!captureOneRevolutionToCellbits()) {
      cachedCyl[slot] = -1;
      g_lastError = FLOPPY_ERR_SECTOR_UNRECOVERABLE;
      g_lastFailCyl = cyl;
      g_lastFailHead = 1;
      g_lastFailSector = -1;
      return -1;
    }
    decodeCurrentTrackIntoCache(slot, cyl, 1);

    bool allGood = true;
    for (int h = 0; h < NUM_HEADS && allGood; h++)
      for (int s = 0; s < SECTORS_PER_TRACK; s++)
        if (!sectorPresent[slot][h][s]) { allGood = false; break; }
    if (allGood) break; // don't waste a retry revolution if everything came back clean
  }

  cachedCyl[slot] = cyl;
  slotLastUsed[slot] = ++cacheUseCounter;
  return slot;
}

static bool readSector(int cyl, int head, int sector, uint8_t *out512) {
  int slot = ensureCylinderCached(cyl);
  if (slot == -1) return false;
  if (!sectorPresent[slot][head][sector - 1]) {
    // A cylinder stays marked cached even if some sectors never came back
    // CRC-valid (otherwise one marginal sector would make the whole
    // cylinder retry forever). But that means this specific miss would
    // otherwise be permanent - the cache slot's cachedCyl staying set skips
    // capture entirely on every future request. Invalidate so the next
    // call gets a genuinely fresh capture; a marginal sector can succeed on
    // a different revolution.
    cachedCyl[slot] = -1;
    g_lastError = FLOPPY_ERR_SECTOR_UNRECOVERABLE;
    g_lastFailCyl = cyl;
    g_lastFailHead = head;
    g_lastFailSector = sector;
    return false;
  }
  memcpy(out512, sectorCache[slot][head][sector - 1], 512);
  return true;
}

// ============================================================================
// FAT12 layer.
// ============================================================================

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

#define MAX_ROOT_ENTRIES 32
static FloppyDirEntry rootEntries[MAX_ROOT_ENTRIES];
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

// Shared by the root directory and subdirectory listings (identical 32-byte
// entry format).
static int parseDirectoryBytes(const uint8_t *buf, uint32_t len, FloppyDirEntry *out, int maxOut, bool skipDotEntries) {
  int n = 0;
  for (uint32_t i = 0; i + 32 <= len && n < maxOut; i += 32) {
    const uint8_t *entry = buf + i;
    uint8_t firstByte = entry[0];
    if (firstByte == 0x00) break;
    if (firstByte == 0xE5) continue;
    uint8_t attr = entry[11];
    if (attr == 0x0F) continue; // LFN entry

    char name[9], ext[4];
    int nl = 0;
    for (int k = 0; k < 8 && entry[k] != ' '; k++) name[nl++] = entry[k];
    name[nl] = 0;
    int el = 0;
    for (int k = 0; k < 3 && entry[8 + k] != ' '; k++) ext[el++] = entry[8 + k];
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
  pinMode(pinDriveSelect, OUTPUT);
  pinMode(pinMotorEnable, OUTPUT);
  pinMode(pinDirection, OUTPUT);
  pinMode(pinStep, OUTPUT);
  pinMode(pinSideSelect, OUTPUT);
  pinMode(pinTrack00, INPUT);
  pinMode(pinIndex, INPUT);
  pinMode(pinReadData, INPUT);
  pinMode(pinDiskChange, INPUT);

  digitalWrite(pinDriveSelect, HIGH);
  digitalWrite(pinMotorEnable, HIGH);
  digitalWrite(pinDirection, HIGH);
  digitalWrite(pinStep, HIGH);
  digitalWrite(pinSideSelect, HIGH);

  digitalWrite(pinMotorEnable, LOW);
  digitalWrite(pinDriveSelect, LOW);
  delay(600);

  homeToTrack0();
  currentCyl = 0;
  invalidateSectorCache();
  setupFluxPio();
  return true;
}

static bool motorOn = true; // floppy_init() (above) already turned it on

void floppy_motor_on() {
  if (motorOn) return;
  digitalWrite(pinDriveSelect, LOW);
  digitalWrite(pinMotorEnable, LOW);
  delay(600);
  motorOn = true;
}

void floppy_motor_off() {
  digitalWrite(pinMotorEnable, HIGH);
  digitalWrite(pinDriveSelect, HIGH);
  motorOn = false;
}

bool floppy_disk_change_asserted() {
  return digitalRead(pinDiskChange) == LOW;
}

bool floppy_remount() {
  stepOnce(true); // guarantee a real step pulse even if already at cylinder 0 - DSKCHG only clears on an actual step
  homeToTrack0();
  currentCyl = 0;
  invalidateSectorCache();
  return floppy_mount();
}

bool floppy_mount() {
  static uint8_t boot[512]; // static: same reasoning as decodeDamPayload's buffers - deep, non-reentrant call chain
  if (!readSector(0, 0, 1, boot)) return false;

  bpb.bytesPerSector = boot[11] | ((uint16_t)boot[12] << 8);
  bpb.sectorsPerCluster = boot[13];
  bpb.reservedSectors = boot[14] | ((uint16_t)boot[15] << 8);
  bpb.numFats = boot[16];
  bpb.rootEntries = boot[17] | ((uint16_t)boot[18] << 8);
  bpb.sectorsPerFat = boot[22] | ((uint16_t)boot[23] << 8);
  bpb.sectorsPerTrack = boot[24] | ((uint16_t)boot[25] << 8);
  bpb.numHeads = boot[26] | ((uint16_t)boot[27] << 8);

  // Upper bounds matter, not just non-zero: sectorsPerTrack/numHeads feed
  // straight into sectorCache[][][]'s fixed dimensions (lbaToChs -> readSector),
  // and sectorsPerCluster is a divisor below - an out-of-spec or corrupt BPB
  // would otherwise overflow the cache or divide by zero.
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
  rootEntryCount = parseDirectoryBytes(rootBytes, rootSectorCount * 512, rootEntries, MAX_ROOT_ENTRIES, false);

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

  static uint8_t buf[8 * 512];
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

  if (entry.size == 0) return true; // valid handle, floppy_read_file_sector will just report out-of-range immediately

  if (entry.startCluster == 0) {
    g_lastError = FLOPPY_ERR_NO_CLUSTERS;
    return false;
  }
  handle->clusterCount = readFatChain(entry.startCluster, handle->clusters, FLOPPY_MAX_FILE_CLUSTERS);
  if (handle->clusterCount == 0) {
    g_lastError = FLOPPY_ERR_NO_CLUSTERS;
    return false;
  }
  // A chain that fills the whole array might be truncated rather than
  // genuinely ending at the cap - only way to know is if the declared file
  // size implies more clusters than we actually read.
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
