"""Turn simulated telemetry frames into on-the-wire binary packets.

The simulator emits :class:`TelemetryFrame` objects. To exercise the *entire*
real pipeline (parser -> CRC -> pipeline stages), those frames are serialised
into the same JSON payload convention the packet processor expects and packed
with :func:`protocol.pack_frame`. Injecting these bytes into the receiver queue
means simulated data travels the identical code path as real radio data.
"""
from __future__ import annotations

import json
from typing import Optional, Iterator, List

from ..telemetry import protocol
from .flight import MissionSimulator


def frame_to_payload(frame) -> bytes:
    """Encode a TelemetryFrame's measurements as the JSON payload convention."""

    obj = {}
    for key, m in frame.measurements.items():
        # Represent a NaN (invalid) value as null so JSON stays valid; the
        # processor will mark it INVALID on decode.
        value = None if (m.value != m.value) else m.value
        obj[key] = {
            "value": value,
            "unit": m.unit,
            "sensor_id": m.sensor_id,
            "quality": m.quality.value,
            "source": m.source.value,
        }
    return json.dumps(obj).encode("utf-8")


class SimulatedPacketSource:
    """Produces packed binary frames from a :class:`MissionSimulator`."""

    def __init__(self, simulator: MissionSimulator, link_key: Optional[bytes] = None) -> None:
        """``link_key`` (optional) makes the source emit authenticated frames
        (FLAGS 0x08 + truncated HMAC-SHA256 tag), exactly like firmware built
        with ``AIRONE_LINK_KEY_HEX``. Payloads remain tagged ``SIMULATED``."""

        self.simulator = simulator
        self.link_key = link_key

    def packets(self) -> Iterator[bytes]:
        for frame in self.simulator.frames():
            ts_us = int(frame.timestamp.timestamp() * 1_000_000)
            payload = frame_to_payload(frame)
            yield protocol.pack_frame(
                packet_type=int(protocol.PacketType.SENSOR_DATA),
                sequence=frame.sequence,
                timestamp_us=ts_us,
                payload_bytes=payload,
                flags=protocol.FLAG_AUTHENTICATED if self.link_key else 0,
                link_key=self.link_key,
            )

    def collect(self) -> List[bytes]:
        return list(self.packets())
