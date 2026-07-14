// floppy_fat12.h - live, on-device FAT12 + flux-capture driver. See README.md
// for the overall architecture (streaming decode, cylinder cache, etc).
#pragma once
#include <stdint.h>

// Sets up GPIOs + PIO capture program, spins up the drive, and homes to
// cylinder 0. Call once at startup before floppy_mount().
bool floppy_init();

// Lets a long multi-attempt cylinder read bail out early if either button
// pin reads pressed (raw digitalRead, not the debounced Button class - the
// point is to react while still stuck inside a single blocking call, before
// loop() ever gets to poll buttons normally). Checked only between retry
// attempts, never mid-capture, so it can't corrupt anything: an aborted
// cylinder just returns whatever sectors were already captured, the same
// outcome as a genuinely marginal disk read. Pass -1 to disable either pin.
void floppy_set_skip_pins(int pin1, int pin2);

// Reads the boot sector, FAT, and root directory into RAM. Must succeed
// before any other floppy_* call. Returns false on failure - check
// floppy_last_error() for why.
bool floppy_mount();

// Spins the drive motor up/down and selects/deselects the drive together -
// on this hardware the activity LED follows Drive Select, not Motor Enable,
// so both need to toggle together to actually turn it off. Callers must
// call floppy_motor_on() before floppy_remount()/any read - a deselected
// drive on a shared ribbon cable ignores/tristates most of its signals.
// floppy_motor_on() blocks ~600ms to let the spindle reach speed (a no-op
// if already on); floppy_motor_off() is instant.
void floppy_motor_on();
void floppy_motor_off();

// True once the drive's DSKCHG line (ribbon pin 34 on a 3.5" drive) is
// asserted - the disk was removed at some point since the last step, so any
// cached data may belong to a different, since-swapped disk. Stays asserted
// until floppy_remount() (or any other stepping) clears it.
bool floppy_disk_change_asserted();

// Steps the drive (clears DSKCHG) and invalidates the sector cache before
// re-mounting. Call once floppy_disk_change_asserted() is true, and rebuild
// the playlist on success - the old playlist's cluster/size values are for
// the old disk. Returns false the same way floppy_mount() does.
bool floppy_remount();

struct FloppyDirEntry {
  char name[9];   // null-terminated 8.3 base name, no trailing spaces
  char ext[4];    // null-terminated extension, no trailing spaces
  uint8_t attr;
  uint16_t startCluster;
  uint32_t size;
};

int floppy_root_entry_count();
const FloppyDirEntry &floppy_root_entry(int index);

// Follows a subdirectory's own cluster chain and parses it like the root
// directory. "." and ".." are dropped. Returns entry count (up to
// maxEntries), or -1 on error.
int floppy_read_subdirectory(const FloppyDirEntry &dirEntry, FloppyDirEntry *outEntries, int maxEntries);

enum FloppyError {
  FLOPPY_OK = 0,
  FLOPPY_ERR_NOT_MOUNTED,        // called something before floppy_mount() succeeded
  FLOPPY_ERR_BAD_BOOT_SECTOR,    // boot sector unreadable or not a recognizable FAT12 BPB
  FLOPPY_ERR_NO_CLUSTERS,        // a file/directory's cluster chain was empty (0-byte or corrupt entry)
  FLOPPY_ERR_TOO_MANY_CLUSTERS,  // a file's chain is longer than this build supports (see FLOPPY_MAX_FILE_CLUSTERS)
  FLOPPY_ERR_SECTOR_UNRECOVERABLE, // a specific sector never came back CRC-valid - see floppy_last_sector_failure()
  FLOPPY_ERR_OUT_OF_RANGE,       // requested a byte offset beyond the file's own length
};

FloppyError floppy_last_error();

// Only meaningful right after floppy_last_error() == FLOPPY_ERR_SECTOR_UNRECOVERABLE.
void floppy_last_sector_failure(int *cyl, int *head, int *sector);

// Cumulative count of real seek+capture attempts (cache misses) since boot -
// not cache hits. Useful as an on-screen activity/health indicator when
// there's no Serial monitor hooked up.
uint32_t floppy_read_count();

// A file's on-disk layout, opened once per file and then used by
// floppy_read_file_sector() to serve arbitrary out-of-order reads cheaply.
// Multiple handles can be open on the same file at once (one per track
// cursor, for a multi-track MIDI file's K-way merge).
#define FLOPPY_MAX_FILE_CLUSTERS 2200 // ~1.1MB of file at this disk's 512B/cluster
struct FloppyFileHandle {
  uint16_t clusters[FLOPPY_MAX_FILE_CLUSTERS];
  int clusterCount;
  uint32_t fileSize;
};

bool floppy_open_file_handle(const FloppyDirEntry &entry, FloppyFileHandle *handle);

// Reads the 512-byte sector containing file-relative byte offset fileOffset
// into out512. Returns false if out of range or unrecoverable - check
// floppy_last_error() to tell those apart.
bool floppy_read_file_sector(const FloppyFileHandle *handle, uint32_t fileOffset, uint8_t *out512);
