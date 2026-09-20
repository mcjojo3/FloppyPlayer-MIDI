// link_protocol.h - wire protocol to pi-player/link_client.py; keep the two in
// step. The RP2040 only ever replies; retrying is the client's decision.
#pragma once
#include <stdint.h>

// Bump on any wire-format change; HELLO refuses a mismatch.
#define LINK_PROTOCOL_VERSION 4u

// Frame: LinkHeader, payload, then CRC16-CCITT (LE) over everything after the magic.

#define LINK_MAGIC0 0xA5
#define LINK_MAGIC1 0x5A

#define LINK_OP_RESPONSE_BIT 0x80 // set on the opcode of every response

#pragma pack(push, 1)
struct LinkHeader {
  uint8_t magic0, magic1;
  uint8_t opcode;
  // Echoed back, so a late reply to an abandoned request can't be mistaken.
  uint8_t sequenceId;
  // Random per boot; a change means the other side rebooted.
  uint32_t bootId;
  uint16_t payloadLen;
};
#pragma pack(pop)

enum LinkOpcode {
  LINK_OP_HELLO            = 0x01, // must come first
  LINK_OP_LIST_ROOT        = 0x02,
  LINK_OP_LIST_SUBDIR      = 0x03,
  LINK_OP_OPEN             = 0x04,
  LINK_OP_READ             = 0x05,
  LINK_OP_WRITE            = 0x06, // reserved - always NOT_IMPLEMENTED
  LINK_OP_DISK_CHANGE_POLL = 0x07,
  LINK_OP_CARD_POLL        = 0x08,
  LINK_OP_REMOUNT          = 0x09,
  LINK_OP_CLOSE            = 0x0A,
};

enum LinkBackend {
  LINK_BACKEND_FLOPPY = 0,
  LINK_BACKEND_SD     = 1,
};

// First byte of every response payload.
enum LinkStatus {
  LINK_STATUS_OK               = 0,
  LINK_STATUS_ERROR            = 1,
  LINK_STATUS_NOT_IMPLEMENTED  = 2,
  LINK_STATUS_BAD_HANDLE       = 3,
  LINK_STATUS_VERSION_MISMATCH = 4,
};

#pragma pack(push, 1)
struct LinkHelloRequest {
  uint32_t clientProtocolVersion;
};
struct LinkHelloResponse {
  uint8_t status; // OK or VERSION_MISMATCH
  uint32_t serverProtocolVersion;
};
#pragma pack(pop)

// LIST_ROOT / LIST_SUBDIR reply: status, entryCount (uint16), then the entries.
#pragma pack(push, 1)
struct LinkDirEntry {
  char name[9]; // null-terminated
  char ext[4];  // null-terminated
  uint8_t attr; // FAT attributes: 0x10 directory, 0x08 volume label
  // Opaque to the client: a cluster on floppy, a directory index on SD (sd_fat.cpp).
  uint32_t startCluster;
  uint32_t size;
};

struct LinkListRootRequest {
  uint8_t backend;
};

struct LinkListSubdirRequest {
  uint8_t backend;
  LinkDirEntry dirEntry;
};
#pragma pack(pop)

// OPEN returns a handle for READ; always CLOSE it (LINK_MAX_HANDLES).
#define LINK_MAX_MIDI_TRACKS 64

#pragma pack(push, 1)
struct LinkMidiTrackRange {
  uint32_t offset;
  uint32_t length;
};

// Reserved space in the OPEN response; never populated (valid is always 0).
struct LinkFileIndex {
  uint8_t valid;
  uint16_t ticksPerQuarter;
  uint32_t initialTempoUsPerQuarter;
  uint8_t trackCount;
  LinkMidiTrackRange tracks[LINK_MAX_MIDI_TRACKS];
};

enum LinkOpenMode {
  LINK_OPEN_READ = 0, // the only supported mode; 1 and 2 are retired
};

struct LinkOpenRequest {
  uint8_t backend;
  uint8_t mode;
  LinkDirEntry entry;
};

struct LinkOpenResponse {
  uint8_t status;
  uint8_t handle;
  uint32_t fileSize;
  LinkFileIndex index;
};
#pragma pack(pop)

// READ reply: status, bytesReturned (uint16), data. Short means end of file.
#define LINK_MAX_READ_BYTES 1024u

#pragma pack(push, 1)
struct LinkReadRequest {
  uint8_t handle;
  uint32_t offset;
  uint16_t length; // clamped to LINK_MAX_READ_BYTES
};
#pragma pack(pop)

// DISK_CHANGE_POLL / CARD_POLL (SD mounted); no request payload. For the floppy,
// asserted is a LinkDiskState: a new disk stays reported until REMOUNT.
enum LinkDiskState : uint8_t {
  LINK_DISK_SAME  = 0,
  LINK_DISK_NEW   = 1,
  LINK_DISK_EMPTY = 2,
};
#pragma pack(push, 1)
struct LinkPollResponse {
  uint8_t status;
  uint8_t asserted;
};
#pragma pack(pop)

// REMOUNT / CLOSE. Closing an already-closed handle returns OK.
#pragma pack(push, 1)
struct LinkRemountRequest {
  uint8_t backend;
};

struct LinkRemountResponse {
  uint8_t status;
};

struct LinkCloseRequest {
  uint8_t handle;
};

struct LinkCloseResponse {
  uint8_t status;
};
#pragma pack(pop)

#define LINK_MAX_PAYLOAD 1200u // fits a full READ response and a directory listing
#define LINK_MAX_DIR_ENTRIES ((LINK_MAX_PAYLOAD - 3) / sizeof(LinkDirEntry)) // 54 per listing
#define LINK_MAX_HANDLES 2

static inline uint16_t link_crc16(const uint8_t *data, uint16_t len) {
  uint16_t crc = 0xFFFF;
  for (uint16_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) {
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
  }
  return crc;
}
