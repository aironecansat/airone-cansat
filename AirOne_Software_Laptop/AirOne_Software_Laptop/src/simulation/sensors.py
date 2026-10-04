"""Sensor emulation with realistic, reproducible error characteristics.

Each :class:`SensorModel` applies, in order: a fixed bias, a slow linear drift,
Gaussian noise, optional saturation clamping, and a random dropout probability.
All randomness is drawn from a caller-supplied :class:`random.Random` so an
entire mission is reproducible from a single seed.

A dropout yields ``None`` — the caller is responsible for turning that into an
explicitly INVALID / MISSING measurement. The simulator never emits a silent 0.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional, Tuple

from .phenomena import boundary_layer_gas_profile


# Noise budgets for the firmware fields (shared by SensorSuite and simulate_*).
GAS_RESISTANCE_NOISE_OHM = 3000.0
ENS160_TVOC_NOISE_PPB = 10.0
ENS160_ECO2_NOISE_PPM = 5.0
IMU_ACCEL_NOISE_MS2 = 0.05
IMU_GYRO_NOISE_DPS = 0.3
BATTERY_CURRENT_NOISE_MA = 5.0
RADIATION_CPM_NOISE = 2.0


@dataclass
class SensorModel:
    sensor_id: str
    unit: str
    bias: float = 0.0
    drift_per_s: float = 0.0
    noise_std: float = 0.0
    saturation: Optional[Tuple[float, float]] = None
    dropout_prob: float = 0.0
    quantization: Optional[float] = None

    def sample(self, true_value: float, t_s: float, rng: random.Random) -> Optional[float]:
        """Return a noisy reading of ``true_value`` at mission time ``t_s``.

        Returns ``None`` on a simulated dropout.
        """

        if self.dropout_prob > 0.0 and rng.random() < self.dropout_prob:
            return None
        value = true_value + self.bias + self.drift_per_s * t_s
        if self.noise_std > 0.0:
            value += rng.gauss(0.0, self.noise_std)
        if self.quantization:
            value = round(value / self.quantization) * self.quantization
        if self.saturation is not None:
            lo, hi = self.saturation
            value = max(lo, min(hi, value))
        return value


@dataclass
class SensorSuite:
    """The full AirOne sensor complement with plausible error budgets."""

    models: dict = field(default_factory=dict)

    @classmethod
    def default(cls) -> "SensorSuite":
        m = {
            # Barometric pressure sensors (Pa). BMP581 is the more precise one.
            "bme688_pressure": SensorModel("BME688", "Pa", bias=15.0, drift_per_s=0.02, noise_std=8.0, saturation=(1000.0, 130000.0)),
            "bmp581_pressure": SensorModel("BMP581", "Pa", bias=-5.0, drift_per_s=0.01, noise_std=3.0, saturation=(1000.0, 130000.0)),
            # Temperature (K).
            "bme688_temperature": SensorModel("BME688", "K", bias=0.3, drift_per_s=0.001, noise_std=0.05, saturation=(200.0, 350.0)),
            "bmp581_temperature": SensorModel("BMP581", "K", bias=-0.15, drift_per_s=0.0005, noise_std=0.03, saturation=(200.0, 350.0)),
            # Humidity (%RH).
            "bme688_humidity": SensorModel("BME688", "%", bias=1.5, noise_std=0.8, saturation=(0.0, 100.0)),
            # Gas indices (unitless).
            "sgp41_voc": SensorModel("SGP41", "index", bias=3.0, noise_std=4.0, saturation=(0.0, 500.0)),
            "sgp41_nox": SensorModel("SGP41", "index", noise_std=0.3, saturation=(0.0, 500.0)),
            # UV (raw counts) and lux.
            "veml6075_uva": SensorModel("VEML6075", "counts", noise_std=20.0, saturation=(0.0, 65535.0)),
            "veml6075_uvb": SensorModel("VEML6075", "counts", noise_std=15.0, saturation=(0.0, 65535.0)),
            "opt3001_lux": SensorModel("OPT3001", "lux", noise_std=50.0, saturation=(0.0, 83000.0)),
            # Magnetometer (uT).
            "mag_x": SensorModel("MMC5603", "uT", bias=1.2, noise_std=0.4),
            "mag_y": SensorModel("MMC5603", "uT", bias=-0.8, noise_std=0.4),
            "mag_z": SensorModel("MMC5603", "uT", bias=0.5, noise_std=0.4),
            # Radiation counts per minute.
            "radiation_cpm": SensorModel("SEN0463", "CPM", noise_std=0.0, saturation=(0.0, 100000.0), quantization=1.0),
            # GNSS.
            "gnss_lat": SensorModel("MAX-M10S", "deg", noise_std=2.0e-5),
            "gnss_lon": SensorModel("MAX-M10S", "deg", noise_std=2.0e-5),
            "gnss_altitude": SensorModel("MAX-M10S", "m", noise_std=3.0, saturation=(-500.0, 120000.0)),
            # Power.
            "battery_voltage": SensorModel("INA219", "V", noise_std=0.01, saturation=(0.0, 5.0)),
            # ---- Firmware fields -------------------------------------
            # BME688 MOX gas-heater resistance (Ohm).
            "bme688_gas_resistance": SensorModel("BME688", "Ohm", noise_std=GAS_RESISTANCE_NOISE_OHM, saturation=(1000.0, 1_000_000.0)),
            # ENS160 digital metal-oxide air-quality sensor.
            "ens160_tvoc": SensorModel("ENS160", "ppb", noise_std=ENS160_TVOC_NOISE_PPB, saturation=(0.0, 65000.0), quantization=1.0),
            "ens160_eco2": SensorModel("ENS160", "ppm", noise_std=ENS160_ECO2_NOISE_PPM, saturation=(400.0, 65000.0), quantization=1.0),
            "ens160_aqi": SensorModel("ENS160", "index", saturation=(1.0, 5.0), quantization=1.0),
            # BMI270 IMU: specific force (m/s^2) and angular rate (deg/s).
            "imu_accel_x": SensorModel("BMI270", "m/s^2", noise_std=IMU_ACCEL_NOISE_MS2, saturation=(-156.9, 156.9)),
            "imu_accel_y": SensorModel("BMI270", "m/s^2", noise_std=IMU_ACCEL_NOISE_MS2, saturation=(-156.9, 156.9)),
            "imu_accel_z": SensorModel("BMI270", "m/s^2", noise_std=IMU_ACCEL_NOISE_MS2, saturation=(-156.9, 156.9)),
            "imu_gyro_x": SensorModel("BMI270", "deg/s", noise_std=IMU_GYRO_NOISE_DPS, saturation=(-2000.0, 2000.0)),
            "imu_gyro_y": SensorModel("BMI270", "deg/s", noise_std=IMU_GYRO_NOISE_DPS, saturation=(-2000.0, 2000.0)),
            "imu_gyro_z": SensorModel("BMI270", "deg/s", noise_std=IMU_GYRO_NOISE_DPS, saturation=(-2000.0, 2000.0)),
            # INA219 load current (mA).
            "battery_current_ma": SensorModel("INA219", "mA", noise_std=BATTERY_CURRENT_NOISE_MA, saturation=(0.0, 3200.0)),
            # Flight state machine numeric twin of the mission_state string.
            "mission_state_code": SensorModel("FSM", "enum", quantization=1.0),
        }
        return cls(models=m)

    def get(self, field_name: str) -> Optional[SensorModel]:
        return self.models.get(field_name)


# ---------------------------------------------------------------------------
# Per-field sensor profiles.
#
# Each ``simulate_*`` returns the altitude/physics-dependent profile value.
# When an ``rng`` is supplied, Gaussian noise of the documented std is added;
# without one the noise-free (ground-truth) profile is returned. The mission
# simulator calls these without an ``rng`` and lets the matching
# :class:`SensorModel` above add noise/dropout, so a mission stays
# reproducible from one seed and noise is never applied twice.
# ---------------------------------------------------------------------------
def _noisy(value: float, std: float, rng: Optional[random.Random]) -> float:
    if rng is not None and std > 0.0:
        value += rng.gauss(0.0, std)
    return value


def _alt_frac(alt_m: float, top_m: float = 1000.0) -> float:
    """Fraction of the way from the ground to ``top_m``, clamped to [0, 1]."""

    return max(0.0, min(1.0, float(alt_m) / top_m))


def simulate_bme688_gas_resistance(alt_m: float, rng: Optional[random.Random] = None,
                                   noise_std: float = GAS_RESISTANCE_NOISE_OHM) -> float:
    """BME688 gas resistance (Ohm): ~50 kOhm at ground -> ~120 kOhm at 1000 m.

    Cleaner air aloft means fewer reducing gases on the MOX surface, so the
    resistance rises with altitude.
    """

    value = 50_000.0 + 70_000.0 * _alt_frac(alt_m)
    return max(1000.0, _noisy(value, noise_std, rng))


def simulate_ens160_tvoc(alt_m: float, rng: Optional[random.Random] = None,
                         noise_std: float = ENS160_TVOC_NOISE_PPB) -> float:
    """ENS160 TVOC (ppb): ~150 ppb in the boundary layer -> ~30 ppb at 1000 m."""

    excess = boundary_layer_gas_profile(alt_m)["tvoc_mult"] - 1.0   # 0.3 .. 0
    value = 30.0 + 400.0 * excess
    return max(0.0, _noisy(value, noise_std, rng))


def simulate_ens160_eco2(alt_m: float, rng: Optional[random.Random] = None,
                         noise_std: float = ENS160_ECO2_NOISE_PPM) -> float:
    """ENS160 equivalent CO2 (ppm): ~420 ppm at ground -> ~400 ppm at 1000 m."""

    value = 400.0 * boundary_layer_gas_profile(alt_m)["eco2_mult"]
    # The ENS160 never reports eCO2 below its 400 ppm floor.
    return max(400.0, _noisy(value, noise_std, rng))


def simulate_ens160_aqi(alt_m: float, rng: Optional[random.Random] = None) -> int:
    """ENS160 AQI-UBA (integer 1..5): 2 inside the boundary layer, 1 aloft.

    With an ``rng`` there is a small (5 %) chance of reading one class off.
    """

    excess = boundary_layer_gas_profile(alt_m)["tvoc_mult"] - 1.0
    aqi = 2 if excess >= 0.15 else 1
    if rng is not None and rng.random() < 0.05:
        aqi += rng.choice((-1, 1))
    return int(max(1, min(5, aqi)))


def simulate_imu_accel(ax_true: float, ay_true: float, az_true: float,
                       rng: Optional[random.Random] = None,
                       noise_std: float = IMU_ACCEL_NOISE_MS2) -> Tuple[float, float, float]:
    """BMI270 accelerometer (m/s^2): true specific force + Gaussian noise."""

    return (_noisy(ax_true, noise_std, rng),
            _noisy(ay_true, noise_std, rng),
            _noisy(az_true, noise_std, rng))


def simulate_imu_gyro(gx_true: float, gy_true: float, gz_true: float,
                      rng: Optional[random.Random] = None,
                      noise_std: float = IMU_GYRO_NOISE_DPS) -> Tuple[float, float, float]:
    """BMI270 gyroscope (deg/s): true angular rate + Gaussian noise."""

    return (_noisy(gx_true, noise_std, rng),
            _noisy(gy_true, noise_std, rng),
            _noisy(gz_true, noise_std, rng))


def simulate_battery_current(power_draw_w: float, voltage_v: float,
                             rng: Optional[random.Random] = None,
                             noise_std: float = BATTERY_CURRENT_NOISE_MA) -> float:
    """INA219 load current (mA) = P / V * 1000 (+ noise)."""

    if voltage_v <= 0.0 or not math.isfinite(voltage_v):
        return float("nan")
    value = (power_draw_w / voltage_v) * 1000.0
    return max(0.0, _noisy(value, noise_std, rng))


def simulate_radiation_cpm(alt_m: float, rng: Optional[random.Random] = None,
                           noise_std: float = RADIATION_CPM_NOISE) -> float:
    """SEN0463 cosmic-ray background (CPM): ~15 at sea level -> ~25 at 1000 m.

    Low-altitude limb of the Regener-Pfotzer curve, approximated as an
    exponential rise ``15 * exp(z * ln(25/15) / 1000)``. Simulation only.
    """

    z = max(0.0, float(alt_m))
    value = 15.0 * math.exp(z * math.log(25.0 / 15.0) / 1000.0)
    return max(0.0, _noisy(value, noise_std, rng))
