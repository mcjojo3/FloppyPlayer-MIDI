"""Client for the RP2040 storage server: implements the wire protocol in
rp2040-storage/link_protocol.h (v4) over USB CDC (/dev/ttyACM0).
"""

from __future__ import annotations

import random
import struct
import time
from dataclasses import dataclass

import serial

PROTOCOL_VERSION = 4

MAGIC0, MAGIC1 = 0xA5, 0x5A
RESPONSE_BIT = 0x80

OP_HELLO = 0x01
OP_LIST_ROOT = 0x02
OP_LIST_SUBDIR = 0x03
OP_OPEN = 0x04
OP_READ = 0x05
OP_DISK_CHANGE_POLL = 0x07
OP_CARD_POLL = 0x08
OP_REMOUNT = 0x09
OP_CLOSE = 0x0A

BACKEND_FLOPPY = 0
BACKEND_SD = 1

OPEN_READ = 0

# DISK_CHANGE_POLL answers. Firmware before the empty-drive probe only says 0 or 1.
DISK_SAME = 0
DISK_NEW = 1    # changed, and a disk is in: remount
DISK_EMPTY = 2  # changed, and the drive is empty

STATUS_OK = 0
STATUS_NAMES = {
    0: "OK",
    1: "ERROR",
    2: "NOT_IMPLEMENTED",
    3: "BAD_HANDLE",
    4: "VERSION_MISMATCH",
}

MAX_READ_BYTES = 1024
MAX_PAYLOAD = 1200

_BODY_FMT = "<BBIH"  # opcode, sequenceId, bootId, payloadLen
_BODY_SIZE = struct.calcsize(_BODY_FMT)
_ENTRY_FMT = "<9s4sBII"
ENTRY_SIZE = struct.calcsize(_ENTRY_FMT)
_FILE_INDEX_SIZE = 1 + 2 + 4 + 1 + 64 * 8  # LinkFileIndex, never populated

# A marginal floppy cylinder can take ~7.5s of re-reads before the RP2040 answers.
RESPONSE_TIMEOUT_S = 10.0


class LinkError(Exception):
    pass


class LinkTimeout(LinkError):
    pass


class LinkStatusError(LinkError):
    def __init__(self, opcode: int, status: int):
        self.opcode = opcode
        self.status = status
        super().__init__(
            f"opcode 0x{opcode:02X} returned {STATUS_NAMES.get(status, status)}"
        )


def crc16(data: bytes) -> int:
    """CRC16-CCITT, poly 0x1021, init 0xFFFF - matches link_crc16()."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class DirEntry:
    name: str
    ext: str
    attr: int
    start_cluster: int
    size: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attr & 0x10)

    @property
    def is_volume_label(self) -> bool:
        return bool(self.attr & 0x08)

    @property
    def filename(self) -> str:
        return f"{self.name}.{self.ext}" if self.ext else self.name

    def pack(self) -> bytes:
        return struct.pack(
            _ENTRY_FMT,
            self.name.encode("ascii", "replace"),
            self.ext.encode("ascii", "replace"),
            self.attr,
            self.start_cluster,
            self.size,
        )

    @classmethod
    def unpack(cls, raw: bytes) -> "DirEntry":
        name, ext, attr, cluster, size = struct.unpack(_ENTRY_FMT, raw)
        return cls(
            name=name.split(b"\0", 1)[0].decode("ascii", "replace"),
            ext=ext.split(b"\0", 1)[0].decode("ascii", "replace"),
            attr=attr,
            start_cluster=cluster,
            size=size,
        )


class LinkClient:
    def __init__(self, port: str = "/dev/ttyACM0", timeout: float = RESPONSE_TIMEOUT_S):
        # baudrate is ignored over CDC but pyserial requires one.
        self._ser = serial.Serial(port, baudrate=115200, timeout=0.05)
        self._timeout = timeout
        self._seq = 0
        self._boot_id = random.getrandbits(32)
        self._peer_boot_id: int | None = None
        self._peer_changed = False

    def close(self) -> None:
        self._ser.close()

    # -- framing ---------------------------------------------------------

    def _read_exact(self, count: int, deadline: float) -> bytes:
        """Read exactly count bytes or raise; the deadline holds even under a trickle of noise."""
        buf = bytearray()
        while len(buf) < count:
            if time.monotonic() >= deadline:
                raise LinkTimeout(f"wanted {count} bytes, got {len(buf)}")
            chunk = self._ser.read(count - len(buf))
            if chunk:
                buf += chunk
        return bytes(buf)

    def _wait_for_magic(self, deadline: float) -> None:
        have0 = False
        while True:
            byte = self._read_exact(1, deadline)[0]
            if not have0:
                have0 = byte == MAGIC0
            elif byte == MAGIC1:
                return
            else:
                have0 = byte == MAGIC0

    def _transact(self, opcode: int, payload: bytes = b"") -> bytes:
        # A late response to an abandoned request must not look like this one.
        self._ser.reset_input_buffer()

        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        body = struct.pack(_BODY_FMT, opcode, seq, self._boot_id, len(payload)) + payload
        self._ser.write(bytes((MAGIC0, MAGIC1)) + body + struct.pack("<H", crc16(body)))
        self._ser.flush()

        deadline = time.monotonic() + self._timeout
        self._wait_for_magic(deadline)
        head = self._read_exact(_BODY_SIZE, deadline)
        resp_op, resp_seq, peer_boot, payload_len = struct.unpack(_BODY_FMT, head)
        if payload_len > MAX_PAYLOAD:
            raise LinkError(f"implausible payload length {payload_len}")
        resp_payload = self._read_exact(payload_len, deadline) if payload_len else b""
        (crc_in,) = struct.unpack("<H", self._read_exact(2, deadline))

        if crc16(head + resp_payload) != crc_in:
            raise LinkError("CRC mismatch")
        if resp_op != (opcode | RESPONSE_BIT):
            raise LinkError(f"expected opcode 0x{opcode | RESPONSE_BIT:02X}, got 0x{resp_op:02X}")
        if resp_seq != seq:
            raise LinkError(f"stale response (seq {resp_seq}, wanted {seq})")

        if self._peer_boot_id is None:
            self._peer_boot_id = peer_boot
        elif peer_boot != self._peer_boot_id:
            self._peer_boot_id = peer_boot
            self._peer_changed = True
        return resp_payload

    def _checked(self, opcode: int, payload: bytes = b"") -> bytes:
        resp = self._transact(opcode, payload)
        if not resp:
            raise LinkError(f"opcode 0x{opcode:02X} returned an empty payload")
        if resp[0] != STATUS_OK:
            raise LinkStatusError(opcode, resp[0])
        return resp

    # -- peer reboot detection -------------------------------------------

    def take_peer_changed(self) -> bool:
        """True once after the RP2040 reboots: handles are gone and hello() must run again."""
        changed, self._peer_changed = self._peer_changed, False
        return changed

    # -- operations ------------------------------------------------------

    def hello(self) -> int:
        resp = self._transact(OP_HELLO, struct.pack("<I", PROTOCOL_VERSION))
        status, server_version = struct.unpack("<BI", resp[:5])
        if status != STATUS_OK:
            raise LinkStatusError(OP_HELLO, status)
        if server_version != PROTOCOL_VERSION:
            raise LinkError(
                f"protocol mismatch: server v{server_version}, client v{PROTOCOL_VERSION}"
            )
        # A fresh handshake is the baseline, not a reboot to react to.
        self._peer_changed = False
        return server_version

    def _parse_listing(self, resp: bytes) -> list[DirEntry]:
        (count,) = struct.unpack("<H", resp[1:3])
        body = resp[3:]
        return [
            DirEntry.unpack(body[i * ENTRY_SIZE : (i + 1) * ENTRY_SIZE])
            for i in range(min(count, len(body) // ENTRY_SIZE))
        ]

    def list_root(self, backend: int) -> list[DirEntry]:
        return self._parse_listing(self._checked(OP_LIST_ROOT, struct.pack("<B", backend)))

    def list_subdir(self, backend: int, entry: DirEntry) -> list[DirEntry]:
        payload = struct.pack("<B", backend) + entry.pack()
        return self._parse_listing(self._checked(OP_LIST_SUBDIR, payload))

    def open(self, backend: int, entry: DirEntry, mode: int = OPEN_READ) -> tuple[int, int]:
        """Returns (handle, size). Always close it - the server has 2 handles."""
        payload = struct.pack("<BB", backend, mode) + entry.pack()
        resp = self._checked(OP_OPEN, payload)
        _, handle, file_size = struct.unpack("<BBI", resp[:6])
        return handle, file_size

    def read(self, handle: int, offset: int, length: int) -> bytes:
        length = min(length, MAX_READ_BYTES)
        resp = self._checked(OP_READ, struct.pack("<BIH", handle, offset, length))
        (returned,) = struct.unpack("<H", resp[1:3])
        return resp[3 : 3 + returned]

    def read_range(self, handle: int, offset: int, length: int) -> bytes:
        """Chunked read. A short chunk means EOF, not an error."""
        out = bytearray()
        while len(out) < length:
            want = min(length - len(out), MAX_READ_BYTES)
            chunk = self.read(handle, offset + len(out), want)
            out += chunk
            if len(chunk) < want:
                break
        return bytes(out)

    def close_handle(self, handle: int) -> None:
        # BAD_HANDLE here just means it was already closed - not an error.
        try:
            self._checked(OP_CLOSE, struct.pack("<B", handle))
        except LinkStatusError:
            pass

    def disk_state(self) -> int:
        """DISK_SAME, DISK_NEW or DISK_EMPTY."""
        return self._checked(OP_DISK_CHANGE_POLL)[1]

    def card_poll(self) -> bool:
        return self._checked(OP_CARD_POLL)[1] != 0

    def remount(self, backend: int) -> bool:
        try:
            self._checked(OP_REMOUNT, struct.pack("<B", backend))
            return True
        except LinkError:
            return False
