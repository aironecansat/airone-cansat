"""Telemetry receiver worker: pulls raw bytes and emits parsed packets."""
from __future__ import annotations

import logging
import queue
from typing import Optional

from ..telemetry.parser import ParsedPacket, StreamParser
from .base_worker import BaseWorker

logger = logging.getLogger(__name__)


class TelemetryReceiverWorker(BaseWorker):
    def __init__(
        self,
        rx_queue: "queue.Queue",
        packet_queue: "queue.Queue",
        link_key: Optional[bytes] = None,
        require_auth: bool = False,
    ) -> None:
        """Create the receiver.

        ``link_key`` enables verification of the optional truncated
        HMAC-SHA256 frame tag (FLAGS bit 0x08). ``require_auth`` makes the
        parser reject every frame that does not carry a valid tag.
        """

        super().__init__("TelemetryReceiver")
        self.rx_queue = rx_queue
        self.packet_queue = packet_queue
        self.parser = StreamParser(link_key=link_key, require_auth=require_auth)
        self.health_status["auth_mode"] = self.parser.auth_mode

    def run(self) -> None:
        while not self.should_stop():
            try:
                data = self.rx_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.health_status["iterations"] += 1
            try:
                packets = self.parser.parse_stream(data)
                for pkt in packets:
                    self.packet_queue.put(pkt)
                self.health_status["crc_errors"] = self.parser.crc_errors
                self.health_status["frames"] = self.parser.frames_parsed
                # Explicit input-hardening counters (unknown VER/TYPE/FLAGS,
                # replays, authentication failures, rejected frames).
                self.health_status.update(self.parser.counters())
            except Exception:  # noqa: BLE001
                self.health_status["errors"] += 1
                logger.exception("Receiver failed to parse a chunk")
