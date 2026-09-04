"""Flight profile and mission simulator.

:class:`FlightProfile` produces the ground-truth altitude/velocity versus time
for a canonical CanSat flight: powered/lofted ascent to apogee, brief apogee,
then parachute descent toward a terminal velocity. :class:`MissionSimulator`
samples the profile plus the phenomena/atmosphere models, applies sensor error,
and emits fully-provenanced :class:`TelemetryFrame` objects.

Every emitted measurement carries ``source=SIMULATED`` and
``quality=QualityState.SIMULATED``. A sensor dropout yields an explicitly
INVALID measurement (never a silent zero).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, Iterator, List, Optional

from ..core.models import (
    DataSource,
    Measurement,
    MissionState,
    QualityState,
    TelemetryFrame,
    utcnow,
)
from . import atmosphere
from .phenomena import GasPlume, GroundTrack, true_radiation_cpm
from .sensors import SensorSuite


@dataclass
class FlightProfile:
    apogee_m: float = 1000.0
    ascent_time_s: float = 60.0
    apogee_hold_s: float = 3.0
    terminal_velocity_ms: float = 8.0
    ground_alt_m: float = 0.0

    @property
    def descent_time_s(self) -> float:
        return max(1.0, (self.apogee_m - self.ground_alt_m) / self.terminal_velocity_ms)

    @property
    def total_time_s(self) -> float:
        return self.ascent_time_s + self.apogee_hold_s + self.descent_time_s

    def altitude(self, t_s: float) -> float:
        """Ground-truth altitude (m) at mission time ``t_s``."""

        if t_s <= 0:
            return self.ground_alt_m
        if t_s < self.ascent_time_s:
            # Smooth decelerating ascent (sine ease-out) to apogee.
            frac = t_s / self.ascent_time_s
            return self.ground_alt_m + (self.apogee_m - self.ground_alt_m) * math.sin(frac * math.pi / 2)
        if t_s < self.ascent_time_s + self.apogee_hold_s:
            return self.apogee_m
        # Descent at (near) terminal velocity.
        td = t_s - self.ascent_time_s - self.apogee_hold_s
        alt = self.apogee_m - self.terminal_velocity_ms * td
        return max(self.ground_alt_m, alt)

    def phase(self, t_s: float) -> MissionState:
        if t_s <= 0:
            return MissionState.PRELAUNCH
        if t_s < self.ascent_time_s:
            return MissionState.ASCENT
        if t_s < self.ascent_time_s + self.apogee_hold_s:
            return MissionState.APOGEE
        if self.altitude(t_s) > self.ground_alt_m + 0.5:
            return MissionState.DESCENT
        return MissionState.LANDED


@dataclass
class MissionSimulator:
    profile: FlightProfile = field(default_factory=FlightProfile)
    sensors: SensorSuite = field(default_factory=SensorSuite.default)
    ground_track: GroundTrack = field(default_factory=GroundTrack)
    plume: GasPlume = field(default_factory=GasPlume)
    sample_rate_hz: float = 2.0
    seed: int = 12345
    mission_id: str = "sim"

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._t0 = utcnow()

    def _true_fields(self, t_s: float) -> Dict[str, float]:
        alt = self.profile.altitude(t_s)
        atmo = atmosphere.state(alt)
        lat, lon = self.ground_track.position(t_s)
        # UV counts rise with altitude (less atmospheric attenuation).
        uv_scale = 1.0 + alt / 2000.0
        return {
            "bme688_pressure": atmo.pressure_Pa,
            "bmp581_pressure": atmo.pressure_Pa,
            "bme688_temperature": atmo.temperature_K,
            "bmp581_temperature": atmo.temperature_K,
            "bme688_humidity": max(0.0, 60.0 - alt / 200.0),
            "sgp41_voc": self.plume.voc_index(alt),
            "sgp41_nox": self.plume.nox_index(alt),
            "veml6075_uva": 800.0 * uv_scale,
            "veml6075_uvb": 500.0 * uv_scale,
            "opt3001_lux": 40000.0 * uv_scale,
            "mag_x": 20.0,
            "mag_y": 5.0,
            "mag_z": 42.0,
            "radiation_cpm": true_radiation_cpm(alt),
            "gnss_lat": lat,
            "gnss_lon": lon,
            "gnss_altitude": alt,
            "battery_voltage": 4.2 - 0.0015 * t_s,
        }

    def frame_at(self, t_s: float, sequence: int) -> TelemetryFrame:
        true = self._true_fields(t_s)
        ts = self._t0 + timedelta(seconds=t_s)
        frame = TelemetryFrame(
            sequence=sequence,
            timestamp=ts,
            mission_phase=self.profile.phase(t_s).value,
        )
        for field_name, true_val in true.items():
            model = self.sensors.get(field_name)
            if model is None:
                continue
            reading = model.sample(true_val, t_s, self._rng)
            if reading is None:
                frame.add(Measurement.invalid(
                    sensor_id=model.sensor_id, unit=model.unit,
                    field_name=field_name, quality=QualityState.MISSING,
                    timestamp=ts,
                ))
                continue
            frame.add(Measurement(
                value=float(reading),
                unit=model.unit,
                timestamp=ts,
                sensor_id=model.sensor_id,
                quality=QualityState.SIMULATED,
                valid=True,
                uncertainty=model.noise_std if model.noise_std > 0 else 0.0,
                calibration_version="sim",
                source=DataSource.SIMULATED,
                field_name=field_name,
            ))
        return frame

    def frames(self) -> Iterator[TelemetryFrame]:
        """Yield frames across the whole flight at the configured rate."""

        dt = 1.0 / self.sample_rate_hz
        n = int(self.profile.total_time_s / dt) + 1
        for i in range(n):
            yield self.frame_at(i * dt, sequence=i)

    def collect(self) -> List[TelemetryFrame]:
        return list(self.frames())
