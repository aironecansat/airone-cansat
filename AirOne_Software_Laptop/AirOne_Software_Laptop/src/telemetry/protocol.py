"""Deterministic binary telemetry frame protocol for AirOne.

Frame layout (all multi-byte integers little-endian)::

    Offset  Size  Field
    0       4     MAGIC bytes: A1 60 4E 45
    4       1     VERSION: 0x71
    5       1     PACKET_TYPE
    6       4     SEQUENCE_NUMBER  (uint32 LE)
    10      8     TIMESTAMP_US     (uint64 LE, microseconds since Unix epoch)
    18      2     PAYLOAD_LENGTH   (uint16 LE)
    20      1     FLAGS  (bit0=FEC_PRESENT, bit1=COMPRESSED, bit2=ENCRYPTED,
                          bit3=AUTHENTICATED)
    21      N     PAYLOAD
    21+N    8     AUTH_TAG  — present ONLY when FLAGS bit3 is set:
                  first 8 bytes of HMAC-SHA256(link_key, bytes[0 .. 21+N-1])
    21+N(+8) 4    CRC32 over every preceding byte (uint32 LE)

The CRC32 is computed with ``binascii.crc32`` masked to 32 bits and packed
little-endian. It covers header, payload and (if present) the auth tag.

**Frame authentication (optional).** When both ends share a link key
(``AIRONE_LINK_KEY`` on the ground, ``AIRONE_LINK_KEY_HEX`` in the firmware)
the transmitter sets ``FLAG_AUTHENTICATED`` and appends a truncated
HMAC-SHA256 tag. The receiver recomputes the tag with a constant-time compare.
A 64-bit tag gives a forgery probability of 2^-64 per attempt, which is far
beyond what a LoRa link budget (a few frames per second) allows. The tag
provides *authenticity and integrity*, not confidentiality — payloads remain
readable on air (documented in ``docs/security_model.md``).

PAYLOAD_LENGTH always counts payload bytes only; the tag is not included, so
un-authenticated receivers can still frame-sync (they will simply reject the
frame at CRC/flag level rather than mis-parse it).
"""
from __future__ import annotations

import binascii
import hashlib
import hmac
import struct
from enum import IntEnum
from typing import Dict, Optional, Tuple

from ..core.errors import CRCError, FrameError, PayloadTooLargeError

MAGIC = bytes([0xA1, 0x60, 0x4E, 0x45])
VERSION = 0x71

HEADER_SIZE = 21  # bytes before payload
CRC_SIZE = 4
AUTH_TAG_SIZE = 8
MAX_PAYLOAD = 65535  # bounded by uint16 PAYLOAD_LENGTH
MIN_LINK_KEY_BYTES = 16

# Header struct: 4s MAGIC, B VERSION, B TYPE, I SEQ, Q TS, H LEN, B FLAGS
_HEADER_STRUCT = struct.Struct("<4sBBIQHB")
assert _HEADER_STRUCT.size == HEADER_SIZE


# FLAGS bit definitions
FLAG_FEC_PRESENT = 0x01
FLAG_COMPRESSED = 0x02
FLAG_ENCRYPTED = 0x04
FLAG_AUTHENTICATED = 0x08
KNOWN_FLAGS = FLAG_FEC_PRESENT | FLAG_COMPRESSED | FLAG_ENCRYPTED | FLAG_AUTHENTICATED


class PacketType(IntEnum):
    SENSOR_DATA = 0x01
    GPS = 0x02
    SYSTEM_STATUS = 0x03
    COMMAND = 0x04
    ACK = 0x05
    HEARTBEAT = 0x06
    FEC_DATA = 0x07
    ERROR = 0x08


VALID_PACKET_TYPES = frozenset(int(t) for t in PacketType)


class UnknownVersionError(FrameError):
    """Frame declares a protocol version this receiver does not implement."""


class UnknownPacketTypeError(FrameError):
    """Frame declares a PACKET_TYPE outside :class:`PacketType`."""


class AuthTagError(FrameError):
    """Authenticated frame whose tag does not verify (or key missing)."""


def crc32(data: bytes) -> int:
    """Return the CRC32 of ``data`` masked to 32 bits."""

    return binascii.crc32(data) & 0xFFFFFFFF


# --- link authentication ---------------------------------------------------
def parse_link_key(hex_key: Optional[str]) -> Optional[bytes]:
    """Decode a hex link key; ``None``/empty → no key. Enforces a minimum
    length so a trivially short key cannot be configured by accident."""

    if hex_key is None:
        return None
    hex_key = hex_key.strip()
    if not hex_key:
        return None
    try:
        key = bytes.fromhex(hex_key)
    except ValueError as exc:
        raise ValueError("link key must be hexadecimal") from exc
    if len(key) < MIN_LINK_KEY_BYTES:
        raise ValueError(f"link key must be at least {MIN_LINK_KEY_BYTES} bytes ({MIN_LINK_KEY_BYTES * 2} hex chars)")
    return key


def compute_auth_tag(key: bytes, header_and_payload: bytes) -> bytes:
    """Truncated HMAC-SHA256 (first ``AUTH_TAG_SIZE`` bytes)."""

    return hmac.new(key, header_and_payload, hashlib.sha256).digest()[:AUTH_TAG_SIZE]


def frame_total_size(payload_length: int, flags: int = 0) -> int:
    """Total wire size for a frame with ``payload_length`` payload bytes."""

    tag = AUTH_TAG_SIZE if flags & FLAG_AUTHENTICATED else 0
    return HEADER_SIZE + payload_length + tag + CRC_SIZE


def pack_frame(
    packet_type: int,
    sequence: int,
    timestamp_us: int,
    payload_bytes: bytes,
    flags: int = 0,
    link_key: Optional[bytes] = None,
) -> bytes:
    """Serialise a complete frame including the trailing CRC32.

    If ``link_key`` is given the frame is authenticated: ``FLAG_AUTHENTICATED``
    is set and the 8-byte tag is appended before the CRC.

    Raises :class:`PayloadTooLargeError` if the payload exceeds ``MAX_PAYLOAD``.
    """

    if len(payload_bytes) > MAX_PAYLOAD:
        raise PayloadTooLargeError(
            f"Payload length {len(payload_bytes)} exceeds maximum {MAX_PAYLOAD}"
        )
    if sequence < 0 or sequence > 0xFFFFFFFF:
        raise FrameError(f"Sequence number {sequence} out of uint32 range")
    if timestamp_us < 0 or timestamp_us > 0xFFFFFFFFFFFFFFFF:
        raise FrameError(f"Timestamp {timestamp_us} out of uint64 range")
    if int(packet_type) not in VALID_PACKET_TYPES:
        raise UnknownPacketTypeError(f"Unknown packet type 0x{int(packet_type):02X}")

    flags = flags & 0xFF
    if link_key is not None:
        if len(link_key) < MIN_LINK_KEY_BYTES:
            raise ValueError("link key too short")
        flags |= FLAG_AUTHENTICATED
    elif flags & FLAG_AUTHENTICATED:
        raise ValueError("FLAG_AUTHENTICATED requested without a link key")

    header = _HEADER_STRUCT.pack(
        MAGIC,
        VERSION,
        int(packet_type) & 0xFF,
        sequence & 0xFFFFFFFF,
        timestamp_us & 0xFFFFFFFFFFFFFFFF,
        len(payload_bytes) & 0xFFFF,
        flags,
    )
    body = header + payload_bytes
    if link_key is not None:
        body += compute_auth_tag(link_key, header + payload_bytes)
    crc = crc32(body)
    return body + struct.pack("<I", crc)


def unpack_frame(
    data: bytes,
    link_key: Optional[bytes] = None,
) -> Tuple[Dict[str, int], bytes]:
    """Parse a single complete frame.

    Returns ``(header_dict, payload_bytes)``. Raises :class:`FrameError` (or a
    subclass) on any structural or integrity problem. The CRC is validated and
    a mismatch raises :class:`CRCError` — frames are never silently accepted.

    Authentication result is reported in ``header["auth_state"]``:

    * ``NOT_CONFIGURED`` — frame carries no tag and no key is configured;
    * ``UNAUTHENTICATED`` — a key is configured but the frame carries no tag;
    * ``AUTHENTICATED`` — tag present and verified;
    * ``INVALID_TAG`` — tag present but wrong (also raised as
      :class:`AuthTagError` *after* the header is validated, so the caller can
      count it) — this function raises so an invalid tag can never be
      mistaken for valid data.
    * ``UNVERIFIABLE`` — tag present but no key configured on this receiver.
    """

    if len(data) < HEADER_SIZE + CRC_SIZE:
        raise FrameError(
            f"Frame too short: {len(data)} bytes, need at least "
            f"{HEADER_SIZE + CRC_SIZE}"
        )

    magic, version, ptype, seq, ts_us, length, flags = _HEADER_STRUCT.unpack(
        data[:HEADER_SIZE]
    )
    if magic != MAGIC:
        raise FrameError(f"Bad MAGIC bytes: {magic!r}")
    if version != VERSION:
        raise UnknownVersionError(f"Unsupported protocol version: 0x{version:02X}")
    if ptype not in VALID_PACKET_TYPES:
        raise UnknownPacketTypeError(f"Unknown packet type: 0x{ptype:02X}")
    if flags & ~KNOWN_FLAGS:
        raise FrameError(f"Unknown FLAGS bits set: 0x{flags:02X}")
    if length > MAX_PAYLOAD:
        raise PayloadTooLargeError(
            f"Declared payload length {length} exceeds maximum {MAX_PAYLOAD}"
        )

    authenticated_flag = bool(flags & FLAG_AUTHENTICATED)
    tag_size = AUTH_TAG_SIZE if authenticated_flag else 0
    expected_total = HEADER_SIZE + length + tag_size + CRC_SIZE
    if len(data) < expected_total:
        raise FrameError(
            f"Frame truncated: have {len(data)} bytes, need {expected_total}"
        )

    payload = data[HEADER_SIZE : HEADER_SIZE + length]
    tag_offset = HEADER_SIZE + length
    crc_offset = tag_offset + tag_size
    (stored_crc,) = struct.unpack("<I", data[crc_offset : crc_offset + CRC_SIZE])
    computed_crc = crc32(data[:crc_offset])
    if stored_crc != computed_crc:
        raise CRCError(
            f"CRC mismatch: stored=0x{stored_crc:08X} computed=0x{computed_crc:08X}"
        )

    header = {
        "version": version,
        "packet_type": ptype,
        "sequence": seq,
        "timestamp_us": ts_us,
        "payload_length": length,
        "flags": flags,
        "fec_present": bool(flags & FLAG_FEC_PRESENT),
        "compressed": bool(flags & FLAG_COMPRESSED),
        "encrypted": bool(flags & FLAG_ENCRYPTED),
        "authenticated_flag": authenticated_flag,
        "total_size": expected_total,
    }

    if authenticated_flag:
        stored_tag = bytes(data[tag_offset:crc_offset])
        header["auth_tag"] = stored_tag.hex()
        if link_key is None:
            header["auth_state"] = "UNVERIFIABLE"
        else:
            expected = compute_auth_tag(link_key, data[:tag_offset])
            if hmac.compare_digest(stored_tag, expected):
                header["auth_state"] = "AUTHENTICATED"
            else:
                header["auth_state"] = "INVALID_TAG"
                raise AuthTagError(f"Authentication tag mismatch for sequence {seq}")
    else:
        header["auth_state"] = "UNAUTHENTICATED" if link_key is not None else "NOT_CONFIGURED"
    return header, payload
