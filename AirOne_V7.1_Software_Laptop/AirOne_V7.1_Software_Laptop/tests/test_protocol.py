"""Tests for the binary telemetry protocol, streaming parser, and FEC."""
import os

import pytest

from src.core.errors import CRCError, FrameError, PayloadTooLargeError
from src.telemetry import fec, protocol
from src.telemetry.parser import StreamParser


def test_pack_unpack_roundtrip():
    payload = b"the quick brown fox"
    frame = protocol.pack_frame(
        protocol.PacketType.SENSOR_DATA, sequence=12345,
        timestamp_us=1_700_000_000_000_000, payload_bytes=payload, flags=0x01,
    )
    header, out = protocol.unpack_frame(frame)
    assert out == payload
    assert header["sequence"] == 12345
    assert header["timestamp_us"] == 1_700_000_000_000_000
    assert header["packet_type"] == int(protocol.PacketType.SENSOR_DATA)
    assert header["payload_length"] == len(payload)
    assert header["fec_present"] is True
    assert header["version"] == protocol.VERSION


def test_crc_rejection():
    frame = bytearray(
        protocol.pack_frame(protocol.PacketType.GPS, 1, 1000, b"abcdef")
    )
    # Flip one payload byte -> CRC must fail.
    frame[protocol.HEADER_SIZE] ^= 0xFF
    with pytest.raises(CRCError):
        protocol.unpack_frame(bytes(frame))


def test_magic_search():
    good = protocol.pack_frame(protocol.PacketType.SENSOR_DATA, 7, 5, b"payload")
    stream = b"\x00\x11garbage\xff" + good
    parser = StreamParser()
    packets = parser.parse_stream(stream)
    assert len(packets) == 1
    assert packets[0].crc_valid
    assert packets[0].payload == b"payload"
    assert packets[0].sequence == 7


def test_duplicate_detection():
    f = protocol.pack_frame(protocol.PacketType.SENSOR_DATA, 99, 10, b"x")
    parser = StreamParser()
    first = parser.parse_stream(f)
    second = parser.parse_stream(f)
    assert first[0].duplicate is False
    assert second[0].duplicate is True


@pytest.mark.skipif(not fec.fec_available(), reason="reedsolo not installed")
def test_fec_repair():
    original = b"telemetry-payload-block"
    encoded = bytearray(fec.encode_payload(original))
    # Introduce a single-byte error.
    encoded[0] ^= 0xAA
    decoded, repaired = fec.decode_payload(bytes(encoded))
    assert decoded == original
    assert repaired is True


def test_oversized_payload():
    # Craft a header claiming a payload larger than MAX_PAYLOAD is impossible via
    # pack_frame (it raises), so verify pack rejects it.
    big = b"\x00" * (protocol.MAX_PAYLOAD + 1)
    with pytest.raises(PayloadTooLargeError):
        protocol.pack_frame(protocol.PacketType.SENSOR_DATA, 1, 1, big)


def test_truncated_frame_waits():
    f = protocol.pack_frame(protocol.PacketType.SENSOR_DATA, 3, 1, b"hello world")
    parser = StreamParser()
    # Feed only the first half: no packet yet.
    assert parser.parse_stream(f[: len(f) // 2]) == []
    # Feed the rest: packet emerges.
    packets = parser.parse_stream(f[len(f) // 2 :])
    assert len(packets) == 1
    assert packets[0].payload == b"hello world"
