"""Serial transport with auto-discovery, baud negotiation, and reconnect.

The receiver runs on its own thread with a dedicated stop event. Decoded raw
bytes are pushed onto ``rx_queue`` for the packet processor. Works without
hardware: if no port is found, ``connect`` returns ``False`` and the caller can
fall back to simulation.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from ..telemetry import protocol

logger = logging.getLogger(__name__)

try:
    import serial  # type: ignore
    import serial.tools.list_ports as list_ports  # type: ignore

    _SERIAL_AVAILABLE = True
except Exception:  # noqa: BLE001
    serial = None  # type: ignore
    list_ports = None  # type: ignore
    _SERIAL_AVAILABLE = False
    logger.warning("pyserial unavailable: serial transport disabled")

# Known USB-serial bridges used by ESP32 / CanSat boards.
KNOWN_VID_PIDS = {
    (0x10C4, None): "CP210x (ESP32)",
    (0x1A86, None): "CH340 (ESP32)",
    (0x0403, None): "FTDI",
}
BAUD_CANDIDATES = [115200, 57600, 9600]
MAX_BACKOFF_S = 30.0


@dataclass
class TransportHealth:
    connected: bool = False
    port: Optional[str] = None
    baud: Optional[int] = None
    bytes_received: int = 0
    packets_received: int = 0
    crc_errors: int = 0
    reconnect_count: int = 0
    last_rssi: Optional[int] = None


class SerialTransport:
    def __init__(self, rx_queue: Optional["queue.Queue"] = None) -> None:
        self.rx_queue: "queue.Queue" = rx_queue or queue.Queue(maxsize=10000)
        self.health = TransportHealth()
        self._serial = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Remembered connection parameters so reconnect keeps the same link.
        self._baud: Optional[int] = None
        self._force_baud: bool = False

    # -- discovery -------------------------------------------------------
    @staticmethod
    def discover_ports() -> List[str]:
        if not _SERIAL_AVAILABLE:
            return []
        ports = []
        for p in list_ports.comports():
            vid = getattr(p, "vid", None)
            for (kvid, kpid), _name in KNOWN_VID_PIDS.items():
                if vid == kvid and (kpid is None or getattr(p, "pid", None) == kpid):
                    ports.insert(0, p.device)  # prioritise known devices
                    break
            else:
                ports.append(p.device)
        return ports

    def _open_fixed_baud(self, port: str, baud: int) -> Optional[int]:
        """Open ``port`` directly at ``baud`` without MAGIC negotiation.

        Used for E22 modules configured in transparent mode at a known fixed
        baud rate, where waiting for a MAGIC preamble would needlessly delay the
        link (the CanSat may not be transmitting yet at connect time).
        """

        try:
            self._serial = serial.Serial(port, baud, timeout=1.0)
            return baud
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to open %s @ %d (fixed baud): %s", port, baud, exc)
            return None

    def _negotiate_baud(self, port: str, preferred: Optional[int] = None) -> Optional[int]:
        """Open the port at each candidate baud and look for a MAGIC preamble.

        If ``preferred`` is given it is tried first so a configured baud rate
        wins before the fallback candidates.
        """

        candidates = list(BAUD_CANDIDATES)
        if preferred is not None:
            candidates = [preferred] + [b for b in candidates if b != preferred]
        for baud in candidates:
            try:
                ser = serial.Serial(port, baud, timeout=1.0)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Cannot open %s @ %d: %s", port, baud, exc)
                continue
            try:
                time.sleep(0.2)
                data = ser.read(512)
                if protocol.MAGIC in data:
                    self._serial = ser
                    return baud
            finally:
                if self._serial is not ser:
                    ser.close()
        # No MAGIC seen: default to the preferred/first candidate so the link
        # still opens (the CanSat may simply not be transmitting yet).
        fallback = preferred if preferred is not None else BAUD_CANDIDATES[0]
        try:
            self._serial = serial.Serial(port, fallback, timeout=1.0)
            return fallback
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to open %s: %s", port, exc)
            return None

    # -- connection ------------------------------------------------------
    def connect(
        self,
        port: Optional[str] = None,
        baud: Optional[int] = None,
        force_baud: bool = False,
    ) -> bool:
        """Open a serial link to the CanSat radio bridge.

        ``port``       explicit device path, or ``None`` to auto-discover.
        ``baud``       preferred baud rate (tried first during negotiation).
        ``force_baud`` open directly at ``baud`` with no MAGIC negotiation
                       (recommended for E22 transparent mode at a fixed baud).

        Returns ``True`` on success. Returns ``False`` (never fabricating a
        link) when pyserial is missing or no device is found — the caller must
        surface an explicit NOT_CONFIGURED state rather than substitute data.
        """

        if not _SERIAL_AVAILABLE:
            logger.error("Cannot connect: pyserial not installed")
            return False
        # Remember parameters so the receive loop can reconnect identically.
        self._baud = baud
        self._force_baud = force_baud
        candidates = [port] if port else self.discover_ports()
        if not candidates:
            logger.warning("No serial ports discovered")
            return False
        for cand in candidates:
            if force_baud and baud is not None:
                opened = self._open_fixed_baud(cand, baud)
            else:
                opened = self._negotiate_baud(cand, preferred=baud)
            if opened is not None:
                self.health.connected = True
                self.health.port = cand
                self.health.baud = opened
                logger.info("Serial connected on %s @ %d baud", cand, opened)
                return True
        return False

    def disconnect(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001
                pass
            self._serial = None
        self.health.connected = False

    def send(self, data: bytes) -> int:
        if self._serial is None or not self.health.connected:
            raise RuntimeError("Serial not connected")
        return int(self._serial.write(data))

    # -- receiver thread -------------------------------------------------
    def start_receiver(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._receive_loop, name="SerialReceiver", daemon=True
        )
        self._thread.start()

    def _receive_loop(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            if self._serial is None or not self.health.connected:
                if not self.connect(
                    self.health.port, baud=self._baud, force_baud=self._force_baud
                ):
                    logger.info("Reconnect failed; backoff %.1fs", backoff)
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                    self.health.reconnect_count += 1
                    continue
                backoff = 1.0
            try:
                data = self._serial.read(1024)
                if data:
                    self.health.bytes_received += len(data)
                    try:
                        self.rx_queue.put_nowait(data)
                    except queue.Full:
                        logger.warning("rx_queue full; dropping %d bytes", len(data))
            except Exception as exc:  # noqa: BLE001
                logger.error("Serial read error: %s", exc)
                self.health.connected = False
                self._serial = None
