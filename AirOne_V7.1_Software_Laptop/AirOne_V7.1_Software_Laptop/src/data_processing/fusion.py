"""Multi-sensor fusion engine.

Every fusion output is a :class:`Measurement` with ``source=FUSED`` and an
uncertainty propagated from its inputs. If all inputs are invalid the output is
explicitly INVALID — a value is never fabricated.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import List, Optional, Tuple

from ..core.models import DataSource, Measurement, QualityState, utcnow

logger = logging.getLogger(__name__)

# Physical constants for barometric altitude.
_P0 = 101325.0  # Pa, sea-level standard pressure
_T0 = 288.15    # K, sea-level standard temperature
_L = 0.0065     # K/m, temperature lapse rate
_G = 9.80665    # m/s^2
_R = 287.05     # J/(kg*K), specific gas constant for dry air
_EXP = (_R * _L) / _G


def _weight_from(m: Measurement) -> float:
    """Inverse-variance weight; falls back to a nominal weight."""

    if not m.valid:
        return 0.0
    if m.uncertainty and m.uncertainty > 0 and math.isfinite(m.uncertainty):
        return 1.0 / (m.uncertainty ** 2)
    return 1.0


def _weighted_fuse(
    inputs: List[Measurement],
    sensor_id: str,
    unit: str,
    field_name: str,
    stage: int = 15,
    timestamp: Optional[datetime] = None,
) -> Measurement:
    """Inverse-variance weighted fusion of homogeneous measurements."""

    ts = timestamp or utcnow()
    valid = [m for m in inputs if m.valid and math.isfinite(m.value)]
    if not valid:
        logger.debug("Fusion for %s: all inputs invalid", field_name)
        return Measurement.invalid(
            sensor_id=sensor_id,
            unit=unit,
            field_name=field_name,
            quality=QualityState.INVALID,
            timestamp=ts,
            processing_stage=stage,
        )

    weights = [_weight_from(m) for m in valid]
    wsum = sum(weights)
    if wsum <= 0:
        wsum = float(len(valid))
        weights = [1.0] * len(valid)
    value = sum(w * m.value for w, m in zip(weights, valid)) / wsum
    # Combined 1-sigma uncertainty of an inverse-variance estimator.
    variance = 1.0 / wsum if all(w > 0 for w in weights) else float("inf")
    uncertainty = math.sqrt(variance) if math.isfinite(variance) else 0.0
    # Confidence reduced if some inputs were dropped.
    quality = QualityState.VALID if len(valid) == len(inputs) else QualityState.SUSPECT

    return Measurement(
        value=float(value),
        unit=unit,
        timestamp=ts,
        sensor_id=sensor_id,
        quality=quality,
        valid=True,
        uncertainty=float(uncertainty),
        calibration_version="fused",
        source=DataSource.FUSED,
        processing_stage=stage,
        field_name=field_name,
    )


class FusionEngine:
    """Fuses redundant/complementary sensors into best estimates."""

    # --- pressure & temperature -------------------------------------------
    def fuse_pressure(
        self, bme688: Measurement, bmp581: Measurement
    ) -> Measurement:
        return _weighted_fuse(
            [bme688, bmp581],
            sensor_id="fused_pressure",
            unit="Pa",
            field_name="pressure",
        )

    def fuse_temperature(
        self, bme688: Measurement, bmp581: Measurement
    ) -> Measurement:
        return _weighted_fuse(
            [bme688, bmp581],
            sensor_id="fused_temperature",
            unit="K",
            field_name="temperature",
        )

    # --- UV index ---------------------------------------------------------
    def fuse_uv_index(
        self,
        uva: Measurement,
        uvb: Measurement,
        alpha_a: float = 0.303,
        alpha_b: float = 3.17,
        re_eff: float = 90.0,
    ) -> Measurement:
        """Compute a UV Index from VEML6075 UVA and UVB channels.

        UVI = (UVA*alpha_a + UVB*alpha_b) / re_eff.
        """

        ts = utcnow()
        if not (uva.valid and uvb.valid):
            return Measurement.invalid(
                sensor_id="fused_uv",
                unit="index",
                field_name="uv_index",
                timestamp=ts,
                processing_stage=15,
            )
        uvi = (uva.value * alpha_a + uvb.value * alpha_b) / re_eff
        uvi = max(0.0, uvi)
        # Propagate uncertainty in quadrature.
        du = math.sqrt(
            (alpha_a * uva.uncertainty) ** 2 + (alpha_b * uvb.uncertainty) ** 2
        ) / re_eff
        return Measurement(
            value=float(uvi),
            unit="index",
            timestamp=ts,
            sensor_id="fused_uv",
            quality=QualityState.VALID,
            valid=True,
            uncertainty=float(du),
            calibration_version="fused",
            source=DataSource.FUSED,
            processing_stage=15,
            field_name="uv_index",
        )

    # --- altitude ---------------------------------------------------------
    @staticmethod
    def pressure_altitude(pressure_pa: float, sea_level_pa: float = _P0) -> float:
        """Barometric (hypsometric) altitude in metres."""

        if pressure_pa <= 0:
            return float("nan")
        return (_T0 / _L) * (1.0 - (pressure_pa / sea_level_pa) ** _EXP)

    def fuse_altitude(
        self,
        pressure: Measurement,
        gnss_altitude: Measurement,
        sea_level_pa: float = _P0,
    ) -> Measurement:
        """Fuse barometric altitude with GNSS altitude."""

        ts = utcnow()
        baro: Optional[Measurement] = None
        if pressure.valid and math.isfinite(pressure.value):
            alt = self.pressure_altitude(pressure.value, sea_level_pa)
            if math.isfinite(alt):
                # Convert pressure uncertainty to altitude uncertainty numerically.
                dp = pressure.uncertainty if pressure.uncertainty > 0 else 10.0
                alt_hi = self.pressure_altitude(
                    max(pressure.value - dp, 1.0), sea_level_pa
                )
                unc = abs(alt_hi - alt)
                baro = Measurement(
                    value=alt,
                    unit="m",
                    timestamp=ts,
                    sensor_id="baro_altitude",
                    quality=QualityState.VALID,
                    valid=True,
                    uncertainty=unc if unc > 0 else 5.0,
                    source=DataSource.ESTIMATED,
                    processing_stage=15,
                    field_name="altitude",
                )
        inputs = [m for m in (baro, gnss_altitude) if m is not None]
        if not inputs:
            return Measurement.invalid(
                sensor_id="fused_altitude",
                unit="m",
                field_name="altitude",
                timestamp=ts,
                processing_stage=15,
            )
        return _weighted_fuse(
            inputs,
            sensor_id="fused_altitude",
            unit="m",
            field_name="altitude",
            timestamp=ts,
        )

    # --- heading ----------------------------------------------------------
    def fuse_heading(
        self,
        mag_heading: Measurement,
        gnss_track: Measurement,
        pdop: float = 1.0,
    ) -> Measurement:
        """Fuse magnetometer heading with GNSS course-over-ground.

        GNSS track is de-weighted as PDOP grows (poor geometry).
        """

        ts = utcnow()
        candidates: List[Measurement] = []
        if mag_heading.valid and math.isfinite(mag_heading.value):
            candidates.append(mag_heading)
        if gnss_track.valid and math.isfinite(gnss_track.value):
            # Inflate GNSS track uncertainty by PDOP.
            base = gnss_track.uncertainty if gnss_track.uncertainty > 0 else 5.0
            candidates.append(
                Measurement(
                    value=gnss_track.value,
                    unit="deg",
                    timestamp=ts,
                    sensor_id=gnss_track.sensor_id,
                    quality=gnss_track.quality,
                    valid=True,
                    uncertainty=base * max(pdop, 1.0),
                    source=gnss_track.source,
                    processing_stage=15,
                    field_name="heading",
                )
            )
        if not candidates:
            return Measurement.invalid(
                sensor_id="fused_heading",
                unit="deg",
                field_name="heading",
                timestamp=ts,
                processing_stage=15,
            )
        # Circular-aware weighted fusion via unit vectors.
        weights = [_weight_from(m) for m in candidates]
        wsum = sum(weights) or float(len(candidates))
        sx = sum(w * math.cos(math.radians(m.value)) for w, m in zip(weights, candidates))
        sy = sum(w * math.sin(math.radians(m.value)) for w, m in zip(weights, candidates))
        heading = math.degrees(math.atan2(sy / wsum, sx / wsum)) % 360.0
        unc = math.sqrt(1.0 / wsum) if wsum > 0 else 0.0
        quality = (
            QualityState.VALID if len(candidates) == 2 else QualityState.SUSPECT
        )
        return Measurement(
            value=float(heading),
            unit="deg",
            timestamp=ts,
            sensor_id="fused_heading",
            quality=quality,
            valid=True,
            uncertainty=float(unc),
            calibration_version="fused",
            source=DataSource.FUSED,
            processing_stage=15,
            field_name="heading",
        )
