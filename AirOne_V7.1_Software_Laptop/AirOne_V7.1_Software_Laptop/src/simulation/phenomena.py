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
