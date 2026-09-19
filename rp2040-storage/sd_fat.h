// sd_fat.h - SD card backend over SdFat, shaped like floppy_fat12.h. No
// card-detect pin: presence is only known from a mount succeeding.
#pragma once
#include <stdint.h>
#include <SdFat.h>

#define SD_SCK_PIN  18
#define SD_MOSI_PIN 19
#define SD_MISO_PIN 20
#define SD_CS_PIN   21

bool sd_init();    // SPI pin setup; once, before sd_mount()
bool sd_mount();
bool sd_remount();

// startCluster is a directory index relative to the parent (see sd_fat.cpp).
struct SdDirEntry {
  char name[9];
  char ext[4];
  uint8_t attr;
  uint32_t startCluster;
  uint32_t size;
};

int sd_root_entry_count();
const SdDirEntry &sd_root_entry(int index);

// Lists a directory entry from any listing; -1 on error.
int sd_read_subdirectory(const SdDirEntry &dirEntry, SdDirEntry *outEntries, int maxEntries);

enum SdError {
  SD_OK = 0,
  SD_ERR_NOT_MOUNTED,
  SD_ERR_OPEN_FAILED,
  SD_ERR_OUT_OF_RANGE,
};
SdError sd_last_error();

struct SdFileHandle {
  FsFile file; // not "File": arduino-pico's FS.h claims that name
  uint32_t fileSize;
};

bool sd_open_file_handle(const SdDirEntry &entry, SdFileHandle *handle);

// Reads up to length bytes at offset; *outGot is short at end of file.
bool sd_read_file_range(SdFileHandle *handle, uint32_t offset, uint16_t length, uint8_t *out, uint16_t *outGot);
