// sd_fat.cpp - files are opened by index within their parent, so listed
// directories stay open in slots. Entries carry their slot and its generation
// in startCluster's top bits: a reused slot fails cleanly and the Pi re-lists.
#include "config.h"
#include "sd_fat.h"
#include <stdio.h>
#include <string.h>

static SdFat g_sd;
static bool g_mounted = false;
static SdError g_lastError = SD_OK;
static FsFile g_rootDir;

#define SD_SLOTS 16
static FsFile g_slots[SD_SLOTS];
static bool g_slotValid[SD_SLOTS];
static uint32_t g_slotKey[SD_SLOTS];    // startCluster of the entry the slot was opened from
static uint8_t g_slotKeyAttr[SD_SLOTS]; // and that entry's SD_ATTR_FROM_SLOT bit
static uint32_t g_slotLastUsed[SD_SLOTS];
static uint8_t g_slotGeneration[SD_SLOTS];
static uint32_t g_useCounter = 0;

#define SD_SLOT_SHIFT 28
#define SD_SLOT_MASK (0xFu << SD_SLOT_SHIFT) // 4 bits: 16 slots
#define SD_GEN_SHIFT 24
#define SD_GEN_MASK (0xFu << SD_GEN_SHIFT)
#define SD_INDEX_MASK (~(SD_SLOT_MASK | SD_GEN_MASK)) // real FAT directory indexes are far smaller

// Entry came from a slot, not the root. Real FAT attributes never use 0x80.
#define SD_ATTR_FROM_SLOT 0x80

static SdDirEntry g_rootEntries[MAX_DIR_ENTRIES];

bool sd_init() {
  SPI.setSCK(SD_SCK_PIN);
  SPI.setTX(SD_MOSI_PIN);
  SPI.setRX(SD_MISO_PIN);
  return true; // SdFat calls SPI.begin() itself
}

static void populateEntry(FsFile &entry, SdDirEntry *out, int slot) {
  char nameBuf[32];
  size_t len = entry.getName(nameBuf, sizeof(nameBuf));
  nameBuf[len < sizeof(nameBuf) ? len : sizeof(nameBuf) - 1] = 0;

  // The protocol carries 8.3-sized fields, so long names are cut to fit.
  char *dot = strrchr(nameBuf, '.');
  size_t nameLen = dot ? (size_t)(dot - nameBuf) : strlen(nameBuf);
  if (nameLen > 8) nameLen = 8;
  memcpy(out->name, nameBuf, nameLen);
  out->name[nameLen] = 0;
  if (dot) {
    strncpy(out->ext, dot + 1, 3);
    out->ext[3] = 0;
  } else {
    out->ext[0] = 0;
  }

  out->attr = entry.isDir() ? 0x10 : 0x00;
  out->startCluster = entry.dirIndex() & SD_INDEX_MASK;
  if (slot >= 0) {
    out->attr |= SD_ATTR_FROM_SLOT;
    out->startCluster |= ((uint32_t)slot << SD_SLOT_SHIFT) | ((uint32_t)g_slotGeneration[slot] << SD_GEN_SHIFT);
  }
  out->size = entry.isDir() ? 0 : (uint32_t)entry.fileSize();
}

// The directory an entry was listed from; nullptr if its slot was reused.
static FsFile *parentOf(const SdDirEntry &entry, uint32_t *outIndex) {
  *outIndex = entry.startCluster & SD_INDEX_MASK;
  if (!(entry.attr & SD_ATTR_FROM_SLOT)) {
    *outIndex = entry.startCluster;
    return &g_rootDir;
  }
  uint32_t slot = (entry.startCluster & SD_SLOT_MASK) >> SD_SLOT_SHIFT;
  uint32_t generation = (entry.startCluster & SD_GEN_MASK) >> SD_GEN_SHIFT;
  if (slot >= SD_SLOTS || !g_slotValid[slot] || g_slotGeneration[slot] != generation) return nullptr;
  g_slotLastUsed[slot] = ++g_useCounter;
  return &g_slots[slot];
}

bool sd_mount() {
  // 20MHz: SdFat's 50MHz default desynced on this board, 4MHz lagged.
  g_mounted = g_sd.begin(SD_CS_PIN, SD_SCK_MHZ(20));
  if (!g_mounted) {
    g_lastError = SD_ERR_NOT_MOUNTED;
    g_sd.initErrorPrint(&DBG_SERIAL); // not Serial - that's the link
    return false;
  }
  if (!g_rootDir.openRoot(&g_sd)) {
    g_mounted = false;
    g_lastError = SD_ERR_NOT_MOUNTED;
    return false;
  }
  for (int i = 0; i < SD_SLOTS; i++) g_slotValid[i] = false;
  return true;
}

bool sd_remount() {
  g_rootDir.close();
  for (int i = 0; i < SD_SLOTS; i++) {
    g_slots[i].close();
    g_slotValid[i] = false;
  }
  return sd_mount();
}

int sd_root_entry_count() {
  if (!g_mounted) {
    g_lastError = SD_ERR_NOT_MOUNTED;
    return 0;
  }
  g_rootDir.rewind();
  int n = 0;
  FsFile entry;
  while (n < MAX_DIR_ENTRIES && entry.openNext(&g_rootDir, O_RDONLY)) {
    populateEntry(entry, &g_rootEntries[n++], -1);
    entry.close();
  }
  return n;
}

const SdDirEntry &sd_root_entry(int index) {
  return g_rootEntries[index];
}

int sd_read_subdirectory(const SdDirEntry &dirEntry, SdDirEntry *outEntries, int maxEntries) {
  if (!g_mounted) {
    g_lastError = SD_ERR_NOT_MOUNTED;
    return -1;
  }
  uint8_t keyAttr = dirEntry.attr & SD_ATTR_FROM_SLOT;
  int slot = -1;
  for (int i = 0; i < SD_SLOTS; i++) {
    if (g_slotValid[i] && g_slotKey[i] == dirEntry.startCluster && g_slotKeyAttr[i] == keyAttr) {
      slot = i;
      break;
    }
  }
  if (slot == -1) {
    uint32_t index;
    FsFile *parent = parentOf(dirEntry, &index);
    if (!parent) {
      g_lastError = SD_ERR_OPEN_FAILED;
      return -1;
    }
    for (int i = 0; i < SD_SLOTS && slot == -1; i++) {
      if (!g_slotValid[i]) slot = i;
    }
    if (slot == -1) {
      // Evict the least recently used - never the parent, which parentOf() just touched.
      slot = 0;
      for (int i = 1; i < SD_SLOTS; i++) {
        if (g_slotLastUsed[i] < g_slotLastUsed[slot]) slot = i;
      }
    }
    g_slots[slot].close();
    g_slotValid[slot] = false;
    g_slotGeneration[slot] = (g_slotGeneration[slot] + 1) & 0xF; // old entries go stale
    if (!g_slots[slot].open(parent, index, O_RDONLY) || !g_slots[slot].isDir()) {
      g_slots[slot].close();
      g_lastError = SD_ERR_OPEN_FAILED;
      return -1;
    }
    g_slotValid[slot] = true;
    g_slotKey[slot] = dirEntry.startCluster;
    g_slotKeyAttr[slot] = keyAttr;
  }
  g_slotLastUsed[slot] = ++g_useCounter;

  int cap = maxEntries < MAX_DIR_ENTRIES ? maxEntries : MAX_DIR_ENTRIES;
  int n = 0;
  FsFile entry;
  g_slots[slot].rewind();
  while (n < cap && entry.openNext(&g_slots[slot], O_RDONLY)) {
    char nameBuf[32] = {0};
    entry.getName(nameBuf, sizeof(nameBuf));
    if (nameBuf[0] != '.') populateEntry(entry, &outEntries[n++], slot); // skip . and ..
    entry.close();
  }
  return n;
}

SdError sd_last_error() {
  return g_lastError;
}

bool sd_open_file_handle(const SdDirEntry &entry, SdFileHandle *handle) {
  if (!g_mounted) {
    g_lastError = SD_ERR_NOT_MOUNTED;
    return false;
  }
  uint32_t index;
  FsFile *parent = parentOf(entry, &index);
  handle->file.close();
  if (!parent || !handle->file.open(parent, index, O_RDONLY)) {
    g_lastError = SD_ERR_OPEN_FAILED;
    return false;
  }
  handle->fileSize = (uint32_t)handle->file.fileSize();
  return true;
}

bool sd_read_file_range(SdFileHandle *handle, uint32_t offset, uint16_t length, uint8_t *out, uint16_t *outGot) {
  if (offset >= handle->fileSize) {
    g_lastError = SD_ERR_OUT_OF_RANGE;
    return false;
  }
  uint32_t remaining = handle->fileSize - offset;
  uint16_t wanted = (length < remaining) ? length : (uint16_t)remaining;
  if (!handle->file.seekSet(offset)) {
    g_lastError = SD_ERR_OPEN_FAILED;
    return false;
  }
  int got = handle->file.read(out, wanted);
  if (got < 0) {
    g_lastError = SD_ERR_OPEN_FAILED;
    return false;
  }
  *outGot = (uint16_t)got;
  return true;
}

// -- floppy cache --

static void cachePath(char *out, size_t size, uint32_t diskId, uint32_t fileKey, const char *ext) {
  snprintf(out, size, "/CACHE/%08lX/%08lX.%s", (unsigned long)diskId, (unsigned long)fileKey, ext);
}

bool sd_cache_open(uint32_t diskId, uint32_t fileKey, SdFileHandle *handle) {
  if (!g_mounted) return false;
  char path[40];
  cachePath(path, sizeof(path), diskId, fileKey, "BIN");
  handle->file.close();
  if (!handle->file.open(&g_sd, path, O_RDONLY)) return false;
  handle->fileSize = (uint32_t)handle->file.fileSize();
  return true;
}

bool sd_cache_begin(uint32_t diskId, uint32_t fileKey, FsFile *out) {
  if (!g_mounted) return false;
  char dir[24];
  snprintf(dir, sizeof(dir), "/CACHE/%08lX", (unsigned long)diskId);
  if (!g_sd.exists(dir) && !g_sd.mkdir(dir, true)) return false;
  char path[40];
  cachePath(path, sizeof(path), diskId, fileKey, "TMP");
  out->close();
  return out->open(&g_sd, path, O_WRONLY | O_CREAT | O_TRUNC);
}

bool sd_cache_finish(uint32_t diskId, uint32_t fileKey, FsFile *file) {
  bool ok = file->close();
  char tmp[40], done[40];
  cachePath(tmp, sizeof(tmp), diskId, fileKey, "TMP");
  cachePath(done, sizeof(done), diskId, fileKey, "BIN");
  if (ok && g_sd.exists(done)) g_sd.remove(done);
  ok = ok && g_sd.rename(tmp, done);
  if (!ok) g_sd.remove(tmp);
  return ok;
}

void sd_cache_abandon(uint32_t diskId, uint32_t fileKey, FsFile *file) {
  file->close();
  char tmp[40];
  cachePath(tmp, sizeof(tmp), diskId, fileKey, "TMP");
  g_sd.remove(tmp);
}
