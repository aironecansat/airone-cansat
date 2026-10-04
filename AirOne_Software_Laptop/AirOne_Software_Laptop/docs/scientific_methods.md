# Scientific Methods

AirOne ships **37 deterministic scientific analyses** across **9
categories**, implemented in `src/scientific/` and registered in
`src/scientific/registry.py`. Every analysis returns a `ScientificResult` with
a value, unit, explicit **uncertainty**, and the assumptions it relied on.

## Scientific-honesty rules

These rules are enforced in the analysis code and are non-negotiable:

1. **Correlation is never reported as causation.** Correlation analyses
   (e.g. `radiation_altitude_correlation`, `correlation_matrix`) report the
   coefficient and sample size and explicitly label the relationship as
   correlation only.
2. **Uncertainty is always propagated.** No analysis returns a point value
   without an uncertainty (or an explicit statement that uncertainty is not yet
   quantifiable, in which case the result is marked accordingly).
3. **Insufficient data yields an explicit "cannot compute", never a guess.**
   An analysis with too few valid samples returns an unavailable/insufficient
   result rather than a fabricated number.
4. **Invalid inputs are excluded, not imputed.** Analyses consume only valid
   measurements; missing values are never zero-filled.
5. **Simulated inputs stay labelled.** Results computed from `SIMULATED` data
   inherit that provenance.

## Categories and analyses

### Atmospheric (10)
`atmospheric_density`, `atmospheric_stability`, `boundary_layer_height`,
`dew_point`, `hypsometric_altitude`, `lapse_rate`, `pressure_gradient`,
`temperature_gradient`, `temperature_inversion`, `vertical_profile`.

Core relations used include the barometric/hypsometric equation for
pressure-derived altitude, the environmental lapse rate (dT/dz), the
Magnus/Arden-Buck formula for dew point, and the ideal-gas relation for air
density. Stability and inversion detection compare the measured lapse rate to
the dry adiabatic lapse rate.

### Descent (4)
`descent_rate`, `drag_coefficient`, `landing_detection`, `terminal_velocity`.

Descent rate is the time derivative of altitude; terminal velocity and the
effective drag coefficient are derived from the balance of gravity and drag
during steady descent. Landing detection combines low vertical speed with
altitude stability.

### Gases (3)
`air_quality_proxy`, `plume_detection`, `voc_profile`.

VOC/gas-resistance readings from the BME688 are turned into an air-quality
proxy and a vertical VOC profile; plume detection flags localised
concentration anomalies.

### GNSS (4)
`displacement_from_start`, `ground_speed`, `ground_track_distance`,
`vertical_speed`.

Horizontal metrics use great-circle (haversine) distance between fixes;
vertical speed uses GNSS altitude deltas. All require valid GNSS fixes — with
no fix, the analyses report unavailable rather than emitting zeros.

### Magnetic (4)
`magnetic_anomaly`, `magnetic_field_magnitude`, `magnetic_heading`,
`magnetic_inclination`.

Field magnitude is the Euclidean norm of the three magnetometer axes; heading
and inclination are derived from the field vector; anomalies are deviations
from the expected local field.

### Power (3)
`battery_health`, `brownout_risk`, `voltage_trend`.

Voltage trend is a regression over the battery-voltage series; brownout risk
compares projected voltage against a threshold; battery health summarises the
discharge behaviour.

### Radiation (4)
`dose_rate`, `mean_count_rate`, `radiation_altitude_correlation`,
`radiation_burst`.

Count rates (CPM) are converted to an approximate dose rate; the
altitude–radiation relationship is reported strictly as a correlation with its
coefficient and sample count; bursts are transient rate excursions.

### UV / Solar (2)
`uv_altitude_trend`, `uv_index`.

UV index from the UV sensor and its trend with altitude.

### Composite (3)
`change_rate_monitor`, `correlation_matrix`, `environmental_hazard_index`.

Cross-sensor summaries: a rate-of-change monitor, a correlation matrix across
key channels (correlation only — never causal), and a composite environmental
hazard index.

## Filtering and fusion

Before analyses run, the pipeline can apply configurable filters (stage 14) and
sensor fusion (stage 15):

- **Kalman 1-D** (e.g. fused pressure), **moving average** (temperature),
  **EKF altitude** — all configured under `pipeline.filter_config`.
- Fused outputs are tagged `DataSource.FUSED`; filtered outputs are tagged
  appropriately so consumers always know the provenance of a number.

## Narrative

`GET /api/v1/narrative` produces a natural-language description of the mission
from the computed results. The narrative is generated from real analysis
outputs and states uncertainty; it never asserts findings the analyses did not
support.
