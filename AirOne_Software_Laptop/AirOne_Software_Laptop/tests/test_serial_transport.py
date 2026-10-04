"""Tests for the real telemetry link (LoRa E22 over USB serial).

Covers:
  * honest degradation: no port -> connect() returns False, never fabricates;
  * force_baud fixed-baud open path;
  * a full end-to-end live path over a PTY (fake CanSat radio) proving that
    bytes flow SerialTransport -> rx_queue -> StreamParser -> valid packet;
  * orchestrator wiring: NOT_CONFIGURED link status and health-report entry.

The PTY-based test is skipped on platforms without ``pty`` (e.g. Windows) or
when pyserial is unavailable.
"""
import json
import os
import queue
import time

import pytest

from src.communication.serial_transport import SerialTransport, _SERIAL_AVAILABLE
from src.telemetry import protocol
from src.telemetry.parser import StreamParser

try:
    import pty  # noqa: F401

    _PTY_AVAILABLE = True
except ImportError:  # pragma: no cover - non-POSIX
    _PTY_AVAILABLE = False

_needs_pty = pytest.mark.skipif(
    not (_PTY_AVAILABLE and _SERIAL_AVAILABLE),
    reason="requires pty + pyserial",
)


def test_connect_no_port_returns_false_without_fabricating():
    """No device -> explicit failure, no link, no data."""
    t = SerialTransport(queue.Queue())
    ok = t.connect(port="/dev/does-not-exist-airone", baud=115200, force_baud=True)
    assert ok is False
    assert t.health.connected is False
    assert t.health.bytes_received == 0
    assert t.health.packets_received == 0


def test_connect_remembers_params_for_reconnect():
    t = SerialTransport(queue.Queue())
    # Even on failure the requested params are remembered for the reconnect loop.
    t.connect(port="/dev/does-not-exist-airone", baud=57600, force_baud=True)
    assert t._baud == 57600
    assert t._force_baud is True


@_needs_pty
def test_live_serial_end_to_end_pty():
    """A frame written to a fake radio arrives, parses, and has a valid CRC."""
    import pty

    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)

    rxq: "queue.Queue" = queue.Queue()
    t = SerialTransport(rxq)
    assert t.connect(port=slave_name, baud=115200, force_baud=True) is True
    assert t.health.connected is True
    assert t.health.baud == 115200
    t.start_receiver()
    try:
        time.sleep(0.3)
        payload = json.dumps(
            {
                "bmp581_pressure": {
                    "value": 98123.4,
                    "unit": "Pa",
                    "sensor_id": "BMP581",
                }
            }
        ).encode("utf-8")
        frame = protocol.pack_frame(
            protocol.PacketType.SENSOR_DATA, 7, int(time.time() * 1e6), payload
        )
        os.write(master, frame)
        # The receiver read timeout is ~1 s; give it margin.
        time.sleep(1.5)

        got = b""
        while not rxq.empty():
            got += rxq.get_nowait()
        assert got, "no bytes received from the live serial link"

        packets = StreamParser().parse_stream(got)
        assert len(packets) == 1
        pkt = packets[0]
        assert pkt.crc_valid is True
        assert pkt.sequence == 7
        decoded = json.loads(pkt.payload.decode("utf-8"))
        assert decoded["bmp581_pressure"]["value"] == pytest.approx(98123.4)
    finally:
        t.disconnect()
        os.close(master)


def test_orchestrator_reports_not_configured_without_hardware(temp_db_path):
    """With no radio the orchestrator link status is explicit NOT_CONFIGURED."""
    import launcher
    from src.api.app import build_services
    from src.workers.orchestrator import Orchestrator

    cfg = launcher.load_config(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "default_config.yaml",
        )
    )
    # Force an explicit, nonexistent port so discovery cannot find a device.
    cfg.setdefault("telemetry", {})["serial_port"] = "/dev/does-not-exist-airone"
    services = build_services(temp_db_path, cfg)
    orch = Orchestrator(services, cfg)

    status = orch.start_telemetry_link()
    assert status.startswith("NOT_CONFIGURED")
    # No data fabricated.
    assert orch.transport.health.connected is False
    # Health report exposes the explicit link state.
    report = orch.health_report()
    assert "TelemetryLink" in report
    assert report["TelemetryLink"]["connected"] is False
    assert report["TelemetryLink"]["status"].startswith("NOT_CONFIGURED")


def test_orchestrator_link_disabled_via_config(temp_db_path):
    import launcher
    from src.api.app import build_services
    from src.workers.orchestrator import Orchestrator

    cfg = launcher.load_config(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "default_config.yaml",
        )
    )
    cfg.setdefault("telemetry", {})["enabled"] = False
    services = build_services(temp_db_path, cfg)
    orch = Orchestrator(services, cfg)
    status = orch.start_telemetry_link()
    assert status.startswith("DISABLED")
