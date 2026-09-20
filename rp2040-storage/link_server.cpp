// link_server.cpp - answers link_protocol.h requests from the floppy and SD backends.
#include "config.h"
#include "link_server.h"
#include "link_protocol.h"
#include "floppy_fat12.h"
#include "sd_fat.h"
#include <Arduino.h>
#include <string.h>

#define LINK_UART Serial // USB CDC - one cable to the Pi carries power and data

// Per message, not per byte, so a trickle of noise can't hold the server.
#define LINK_RECEIVE_DEADLINE_MS 1000

static_assert(MAX_DIR_ENTRIES <= LINK_MAX_DIR_ENTRIES, "listing larger than one response");

static uint8_t g_rxBuf[LINK_MAX_PAYLOAD];
static uint8_t g_txBuf[sizeof(LinkHeader) + LINK_MAX_PAYLOAD + 2];

static bool g_mounted = false;   // floppy
static bool g_sdMounted = false;

// Tagged by backend rather than a union: FsFile isn't trivially copyable.
struct HandleSlot {
  bool inUse;
  uint8_t backend; // where reads come from
  uint8_t origin;  // what the Pi opened: a floppy file may be served from its SD copy
  FloppyFileHandle fh;
  SdFileHandle sdfh;
  // A floppy file being copied to the SD cache as the Pi reads it, front to back.
  bool caching;
  FsFile cacheFile;
  uint32_t cached, diskId, fileKey;
};
static HandleSlot g_handles[LINK_MAX_HANDLES];

static uint8_t g_currentSequenceId = 0; // of the request being answered
static uint32_t g_myBootId = 0;
static uint32_t g_lastClientBootId = 0;
static bool g_haveClientBootId = false;

static void stopCaching(HandleSlot &slot) {
  if (slot.caching) sd_cache_abandon(slot.diskId, slot.fileKey, &slot.cacheFile);
  slot.caching = false;
}

static void releaseHandle(HandleSlot &slot) {
  if (slot.inUse && slot.backend == LINK_BACKEND_SD) slot.sdfh.file.close();
  stopCaching(slot);
  slot.inUse = false;
}

// Only the remounted backend's handles go stale - cached floppy files count as both.
static void resetHandles(uint8_t backend) {
  for (int i = 0; i < LINK_MAX_HANDLES; i++) {
    if (g_handles[i].backend == backend || g_handles[i].origin == backend) releaseHandle(g_handles[i]);
  }
}

// Copies what the Pi just read into the cache; anything but front-to-back gives up.
static void cacheAppend(HandleSlot &h, uint32_t offset, const uint8_t *data, uint16_t len) {
  if (!h.caching || offset + len <= h.cached) return; // a re-read of what's copied already
  if (offset != h.cached || h.cacheFile.write(data, len) != len) {
    stopCaching(h);
    return;
  }
  h.cached += len;
  if (h.cached >= h.fh.fileSize) {
    if (!sd_cache_finish(h.diskId, h.fileKey, &h.cacheFile)) DBG_SERIAL.println("SD cache: could not save a copy.");
    h.caching = false;
  }
}

// A floppy file: its SD copy if there is one, else the disk, copying as it goes.
static bool openFloppyFile(HandleSlot &h, const FloppyDirEntry &entry, uint32_t *outSize) {
  h.origin = LINK_BACKEND_FLOPPY;
  h.caching = false;
#if SD_FLOPPY_CACHE
  uint32_t diskId = floppy_disk_id(), fileKey = floppy_file_key(entry);
  if (g_sdMounted && entry.size > 0 && sd_cache_open(diskId, fileKey, &h.sdfh)) {
    if (h.sdfh.fileSize == entry.size) {
      h.backend = LINK_BACKEND_SD;
      *outSize = entry.size;
      return true;
    }
    h.sdfh.file.close();
  }
#endif
  h.backend = LINK_BACKEND_FLOPPY;
  if (!floppy_open_file_handle(entry, &h.fh)) return false;
  *outSize = h.fh.fileSize;
#if SD_FLOPPY_CACHE
  if (g_sdMounted && entry.size > 0 && sd_cache_begin(diskId, fileKey, &h.cacheFile)) {
    h.caching = true;
    h.cached = 0;
    h.diskId = diskId;
    h.fileKey = fileKey;
  }
#endif
  return true;
}

static void resetAllHandles() {
  for (int i = 0; i < LINK_MAX_HANDLES; i++) releaseHandle(g_handles[i]);
}

static int allocHandle() {
  for (int i = 0; i < LINK_MAX_HANDLES; i++) {
    if (!g_handles[i].inUse) return i;
  }
  return -1;
}

// FloppyDirEntry and SdDirEntry share LinkDirEntry's field names.
template <typename T> static void toLink(const T &src, LinkDirEntry *dst) {
  memcpy(dst->name, src.name, sizeof(dst->name));
  memcpy(dst->ext, src.ext, sizeof(dst->ext));
  dst->attr = src.attr;
  dst->startCluster = src.startCluster;
  dst->size = src.size;
}

template <typename T> static void fromLink(const LinkDirEntry &src, T *dst) {
  memcpy(dst->name, src.name, sizeof(dst->name));
  memcpy(dst->ext, src.ext, sizeof(dst->ext));
  dst->attr = src.attr;
  dst->startCluster = src.startCluster;
  dst->size = src.size;
}

// -- framing --

static void sendMessage(uint8_t opcode, const void *payload, uint16_t payloadLen) {
  LinkHeader hdr;
  hdr.magic0 = LINK_MAGIC0;
  hdr.magic1 = LINK_MAGIC1;
  hdr.opcode = opcode | LINK_OP_RESPONSE_BIT;
  hdr.sequenceId = g_currentSequenceId;
  hdr.bootId = g_myBootId;
  hdr.payloadLen = payloadLen;

  memcpy(g_txBuf, &hdr, sizeof(hdr));
  if (payloadLen > 0) memcpy(g_txBuf + sizeof(hdr), payload, payloadLen);
  uint16_t crc = link_crc16(g_txBuf + 2, (uint16_t)(sizeof(hdr) - 2 + payloadLen));
  g_txBuf[sizeof(hdr) + payloadLen] = (uint8_t)(crc & 0xFF);
  g_txBuf[sizeof(hdr) + payloadLen + 1] = (uint8_t)(crc >> 8);

  // SerialUSB gives up by itself if the host stops reading, so this can't hang.
  LINK_UART.write(g_txBuf, sizeof(hdr) + payloadLen + 2);
}

static bool readExactUntilDeadline(uint8_t *buf, size_t len, uint32_t deadlineMs) {
  size_t got = 0;
  while (got < len) {
    if ((int32_t)(millis() - deadlineMs) >= 0) return false;
    if (LINK_UART.available() > 0) {
      int b = LINK_UART.read();
      if (b >= 0) buf[got++] = (uint8_t)b;
      continue;
    }
    yield();
  }
  return true;
}

static bool waitForMagic(uint32_t deadlineMs) {
  bool haveMagic0 = false;
  while (true) {
    uint8_t b;
    if (!readExactUntilDeadline(&b, 1, deadlineMs)) return false;
    if (!haveMagic0) {
      haveMagic0 = (b == LINK_MAGIC0);
    } else if (b == LINK_MAGIC1) {
      return true;
    } else {
      haveMagic0 = (b == LINK_MAGIC0);
    }
  }
}

// A rebooted client will never CLOSE what its previous session opened.
static void checkClientBootId(uint32_t clientBootId) {
  if (g_haveClientBootId && clientBootId != g_lastClientBootId) {
    DBG_SERIAL.println("Client rebooted - resetting handles.");
    resetAllHandles();
  }
  g_lastClientBootId = clientBootId;
  g_haveClientBootId = true;
}

// Payload lands in g_rxBuf. On timeout or a bad frame, stay silent; the client retries.
static bool receiveMessage(uint8_t *outOpcode, uint16_t *outPayloadLen) {
  uint32_t deadlineMs = millis() + LINK_RECEIVE_DEADLINE_MS;
  if (!waitForMagic(deadlineMs)) return false;

  uint8_t rest[8]; // opcode, sequenceId, bootId (4), payloadLen (2)
  if (!readExactUntilDeadline(rest, 8, deadlineMs)) return false;
  uint32_t clientBootId = (uint32_t)rest[2] | ((uint32_t)rest[3] << 8) | ((uint32_t)rest[4] << 16) | ((uint32_t)rest[5] << 24);
  uint16_t payloadLen = (uint16_t)rest[6] | ((uint16_t)rest[7] << 8);
  if (payloadLen > LINK_MAX_PAYLOAD) return false;
  if (payloadLen > 0 && !readExactUntilDeadline(g_rxBuf, payloadLen, deadlineMs)) return false;

  uint8_t crcBytes[2];
  if (!readExactUntilDeadline(crcBytes, 2, deadlineMs)) return false;
  uint16_t receivedCrc = (uint16_t)crcBytes[0] | ((uint16_t)crcBytes[1] << 8);

  static uint8_t crcCheckBuf[8 + LINK_MAX_PAYLOAD];
  memcpy(crcCheckBuf, rest, 8);
  if (payloadLen > 0) memcpy(crcCheckBuf + 8, g_rxBuf, payloadLen);
  if (link_crc16(crcCheckBuf, (uint16_t)(8 + payloadLen)) != receivedCrc) return false;

  checkClientBootId(clientBootId);
  *outOpcode = rest[0];
  g_currentSequenceId = rest[1];
  *outPayloadLen = payloadLen;
  return true;
}

static void sendStatus(uint8_t opcode, uint8_t status) {
  sendMessage(opcode, &status, 1);
}

// -- handlers: each answers exactly once --

static void handleHello() {
  LinkHelloRequest *req = (LinkHelloRequest *)g_rxBuf;
  LinkHelloResponse resp;
  resp.serverProtocolVersion = LINK_PROTOCOL_VERSION;
  resp.status = (req->clientProtocolVersion == LINK_PROTOCOL_VERSION)
                    ? LINK_STATUS_OK
                    : LINK_STATUS_VERSION_MISMATCH;
  sendMessage(LINK_OP_HELLO, &resp, sizeof(resp));
}

static void sendListing(uint8_t opcode, uint8_t status, uint8_t *respBuf, int count) {
  if (status != LINK_STATUS_OK || count < 0) {
    status = (status == LINK_STATUS_OK) ? LINK_STATUS_ERROR : status;
    count = 0;
  }
  respBuf[0] = status;
  respBuf[1] = (uint8_t)(count & 0xFF);
  respBuf[2] = (uint8_t)(count >> 8);
  sendMessage(opcode, respBuf, (uint16_t)(3 + count * sizeof(LinkDirEntry)));
}

static void handleListRoot() {
  LinkListRootRequest *req = (LinkListRootRequest *)g_rxBuf;
  static uint8_t respBuf[LINK_MAX_PAYLOAD];
  LinkDirEntry *out = (LinkDirEntry *)(respBuf + 3);
  int count = 0;
  uint8_t status = LINK_STATUS_OK;

  if (req->backend == LINK_BACKEND_FLOPPY) {
    if (!g_mounted) status = LINK_STATUS_ERROR;
    else {
      count = floppy_root_entry_count();
      for (int i = 0; i < count; i++) toLink(floppy_root_entry(i), &out[i]);
    }
  } else if (req->backend == LINK_BACKEND_SD) {
    if (!g_sdMounted) status = LINK_STATUS_ERROR;
    else {
      count = sd_root_entry_count();
      for (int i = 0; i < count; i++) toLink(sd_root_entry(i), &out[i]);
    }
  } else {
    status = LINK_STATUS_NOT_IMPLEMENTED;
  }
  sendListing(LINK_OP_LIST_ROOT, status, respBuf, count);
}

static void handleListSubdir() {
  LinkListSubdirRequest *req = (LinkListSubdirRequest *)g_rxBuf;
  static uint8_t respBuf[LINK_MAX_PAYLOAD];
  LinkDirEntry *out = (LinkDirEntry *)(respBuf + 3);
  int count = 0;
  uint8_t status = LINK_STATUS_OK;

  if (req->backend == LINK_BACKEND_FLOPPY) {
    if (!g_mounted) status = LINK_STATUS_ERROR;
    else {
      FloppyDirEntry dir;
      fromLink(req->dirEntry, &dir);
      static FloppyDirEntry entries[MAX_DIR_ENTRIES];
      count = floppy_read_subdirectory(dir, entries, MAX_DIR_ENTRIES);
      for (int i = 0; i < count; i++) toLink(entries[i], &out[i]);
    }
  } else if (req->backend == LINK_BACKEND_SD) {
    if (!g_sdMounted) status = LINK_STATUS_ERROR;
    else {
      SdDirEntry dir;
      fromLink(req->dirEntry, &dir);
      static SdDirEntry entries[MAX_DIR_ENTRIES];
      count = sd_read_subdirectory(dir, entries, MAX_DIR_ENTRIES);
      for (int i = 0; i < count; i++) toLink(entries[i], &out[i]);
    }
  } else {
    status = LINK_STATUS_NOT_IMPLEMENTED;
  }
  sendListing(LINK_OP_LIST_SUBDIR, status, respBuf, count);
}

static void handleOpen() {
  LinkOpenRequest *req = (LinkOpenRequest *)g_rxBuf;
  LinkOpenResponse resp;
  memset(&resp, 0, sizeof(resp)); // index.valid stays 0

  int slot = -1;
  if (req->mode != LINK_OPEN_READ ||
      (req->backend != LINK_BACKEND_FLOPPY && req->backend != LINK_BACKEND_SD)) {
    resp.status = LINK_STATUS_NOT_IMPLEMENTED;
  } else if (!(req->backend == LINK_BACKEND_FLOPPY ? g_mounted : g_sdMounted)) {
    resp.status = LINK_STATUS_ERROR;
  } else if ((slot = allocHandle()) < 0) {
    resp.status = LINK_STATUS_ERROR; // pool exhausted
  } else {
    HandleSlot &h = g_handles[slot];
    bool ok;
    if (req->backend == LINK_BACKEND_FLOPPY) {
      FloppyDirEntry entry;
      fromLink(req->entry, &entry);
      uint32_t size = 0;
      ok = openFloppyFile(h, entry, &size);
      resp.fileSize = size; // not &resp.fileSize: the struct is packed
    } else {
      SdDirEntry entry;
      fromLink(req->entry, &entry);
      ok = sd_open_file_handle(entry, &h.sdfh);
      resp.fileSize = h.sdfh.fileSize;
      h.backend = h.origin = LINK_BACKEND_SD;
      h.caching = false;
    }
    if (ok) {
      h.inUse = true;
      resp.status = LINK_STATUS_OK;
      resp.handle = (uint8_t)slot;
    } else {
      resp.status = LINK_STATUS_ERROR;
      resp.fileSize = 0;
    }
  }
  sendMessage(LINK_OP_OPEN, &resp, sizeof(resp));
}

static void handleRead() {
  LinkReadRequest *req = (LinkReadRequest *)g_rxBuf;
  static uint8_t respBuf[3 + LINK_MAX_READ_BYTES];
  uint8_t status = LINK_STATUS_OK;
  uint16_t bytesReturned = 0;
  uint16_t wanted = req->length > LINK_MAX_READ_BYTES ? LINK_MAX_READ_BYTES : req->length;

  if (req->handle >= LINK_MAX_HANDLES || !g_handles[req->handle].inUse) {
    status = LINK_STATUS_BAD_HANDLE;
  } else if (g_handles[req->handle].backend == LINK_BACKEND_SD) {
    if (!sd_read_file_range(&g_handles[req->handle].sdfh, req->offset, wanted, respBuf + 3, &bytesReturned)) {
      status = LINK_STATUS_ERROR;
    }
  } else {
    FloppyFileHandle *fh = &g_handles[req->handle].fh;
    if (req->offset >= fh->fileSize) {
      status = LINK_STATUS_ERROR;
    } else {
      if (wanted > fh->fileSize - req->offset) wanted = (uint16_t)(fh->fileSize - req->offset);
      // The floppy serves whole sectors; copy out the overlapping slice of each.
      uint32_t pos = req->offset;
      uint16_t got = 0;
      uint8_t sector[512];
      while (got < wanted) {
        if (!floppy_read_file_sector(fh, pos - (pos % 512), sector)) {
          status = LINK_STATUS_ERROR; // fail the whole request, never return a partial one
          break;
        }
        uint16_t within = (uint16_t)(pos % 512);
        uint16_t chunk = 512 - within;
        if (chunk > wanted - got) chunk = wanted - got;
        memcpy(respBuf + 3 + got, sector + within, chunk);
        got += chunk;
        pos += chunk;
      }
      if (status == LINK_STATUS_OK) {
        bytesReturned = got;
        cacheAppend(g_handles[req->handle], req->offset, respBuf + 3, got);
      }
    }
  }

  respBuf[0] = status;
  respBuf[1] = (uint8_t)(bytesReturned & 0xFF);
  respBuf[2] = (uint8_t)(bytesReturned >> 8);
  sendMessage(LINK_OP_READ, respBuf, (uint16_t)(3 + bytesReturned));
}

static void handlePoll(uint8_t opcode, uint8_t value) {
  LinkPollResponse resp;
  resp.status = LINK_STATUS_OK;
  resp.asserted = value;
  sendMessage(opcode, &resp, sizeof(resp));
}

static void handleRemount() {
  LinkRemountRequest *req = (LinkRemountRequest *)g_rxBuf;
  uint8_t status;
  if (req->backend == LINK_BACKEND_FLOPPY) {
    resetHandles(LINK_BACKEND_FLOPPY); // their cluster chains belong to the old disk
    g_mounted = floppy_remount();
    status = g_mounted ? LINK_STATUS_OK : LINK_STATUS_ERROR;
  } else if (req->backend == LINK_BACKEND_SD) {
    resetHandles(LINK_BACKEND_SD);
    g_sdMounted = sd_remount();
    status = g_sdMounted ? LINK_STATUS_OK : LINK_STATUS_ERROR;
  } else {
    status = LINK_STATUS_NOT_IMPLEMENTED;
  }
  sendStatus(LINK_OP_REMOUNT, status);
}

static void handleClose() {
  LinkCloseRequest *req = (LinkCloseRequest *)g_rxBuf;
  if (req->handle >= LINK_MAX_HANDLES) {
    sendStatus(LINK_OP_CLOSE, LINK_STATUS_BAD_HANDLE);
    return;
  }
  releaseHandle(g_handles[req->handle]);
  sendStatus(LINK_OP_CLOSE, LINK_STATUS_OK);
}

// -- entry points --

void link_server_init() {
  LINK_UART.begin(); // doesn't wait for a host - boots fine with the Pi off
  g_myBootId = rp2040.hwrand32();

  g_mounted = floppy_mount();
  DBG_SERIAL.println(g_mounted ? "Floppy mounted." : "Floppy not mounted.");
  sd_init();
  g_sdMounted = sd_mount();
  DBG_SERIAL.println(g_sdMounted ? "SD card mounted." : "SD card not mounted.");
  resetAllHandles();
}

void link_server_poll() {
  uint8_t opcode;
  uint16_t payloadLen;
  if (!receiveMessage(&opcode, &payloadLen)) return;

  switch (opcode) {
    case LINK_OP_HELLO:            handleHello(); break;
    case LINK_OP_LIST_ROOT:        handleListRoot(); break;
    case LINK_OP_LIST_SUBDIR:      handleListSubdir(); break;
    case LINK_OP_OPEN:             handleOpen(); break;
    case LINK_OP_READ:             handleRead(); break;
    case LINK_OP_WRITE:            sendStatus(LINK_OP_WRITE, LINK_STATUS_NOT_IMPLEMENTED); break;
    case LINK_OP_DISK_CHANGE_POLL: handlePoll(opcode, floppy_poll_disk_change()); break;
    case LINK_OP_CARD_POLL:        handlePoll(opcode, g_sdMounted ? 1 : 0); break;
    case LINK_OP_REMOUNT:          handleRemount(); break;
    case LINK_OP_CLOSE:            handleClose(); break;
    default: break; // unknown opcode: stay silent, like any dropped frame
  }
}
