"""International Standard Atmosphere (ISA) model.

Implements the 1976 US Standard Atmosphere for the troposphere and lower
stratosphere (0..20 km), which is ample for a CanSat. Returns SI units:
pressure in Pa, temperature in K, density in kg/m^3.

Reference sea-level values:
    T0 = 288.15 K, P0 = 101325 Pa, lapse rate L = 0.0065 K/m to 11 km,
    isothermal 216.65 K from 11 km to 20 km.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

T0 = 288.15          # K
P0 = 101325.0        # Pa
L = 0.0065           # K/m (troposphere)
G0 = 9.80665         # m/s^2
R = 287.05           # J/(kg*K) specific gas constant, dry air
TROPOPAUSE_ALT = 11000.0   # m
TROPOPAUSE_T = 216.65      # K
# Pressure at the tropopause from the troposphere formula.
TROPOPAUSE_P = P0 * (TROPOPAUSE_T / T0) ** (G0 / (R * L))


@dataclass
class AtmoState:
    altitude_m: float
    temperature_K: float
    pressure_Pa: float
    density_kg_m3: float


def temperature(altitude_m: float) -> float:
    """ISA temperature (K) at geopotential altitude."""

    if altitude_m <= TROPOPAUSE_ALT:
        return T0 - L * altitude_m
    return TROPOPAUSE_T


def pressure(altitude_m: float) -> float:
    """ISA pressure (Pa) at geopotential altitude."""

    if altitude_m <= TROPOPAUSE_ALT:
        return P0 * (temperature(altitude_m) / T0) ** (G0 / (R * L))
    # Isothermal layer above the tropopause.
    return TROPOPAUSE_P * math.exp(-G0 * (altitude_m - TROPOPAUSE_ALT) / (R * TROPOPAUSE_T))


def density(altitude_m: float) -> float:
    """ISA density (kg/m^3) via the ideal gas law."""

    return pressure(altitude_m) / (R * temperature(altitude_m))


def state(altitude_m: float) -> AtmoState:
    t = temperature(altitude_m)
    p = pressure(altitude_m)
    return AtmoState(
        altitude_m=altitude_m,
        temperature_K=t,
        pressure_Pa=p,
        density_kg_m3=p / (R * t),
    )
