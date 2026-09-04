"""Streaming binary telemetry parser.

Handles a continuous byte stream that may contain garbage between frames,
partial frames split across reads, duplicate sequence numbers, and
out-of-order delivery. CRC failures are reported explicitly and never
silently accepted.

Input hardening (Team AirOne security audit):

* LEN is bounds-checked *before* any allocation; unknown VER / TYPE / FLAGS
  are rejected and **counted** (``invalid_version``, ``invalid_type``,
  ``invalid_flags``) instead of being silently resynced past.
* Optional frame authentication (``link_key``): each frame carries an
  explicit ``auth_state`` — ``AUTHENTICATED``, ``UNAUTHENTICATED`` (key set,
  no tag), ``UNVERIFIABLE`` (tag, no key), ``NOT_CONFIGURED``. A frame with an
  *invalid* tag is emitted as an error packet (``crc_valid=False``,
  ``auth_state="INVALID_TAG"``) so downstream marks it CORRUPTED and it can
  never be mistaken for data. With ``require_auth=True`` every frame that is
  not ``AUTHENTICATED`` is flagged ``rejected=True``.
* Replay/regression detection: a sequence number that is *not* a duplicate
  but is far behind the highest seen (beyond ``order_tolerance``) is reported
  as ``replay=True`` (counter ``replays``) — the packet is still emitted so
  the operator sees the anomaly, but is marked and never treated as fresh.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

from ..core.errors import CRCError, FrameError, PayloadTooLargeError
from . import protocol

logger = logging.getLogger(__name__)


@dataclass
class ParsedPacket:
    """A packet emitted by the streaming parser."""

    header: Dict[str, int]
    payload: bytes
    crc_valid: bool
    duplicate: bool = False
    out_of_order: bool = False
    replay: bool = False
    auth_state: str = "NOT_CONFIGURED"
    rejected: bool = False  # True when require_auth is on and frame is not AUTHENTICATED
    error: str = ""

    @property
    def sequence(self) -> int:
        return int(self.header.get("sequence", 0))

    @property
    def authenticated(self) -> bool:
        return self.auth_state == "AUTHENTICATED"


class StreamParser:
    """Incrementally parse frames out of an arbitrary byte stream."""

    def __init__(
        self,
        dedup_window: int = 256,
        order_tolerance: int = 8,
        max_buffer: int = 1 << 20,
        link_key: Optional[bytes] = None,
        require_auth: bool = False,
    ) -> None:
        self._buffer = bytearray()
        self._seen_sequences: Deque[int] = deque(maxlen=dedup_window)
        self._seen_set: set = set()
        self._last_sequence: Optional[int] = None
        self._order_tolerance = order_tolerance
        self._max_buffer = max_buffer
        if link_key is not None and len(link_key) < protocol.MIN_LINK_KEY_BYTES:
            raise ValueError("link key too short")
        if require_auth and link_key is None:
            raise ValueError("require_auth=True needs a link key")
        self._link_key = link_key
        self._require_auth = bool(require_auth)
        # Diagnostics counters.
        self.crc_errors = 0
        self.frames_parsed = 0
        self.duplicates = 0
        self.out_of_order = 0
        self.replays = 0
        self.invalid_version = 0
        self.invalid_type = 0
        self.invalid_flags = 0
        self.invalid_length = 0
        self.auth_failures = 0
        self.unauthenticated = 0
        self.rejected = 0

    # ------------------------------------------------------------------
    @property
    def auth_mode(self) -> str:
        if self._link_key is None:
            return "NOT_CONFIGURED"
        return "REQUIRED" if self._require_auth else "OPTIONAL"

    def counters(self) -> Dict[str, int]:
        return {
            "frames_parsed": self.frames_parsed,
            "crc_errors": self.crc_errors,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "replays": self.replays,
            "invalid_version": self.invalid_version,
            "invalid_type": self.invalid_type,
            "invalid_flags": self.invalid_flags,
            "invalid_length": self.invalid_length,
            "auth_failures": self.auth_failures,
            "unauthenticated": self.unauthenticated,
            "rejected": self.rejected,
        }

    def _record_sequence(self, seq: int) -> None:
        if len(self._seen_sequences) == self._seen_sequences.maxlen:
            oldest = self._seen_sequences[0]
            # Will be evicted by the append below; drop from set too.
            self._seen_set.discard(oldest)
        self._seen_sequences.append(seq)
        self._seen_set.add(seq)

    def _is_duplicate(self, seq: int) -> bool:
        return seq in self._seen_set

    def _error_packet(self, frame_bytes: bytes, error: str, auth_state: str) -> ParsedPacket:
        try:
            header = {
                "sequence": int.from_bytes(frame_bytes[6:10], "little"),
                "payload_length": int.from_bytes(frame_bytes[18:20], "little"),
                "packet_type": frame_bytes[5],
                "flags": frame_bytes[20],
            }
        except Exception:  # noqa: BLE001
            header = {"sequence": 0, "payload_length": 0}
        return ParsedPacket(
            header=header, payload=b"", crc_valid=False, error=error,
            auth_state=auth_state, rejected=True,
        )

    def parse_stream(self, data: bytes) -> List[ParsedPacket]:
        """Feed more bytes and return any complete packets found."""

        if data:
            self._buffer.extend(data)
        if len(self._buffer) > self._max_buffer:
            # Prevent unbounded growth if we never see a valid MAGIC.
            logger.warning(
                "Parser buffer exceeded %d bytes; discarding oldest half",
                self._max_buffer,
            )
            del self._buffer[: len(self._buffer) // 2]

        packets: List[ParsedPacket] = []

        while True:
            idx = self._buffer.find(protocol.MAGIC)
            if idx == -1:
                # Keep only a trailing window that might contain a partial MAGIC.
                if len(self._buffer) > len(protocol.MAGIC):
                    del self._buffer[: -len(protocol.MAGIC)]
                break

            if idx > 0:
                # Discard garbage preceding the MAGIC.
                del self._buffer[:idx]

            if len(self._buffer) < protocol.HEADER_SIZE + protocol.CRC_SIZE:
                break  # Wait for more bytes.

            # --- header pre-checks BEFORE any allocation -------------------
            version = self._buffer[4]
            ptype = self._buffer[5]
            flags = self._buffer[20]
            length = int.from_bytes(self._buffer[18:20], "little")
            if version != protocol.VERSION:
                self.invalid_version += 1
                logger.warning("Frame with unknown VERSION 0x%02X rejected (INVALID)", version)
                del self._buffer[: len(protocol.MAGIC)]
                continue
            if ptype not in protocol.VALID_PACKET_TYPES:
                self.invalid_type += 1
                logger.warning("Frame with unknown PACKET_TYPE 0x%02X rejected (INVALID)", ptype)
                del self._buffer[: len(protocol.MAGIC)]
                continue
            if flags & ~protocol.KNOWN_FLAGS:
                self.invalid_flags += 1
                logger.warning("Frame with unknown FLAGS 0x%02X rejected (INVALID)", flags)
                del self._buffer[: len(protocol.MAGIC)]
                continue
            if length > protocol.MAX_PAYLOAD:
                self.invalid_length += 1
                logger.debug("Invalid payload length %d; skipping MAGIC", length)
                del self._buffer[: len(protocol.MAGIC)]
                continue

            total = protocol.frame_total_size(length, flags)
            if len(self._buffer) < total:
                break  # Incomplete frame; wait for more data.

            frame_bytes = bytes(self._buffer[:total])
            try:
                header, payload = protocol.unpack_frame(frame_bytes, link_key=self._link_key)
            except CRCError as exc:
                self.crc_errors += 1
                logger.warning("CRC validation failed: %s", exc)
                # Emit an explicit error packet so upstream marks CORRUPTED.
                packets.append(self._error_packet(frame_bytes, str(exc), "UNKNOWN"))
                del self._buffer[:total]
                continue
            except protocol.AuthTagError as exc:
                self.auth_failures += 1
                self.rejected += 1
                logger.warning("Frame authentication FAILED: %s", exc)
                packets.append(self._error_packet(frame_bytes, str(exc), "INVALID_TAG"))
                del self._buffer[:total]
                continue
            except (PayloadTooLargeError, FrameError) as exc:
                logger.debug("Frame error, resyncing: %s", exc)
                del self._buffer[: len(protocol.MAGIC)]
                continue

            del self._buffer[:total]
            self.frames_parsed += 1

            auth_state = str(header.get("auth_state", "NOT_CONFIGURED"))
            rejected = False
            if auth_state == "UNAUTHENTICATED":
                self.unauthenticated += 1
            if self._require_auth and auth_state != "AUTHENTICATED":
                rejected = True
                self.rejected += 1
                logger.warning(
                    "Frame seq=%d rejected: authentication REQUIRED but state is %s",
                    header["sequence"], auth_state,
                )

            seq = header["sequence"]
            duplicate = self._is_duplicate(seq)
            out_of_order = False
            replay = False
            if duplicate:
                self.duplicates += 1
                logger.debug("Duplicate sequence %d discarded", seq)
            else:
                if (
                    self._last_sequence is not None
                    and seq < self._last_sequence - self._order_tolerance
                ):
                    # Far behind the newest sequence and not in the dedup
                    # window: either a very late frame or a replayed capture.
                    out_of_order = True
                    replay = True
                    self.out_of_order += 1
                    self.replays += 1
                    logger.warning(
                        "Sequence regression %d (last=%d): flagged as REPLAY", seq, self._last_sequence
                    )
                self._record_sequence(seq)
                if self._last_sequence is None or seq > self._last_sequence:
                    self._last_sequence = seq

            packets.append(
                ParsedPacket(
                    header=header,
                    payload=payload,
                    crc_valid=True,
                    duplicate=duplicate,
                    out_of_order=out_of_order,
                    replay=replay,
                    auth_state=auth_state,
                    rejected=rejected,
                )
            )

        return packets

    def reset(self) -> None:
        self._buffer.clear()
        self._seen_sequences.clear()
        self._seen_set.clear()
        self._last_sequence = None
