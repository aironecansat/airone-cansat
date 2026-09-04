"""Simulation-engine tests: ISA correctness, reproducibility, provenance."""
from __future__ import annotations

import pytest

from src.core.models import DataSource, QualityState
from src.simulation import (
    FlightProfile,
    MissionSimulator,
    SimulatedPacketSource,
    atmosphere,
)
from src.telemetry import protocol


def test_isa_sea_level():
    assert atmosphere.temperature(0.0) == pytest.approx(288.15, abs=1e-6)
    assert atmosphere.pressure(0.0) == pytest.approx(101325.0, abs=1e-6)
    assert atmosphere.density(0.0) == pytest.approx(1.225, abs=2e-3)


def test_isa_tropopause():
    assert atmosphere.temperature(11000.0) == pytest.approx(216.65, abs=1e-2)
    # Reference tropopause pressure ~ 22632 Pa.
    assert atmosphere.pressure(11000.0) == pytest.approx(22632.0, rel=1e-3)


def test_flight_profile_shape():
    prof = FlightProfile(apogee_m=1000.0, ascent_time_s=60.0, terminal_velocity_ms=8.0)
    assert prof.altitude(0.0) == pytest.approx(0.0, abs=1e-6)
    assert prof.altitude(60.0) == pytest.approx(1000.0, abs=1.0)
    # After apogee it descends.
    assert prof.altitude(prof.total_time_s) == pytest.approx(0.0, abs=1.0)


def test_simulation_reproducible_from_seed():
    a = MissionSimulator(profile=FlightProfile(apogee_m=800.0), sample_rate_hz=2.0, seed=7).collect()
    b = MissionSimulator(profile=FlightProfile(apogee_m=800.0), sample_rate_hz=2.0, seed=7).collect()
    assert len(a) == len(b)
    for fa, fb in zip(a, b):
        assert fa.get("bme688_pressure").value == fb.get("bme688_pressure").value


def test_simulated_measurements_carry_provenance():
    frames = MissionSimulator(sample_rate_hz=2.0, seed=1).collect()
    saw_valid = False
    for fr in frames:
        for m in fr.measurements.values():
            if m.valid:
                saw_valid = True
                assert m.source == DataSource.SIMULATED
                assert m.quality == QualityState.SIMULATED
    assert saw_valid


def test_simulated_packets_parse_and_crc_ok():
    sim = MissionSimulator(sample_rate_hz=2.0, seed=3)
    packets = SimulatedPacketSource(sim).collect()
    assert packets
    for pkt in packets[:20]:
        header, payload = protocol.unpack_frame(pkt)
        assert header["magic_ok"] if "magic_ok" in header else True
        # CRC is validated inside unpack_frame; a bad CRC would raise.
        assert len(payload) > 0
        assert header["sequence"] >= 0


def test_dropout_produces_invalid_not_zero():
    """A sensor with 100% dropout must yield INVALID measurements, never 0."""

    from src.simulation.sensors import SensorModel, SensorSuite
    from src.simulation.flight import MissionSimulator as MS

    suite = SensorSuite.default()
    suite.models["bme688_pressure"].dropout_prob = 1.0
    sim = MS(sensors=suite, sample_rate_hz=1.0, seed=5)
    frames = sim.collect()
    ms = [fr.get("bme688_pressure") for fr in frames if fr.get("bme688_pressure")]
    assert ms
    for m in ms:
        assert not m.valid
        assert m.quality == QualityState.MISSING
