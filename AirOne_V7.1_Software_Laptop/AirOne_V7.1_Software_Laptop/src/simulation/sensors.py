"""Sensor emulation with realistic, reproducible error characteristics.

Each :class:`SensorModel` applies, in order: a fixed bias, a slow linear drift,
Gaussian noise, optional saturation clamping, and a random dropout probability.
All randomness is drawn from a caller-supplied :class:`random.Random` so an
entire mission is reproducible from a single seed.

A dropout yields ``None`` — the caller is responsible for turning that into an
explicitly INVALID / MISSING measurement. The simulator never emits a silent 0.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional, Tuple


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
        }
        return cls(models=m)

    def get(self, field_name: str) -> Optional[SensorModel]:
        return self.models.get(field_name)
