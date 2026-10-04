"""True-value models for radiation, GNSS trajectory, and gas plumes.

These produce the *ground-truth* environment that sensors then observe (with
error) in :mod:`sensors`. Kept deterministic given a seed / time so a mission
is fully reproducible.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# --------------------------------------------------------------------------
# Radiation: cosmic-ray background rises with altitude (Regener-Pfotzer-like).
# --------------------------------------------------------------------------
def true_radiation_cpm(altitude_m: float, base_cpm: float = 20.0) -> float:
    """Approximate background count rate (CPM) as a function of altitude.

    Rises roughly exponentially with altitude toward the Pfotzer maximum. This
    is a simplified model for simulation only, not a physical prediction.
    """

    # Gentle exponential growth; ~doubles every ~4 km within CanSat range.
    return base_cpm * math.exp(altitude_m / 6000.0)


# --------------------------------------------------------------------------
# GNSS trajectory: horizontal drift under a constant wind.
# --------------------------------------------------------------------------
@dataclass
class GroundTrack:
    start_lat: float = 51.0
    start_lon: float = -1.0
    wind_east_ms: float = 4.0
    wind_north_ms: float = 2.0

    def position(self, t_s: float) -> tuple:
        """Return (lat, lon) after drifting for ``t_s`` seconds under wind."""

        # Convert metre offsets to degrees (approx near the start latitude).
        d_north_m = self.wind_north_ms * t_s
        d_east_m = self.wind_east_ms * t_s
        dlat = d_north_m / 111_320.0
        dlon = d_east_m / (111_320.0 * math.cos(math.radians(self.start_lat)))
        return self.start_lat + dlat, self.start_lon + dlon


# --------------------------------------------------------------------------
# Gas plume: Gaussian concentration bump encountered during descent.
# --------------------------------------------------------------------------
@dataclass
class GasPlume:
    """A VOC-index bump centred at a given altitude during descent."""

    center_alt_m: float = 400.0
    width_m: float = 120.0
    peak_index: float = 260.0
    baseline_index: float = 100.0

    def voc_index(self, altitude_m: float) -> float:
        bump = self.peak_index * math.exp(-((altitude_m - self.center_alt_m) ** 2) / (2 * self.width_m ** 2))
        return self.baseline_index + bump

    def nox_index(self, altitude_m: float) -> float:
        bump = (self.peak_index / 100.0) * math.exp(-((altitude_m - self.center_alt_m) ** 2) / (2 * self.width_m ** 2))
        return 1.0 + bump


# --------------------------------------------------------------------------
# UV attenuation: less overhead air column -> slightly more UV with altitude.
# --------------------------------------------------------------------------
UV_SCALE_HEIGHT_M = 8000.0   # effective scale height of the attenuating column
UV_OPTICAL_DEPTH = 0.65      # tuned so the factor is ~1.08 at 1000 m


def uv_attenuation_factor(alt_m: float) -> float:
    """Multiplier for UV irradiance versus altitude (1.0 at ground).

    Beer-Lambert style: the attenuating column above the CanSat falls off as
    ``exp(-z/H)`` (H ~ 8 km), so the transmitted fraction relative to the
    ground is ``exp(tau * (1 - exp(-z/H)))``. ~1.0 at 0 m, ~1.08 at 1000 m.
    Simplified model for simulation only.
    """

    z = max(0.0, float(alt_m))
    return math.exp(UV_OPTICAL_DEPTH * (1.0 - math.exp(-z / UV_SCALE_HEIGHT_M)))


# --------------------------------------------------------------------------
# Boundary-layer gas chemistry: VOC/CO2 trapped below the PBL top.
# --------------------------------------------------------------------------
PBL_CENTER_M = 350.0         # midpoint of the 200-500 m transition
PBL_WIDTH_M = 50.0           # sigmoid width (~95 % complete at 200/500 m)
PBL_TVOC_EXCESS = 0.3        # tvoc multiplier 1.3 inside the boundary layer
PBL_ECO2_EXCESS = 0.05       # eCO2 multiplier 1.05 (~420 vs ~400 ppm)


def _pbl_fraction(alt_m: float) -> float:
    """1.0 well inside the boundary layer, 0.0 in the free troposphere."""

    x = (float(alt_m) - PBL_CENTER_M) / PBL_WIDTH_M
    x = max(-60.0, min(60.0, x))  # avoid overflow in exp
    return 1.0 / (1.0 + math.exp(x))


def boundary_layer_gas_profile(alt_m: float) -> dict:
    """VOC / eCO2 boundary-layer multipliers versus altitude.

    Below ~200 m the TVOC multiplier is ~1.3 (polluted mixed layer); above
    ~500 m it approaches the clean-air baseline 1.0, with a smooth sigmoid
    transition centred on 350 m.

    Returns ``{'tvoc_mult': float, 'eco2_mult': float}``.
    """

    f = _pbl_fraction(alt_m)
    return {
        "tvoc_mult": 1.0 + PBL_TVOC_EXCESS * f,
        "eco2_mult": 1.0 + PBL_ECO2_EXCESS * f,
    }
