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
from .sensors import (
    SensorSuite,
    simulate_battery_current,
    simulate_bme688_gas_resistance,
    simulate_ens160_aqi,
    simulate_ens160_eco2,
    simulate_ens160_tvoc,
    simulate_imu_accel,
    simulate_imu_gyro,
)

G0 = 9.80665  # standard gravity (m/s^2)
AVIONICS_POWER_W = 1.5  # nominal electrical load of the CanSat

# Numeric twin of the firmware ``mission_state`` string: index into the
# firmware's STATE_NAMES[] table (airone_cansat.ino), sent as mission_state_code.
MISSION_STATE_CODES: Dict[str, int] = {
    MissionState.BOOT.value: 0,
    MissionState.SELF_TEST.value: 1,
    MissionState.PRELAUNCH.value: 2,
    MissionState.ASCENT.value: 3,
    MissionState.APOGEE.value: 4,
    MissionState.DESCENT.value: 5,
    MissionState.LANDED.value: 6,
}


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

    def vertical_acceleration(self, t_s: float, dt: float = 0.05) -> float:
        """Ground-truth kinematic vertical acceleration (m/s^2, +up).

        Central second difference of :meth:`altitude`. Before launch and after
        landing the profile is flat, so this is 0. The idealised profile has
        velocity steps at launch and parachute deployment; the result is
        clamped to a physically plausible envelope (free fall .. ~3 g) so
        those kinks do not produce impossible spikes.
        """

        a = (self.altitude(t_s + dt) - 2.0 * self.altitude(t_s) + self.altitude(t_s - dt)) / (dt * dt)
        return max(-G0, min(3.0 * G0, a))

    def body_rates(self, t_s: float) -> tuple:
        """Ground-truth body angular rates (gx, gy, gz) in deg/s.

        * pre-launch / landed: at rest;
        * ascent: slow roll about the long axis (5 deg/s);
        * apogee: parachute-deployment tumble decaying over the hold;
        * descent: steady canopy spin (~30 deg/s) plus a ~0.5 Hz pendulum
          swing that damps as the canopy stabilises.
        """

        phase = self.phase(t_s)
        if phase in (MissionState.PRELAUNCH, MissionState.LANDED):
            return 0.0, 0.0, 0.0
        if phase == MissionState.ASCENT:
            return 0.0, 0.0, 5.0
        if phase == MissionState.APOGEE:
            ta = t_s - self.ascent_time_s
            decay = math.exp(-ta / 1.0)
            return (120.0 * decay * math.sin(2 * math.pi * 1.5 * ta),
                    90.0 * decay * math.cos(2 * math.pi * 1.5 * ta),
                    30.0 + 60.0 * decay)
        td = t_s - self.ascent_time_s - self.apogee_hold_s
        swing = 15.0 * math.exp(-td / 20.0) + 2.0
        w = 2 * math.pi * 0.5
        return swing * math.sin(w * td), swing * math.cos(w * td), 30.0


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
        battery_v = 4.2 - 0.0015 * t_s
        # Accelerometer measures specific force: kinematic accel + gravity on
        # the vertical (body z) axis; lateral axes are ~0 (noise from model).
        ax, ay, az = simulate_imu_accel(0.0, 0.0, self.profile.vertical_acceleration(t_s) + G0)
        gx, gy, gz = simulate_imu_gyro(*self.profile.body_rates(t_s))
        phase = self.profile.phase(t_s)
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
            "battery_voltage": battery_v,
            # ---- Firmware fields (noise-free truth; SensorModel adds noise)
            "bme688_gas_resistance": simulate_bme688_gas_resistance(alt),
            "ens160_tvoc": simulate_ens160_tvoc(alt),
            "ens160_eco2": simulate_ens160_eco2(alt),
            "ens160_aqi": float(simulate_ens160_aqi(alt)),
            "imu_accel_x": ax,
            "imu_accel_y": ay,
            "imu_accel_z": az,
            "imu_gyro_x": gx,
            "imu_gyro_y": gy,
            "imu_gyro_z": gz,
            "battery_current_ma": simulate_battery_current(AVIONICS_POWER_W, battery_v),
            "mission_state_code": float(MISSION_STATE_CODES[phase.value]),
        }

    def frame_at(self, t_s: float, sequence: int) -> TelemetryFrame:
        true = self._true_fields(t_s)
        ts = self._t0 + timedelta(seconds=t_s)
        frame = TelemetryFrame(
            sequence=sequence,
            timestamp=ts,
            mission_phase=self.profile.phase(t_s).value,
        )
        # Firmware ``mission_state`` is an enum string, which cannot be a
        # numeric Measurement; it rides on the frame (mission_phase + metadata)
        # and its numeric twin ``mission_state_code`` is a measurement.
        frame.metadata["mission_state"] = frame.mission_phase
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
