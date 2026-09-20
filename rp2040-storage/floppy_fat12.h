// floppy_fat12.h - PIO flux capture, MFM decode, cylinder cache, read-only FAT12.
#pragma once
#include <stdint.h>

// Sets up the pins and PIO, spins up the drive and homes to cylinder 0.
bool floppy_init();

// Reads the boot sector, FAT and root; floppy_last_error() says why it failed.
bool floppy_mount();

// Stops an idle spindle (FLOPPY_MOTOR_IDLE_MS). Call every loop().
void floppy_idle();

// DSKCHG (ribbon pin 34): the disk was removed since the last step.
bool floppy_disk_change_asserted();

// 0 no change, 1 a new disk is in (until remounted), 2 the drive is empty. While
// DSKCHG says removed, steps the head now and then - no spin-up - to look for a disk.
uint8_t floppy_poll_disk_change();

// Clears DSKCHG, drops the cache and mounts again. Rebuild the playlist after.
bool floppy_remount();

struct FloppyDirEntry {
  char name[9]; // null-terminated, no padding
  char ext[4];
  uint8_t attr;
  uint16_t startCluster;
  uint32_t size;
};

int floppy_root_entry_count();
const FloppyDirEntry &floppy_root_entry(int index);

// Fingerprint of the mounted disk (boot sector, FAT and root directory) and of
// one file on it: together they name the file's copy in the SD cache.
uint32_t floppy_disk_id();
uint32_t floppy_file_key(const FloppyDirEntry &entry);

// Entries of a subdirectory, without "." and ".."; -1 on error.
int floppy_read_subdirectory(const FloppyDirEntry &dirEntry, FloppyDirEntry *outEntries, int maxEntries);

enum FloppyError {
  FLOPPY_OK = 0,
  FLOPPY_ERR_NOT_MOUNTED,
  FLOPPY_ERR_BAD_BOOT_SECTOR,
  FLOPPY_ERR_NO_CLUSTERS,          // empty or corrupt cluster chain
  FLOPPY_ERR_TOO_MANY_CLUSTERS,    // file longer than FLOPPY_MAX_FILE_CLUSTERS
  FLOPPY_ERR_SECTOR_UNRECOVERABLE, // a sector never came back CRC-valid
  FLOPPY_ERR_OUT_OF_RANGE,
};

FloppyError floppy_last_error();

// A file's cluster chain, resolved once at open.
#define FLOPPY_MAX_FILE_CLUSTERS 2200 // ~1.1MB at 512B clusters
struct FloppyFileHandle {
  uint16_t clusters[FLOPPY_MAX_FILE_CLUSTERS];
  int clusterCount;
  uint32_t fileSize;
};

bool floppy_open_file_handle(const FloppyDirEntry &entry, FloppyFileHandle *handle);

// Reads the 512-byte sector holding file offset fileOffset.
bool floppy_read_file_sector(const FloppyFileHandle *handle, uint32_t fileOffset, uint8_t *out512);
