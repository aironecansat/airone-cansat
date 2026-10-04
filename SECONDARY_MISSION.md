# AirOne Secondary Mission: **AirChem-Rad**

### Vertical profiling of air chemistry, ultraviolet light and ionising radiation in the lowest kilometre of the atmosphere

#### Overview

AirChem-Rad uses the AirOne CanSat to measure, on one flight, how air chemistry, UV light and background ionising radiation change with altitude. It uses the ascent and the parachute descent. A metal-oxide gas sensor (BME688), a VOC/NOx index sensor (SGP41) and a TVOC/eCO₂ sensor (ENS160) measure air chemistry. A UV-A/UV-B photodiode (VEML6075) and an ambient-light sensor (OPT3001) measure sunlight. A Geiger–Müller counter (DFRobot SEN0463) measures ionising radiation. Every sample is tagged with barometric altitude (BMP581/BME688), GNSS position (MAX-M10S), attitude/spin (BMI270 + MMC5603), a DS3231-backed timestamp and the on-board flight-state machine (`mission_state`). This gives one time-aligned vertical profile. On the ground we test whether the lowest atmospheric layer (the boundary layer) is chemically different from the air above it, whether UV and radiation follow their expected altitude trends, and how far a low-cost CanSat can detect these effects. The mission reuses the primary-mission sensors and needs no extra hardware or ground-station software changes. All data use the existing JSON convention, and a missing sensor reading is omitted, never sent as zero.

---

### 1\. Hypotheses (numbered, testable)

Each hypothesis has a measurable quantity, a statistical test and a pass/fail criterion. "Altitude" means `altitude_rel` (metres above the launch-site baseline that the firmware averages during `SELF_TEST`).

| \# | Hypothesis | Measured quantity | Test | Supported if… |
| --- | --- | --- | --- | --- |
| **H1** | VOC concentration falls with altitude, so the BME688 gas resistance rises (cleaner air raises MOX resistance). | `bme688_gas_resistance`, `sgp41_voc_raw`, `ens160_tvoc` vs `altitude_rel` | Spearman ρ, descent leg only | ρ(R_gas, alt) > 0 and ρ(TVOC, alt) < 0, each with p < 0.05 |
| **H2** | If the flight crosses the boundary-layer (BL) top, the chemistry changes as a **step**, not a smooth gradient, at the same height as the thermodynamic BL top. | Chemistry change-point height _z_\_chem vs θ / humidity-gradient height _z_\_BL | Piecewise-linear (one breakpoint) fit vs linear fit, compared by ΔBIC | ΔBIC > 6 favouring the breakpoint model **and** \|_z_\_chem − _z_\_BL\| < 50 m. A null result is valid if _z_\_BL is above the apogee. |
| **H3** | Radiation count rate depends on altitude. Close to the ground the terrestrial-gamma part falls with height, while the cosmic-ray part slowly rises (roughly exponentially, e-folding over km). | `radiation_counts` (per-sample increments) binned by altitude | Poisson maximum-likelihood fit of the model in §4.3; likelihood-ratio test vs a constant rate | Constant-rate model rejected at p < 0.05. If not, report an upper limit on the gradient. |
| **H4** | UV-B rises faster with altitude than UV-A (less ozone/aerosol path), so the UV-B/UV-A ratio increases with height. | `veml6075_uvb / veml6075_uva` (upward-facing, sun-lit samples only) | Weighted linear regression of ratio vs altitude | Slope > 0 at 95 % confidence |
| **H5** | Profiles measured on ascent and descent at the same altitude agree within sensor uncertainty, unless the sensor's response time causes **hysteresis**. | Same quantity, ascent vs descent, in matched altitude bins | Paired Wilcoxon signed-rank test per quantity | Agreement (p > 0.05) for BMP581/BME688 T and P; any significant hysteresis in the gas channels is quantified as a time constant τ |
| **H6** | Once the parachute stabilises (≈ 5 s after `DESCENT` starts), the spin rate settles to a near-constant value. Pendulum/spin motion modulates the UV and light readings at that frequency. | `imu_gyro_z`, `mag_x/y`, `opt3001_lux` | FFT / Lomb–Scargle of lux vs gyro-derived spin frequency | Lux spectral peak within ±10 % of the spin frequency. This confirms the attitude filter used for H4 is needed. |

---

### 2\. Sensors used and what they measure

All fields are sent as `field -> {value, unit, sensor_id}`. Fields not read successfully in a cycle are **omitted**.

| Sensor (`sensor_id`) | Bus | Telemetry fields (unit) | Role in AirChem-Rad |
| --- | --- | --- | --- |
| **BME688** | I²C via PCA9548A (0x76) | `bme688_temperature` (K), `bme688_pressure` (Pa), `bme688_humidity` (%), `bme688_gas_resistance` (Ohm) | Temperature and humidity for potential temperature θ and specific humidity _q_ (BL detection). MOX gas resistance is a broad VOC/reducing-gas proxy (H1, H2). |
| **BMP581** | I²C via mux | `bmp581_temperature` (K), `bmp581_pressure` (Pa) | Main high-resolution pressure for altitude. Second temperature to cross-check self-heating. |
| **SGP41** | I²C via mux | `sgp41_voc_raw`, `sgp41_nox_raw` (ticks); `sgp41_voc`, `sgp41_nox` (Sensirion Gas Index, 0–500) | VOC and NOx (oxidising gases). Raw ticks are used for profiles, because the Gas Index adapts to its own baseline (§4.1). The index is sent only once the algorithm has a valid value. |
| **ENS160** | I²C via mux | `ens160_tvoc` (ppb), `ens160_eco2` (ppm), `ens160_aqi` (1–5), `ens160_status` (flag) | Second, independent MOX-array estimate of TVOC/eCO₂. The status flag marks warm-up/initial-start-up samples, which are excluded. |
| **VEML6075** | I²C via mux | `veml6075_uva`, `veml6075_uvb` (counts) | UV-A (≈365 nm) and UV-B (≈330 nm) irradiance proxies (H4). |
| **OPT3001** | I²C via mux | `opt3001_lux` (lux) | Visible light. Used to detect sun-facing vs shadowed samples and the spin modulation (H6). |
| **SEN0463 Geiger counter** | GPIO interrupt | `radiation_counts` (cumulative counts), `radiation_cpm` (CPM, 60 s rolling window) | Ionising radiation (β/γ) (H3). Fits use cumulative counts; CPM is for display only (it smooths over 60 s). |
| **BMI270** | SPI (CS = 15) | `imu_accel_x/y/z` (m/s²), `imu_gyro_x/y/z` (deg/s) | Launch detection, spin rate, attitude filter for UV/light samples (H6). |
| **MMC5603** | I²C via mux | `mag_x/y/z` (µT) | Heading/spin phase, independent of gyro drift. |
| **MAX-M10S GNSS** | UART2 | `gnss_lat`, `gnss_lon` (deg), `gnss_altitude` (m) | Horizontal drift (wind profile) and an independent altitude check. |
| **DS3231 RTC** | I²C via mux | Frame-header timestamp (µs since Unix epoch: GNSS → DS3231 → 0) | Absolute time when GNSS has no fix. Needed to compare with weather and solar-elevation data. |
| **INA219** | I²C via mux | `battery_voltage` (V), `battery_current_ma` (mA) | Housekeeping. Large current steps (heater cycles of the MOX sensors) are flagged so they are not mistaken for real chemistry changes. |
| **Flight-state machine** (`FSM`) | — | `altitude_rel` (m), `vertical_speed` (m/s), `mission_state` (string), `mission_state_code` (0–6), `sensor_health_mask` (bitmask) | Splits the flight into BOOT, SELF_TEST, PRELAUNCH, ASCENT, APOGEE, DESCENT and LANDED for analysis (codes 0–6 in that order). |

> **Note on `mission_state`.** The unchanged ground parser accepts only numeric values, so it marks the string field `mission_state` invalid but still accepts the frame. Ground analysis therefore uses **`mission_state_code`** (0 = BOOT … 6 = LANDED). The string is kept for people reading the raw logs.

---

### 3\. Expected altitude profiles

The expected flight envelope is a rocket- or drone-launched CanSat reaching a few hundred metres to ≈1 km, with a parachute descent of tens of seconds to a few minutes. The values below are expected **trends**, not calibrated predictions.

| Quantity | Expected profile with altitude | Physical reason | Main confounders |
| --- | --- | --- | --- |
| Pressure (`*_pressure`) | Falls ≈ 12 Pa per metre near the surface (hypsometric) | Hydrostatic balance | None significant. This is the altitude reference. |
| Temperature | Falls ≈ 6.5–9.8 K/km. It may **rise** with height through a surface or BL-top inversion. | Adiabatic cooling / inversions | Solar heating of the can, sensor self-heating. Cross-check BMP581 against BME688. |
| Potential temperature θ | About constant inside a well-mixed daytime BL, then a sharp rise at the BL top | Mixing within the BL | — |
| Humidity / specific humidity _q_ | About constant in the BL, then drops above the BL top | Surface moisture source, trapped below the inversion | RH sensor lag on fast ascent |
| `bme688_gas_resistance` | **Increases** with altitude, with a step increase at the BL top | Fewer reducing VOCs aloft | Strong dependence on humidity and temperature. Corrected in §4.1. |
| `sgp41_voc_raw` / `ens160_tvoc` | **Decrease** with altitude, with a step decrease at the BL top | Surface VOC sources (traffic, vegetation) mixed only within the BL | Sensor response time (seconds to tens of seconds) causes ascent/descent hysteresis (H5) |
| `sgp41_nox_raw` | Small decrease, possibly within noise at low NO₂ | Surface NOx sources | Low sensitivity at background levels |
| `ens160_eco2` | Weak decrease. This is a TVOC-derived **equivalent**, not real CO₂. | Calculated from MOX response | Should not be read as CO₂ |
| `veml6075_uvb`, `veml6075_uva` | Both **increase** slightly (a few % per 100s of m). UV-B/UV-A increases. | Shorter path through ozone/aerosol, less haze below the BL top | Attitude/spin (sensor not sun-facing), clouds. Strong filtering needed (§4.4). |
| `opt3001_lux` | Strongly modulated by spin. The envelope rises slightly with altitude above haze. | Aerosol extinction | Spin and pendulum motion |
| Radiation count rate | Near the ground: falls over the first \~100–300 m as the terrestrial γ-ray contribution (⁴⁰K, U/Th series) is absorbed by air. Higher up: a slow rise from the cosmic-ray secondary component. Over < 1 km the net change is small (tens of %). | Air absorbs ground radiation; the cosmic-ray flux grows with height up to the Pfotzer maximum at ≈ 15–20 km, which is far above CanSat altitude | **Low count statistics.** At tens of CPM, a 2–3 minute flight gives only tens of counts in total (§4.3). |
| Spin rate (`imu_gyro_z`) | High and irregular at ejection, then settles to a near-constant rate within seconds | Parachute aerodynamics | — |

---

### 4\. Data products and analysis plan

#### 4.1 Pre-processing (ground side)

1. **Load** frames logged by the unchanged ground station (CSV/JSON export). Check the frame CRC (already done by the ground protocol layer) and drop duplicate sequence numbers. The firmware's FRAM keeps `seq` increasing across a brownout reset, so gaps in `seq` show lost packets, not reboots.

2. **Segment** by `mission_state_code`. Profiles use ASCENT (3) and DESCENT (5). PRELAUNCH (2) gives the ground reference and sensor warm-up check. LANDED (6) gives a second ground reference.

3. **Altitude.** Use `altitude_rel` from the on-board filter as the main altitude. Recompute it independently from `bmp581_pressure` with the hypsometric equation, using the measured surface temperature. Use `gnss_altitude` only as a sanity check (its vertical error is \~several m).

4. **Derived variables:**

  * Potential temperature θ = T · (100000 / p)^0.2857

  * Specific humidity _q_ from RH, T and p (Magnus formula)

  * Humidity-corrected gas resistance: fit ln R_gas = a + b·_q_ + c·T on the PRELAUNCH segment, then use the residual in flight. This takes out the known humidity/temperature sensitivity of MOX sensors.

5. **Quality flags:**

  * Remove ENS160 samples whose `ens160_status` is not "normal operation".

  * Remove SGP41 _index_ values during the algorithm's start-up blackout. Raw ticks are always kept.

  * Use `sensor_health_mask` to remove samples from sensors that dropped off the bus.

  * Flag samples with a battery-current step > 50 mA.

#### 4.2 Products

| Product | Description | Format |
| --- | --- | --- |
| **P1** Time series | Every field vs time, with flight phases shaded | PNG + CSV |
| **P2** Binned vertical profiles | Each quantity averaged in 10 m altitude bins (20 m for UV, 50 m for radiation), ascent and descent separately, with median ± IQR and N per bin | PNG + CSV |
| **P3** Correlation matrix | Spearman ρ between every science quantity and altitude, with p-values (Holm–Bonferroni corrected for 6 hypotheses) | Table |
| **P4** Boundary-layer report | _z_\_BL from θ and _q_ gradients, _z_\_chem from change-point fits, with confidence intervals | Table + annotated profile plot |
| **P5** Radiation fit | Fitted model parameters, likelihood-ratio statistic, counts per bin with Poisson error bars | Table + plot |
| **P6** Hysteresis/response-time | Ascent-minus-descent differences and fitted first-order τ for each gas sensor | Table |
| **P7** Flight track | GNSS ground track and drift-derived wind estimate vs altitude | Map/plot |

#### 4.3 Radiation (CPM-vs-altitude) fit

* **Counts, not CPM.** The firmware's `radiation_cpm` is a 60 s rolling window, which smooths over \~60 s and correlates neighbouring samples. Fit instead the per-sample **increments** Δ_n_\_i of `radiation_counts` over intervals Δ_t_\_i. These are independent Poisson variables.

* **Model:** λ(_z_) = _A_·e^(−_z_/_L_\_γ) + _B_·e^(_z_/_H_\_c) + _C_.

  * The first term is terrestrial γ (_L_\_γ ≈ 100–200 m in air).

  * The second term is cosmic (_H_\_c ≈ 1–2 km).

  * _C_ is the tube's intrinsic background.

  * Over a short flight the model is too flexible, so _L_\_γ and _H_\_c are **fixed** at literature values and only _A_ and _B_ are fitted. _C_ is constrained by a long ground measurement before launch.

* **Likelihood:** ln ℒ = Σ\_i \[Δ_n_\_i ln(λ(_z_\_i)Δ_t_\_i) − λ(_z_\_i)Δ_t_\_i\]. Maximise it, and compare with the constant-rate model using the likelihood-ratio statistic (χ², 1 d.o.f.).

* **Honest statistics.** At \~20–30 CPM background and a flight of \~2–3 minutes, the total is roughly 40–90 counts. That is enough to detect a change of more than \~30–50 % between the bottom and top halves of the profile, but not small gradients. The report therefore gives either a detection or a **90 % upper limit** on the gradient. To improve the statistics:

  1. Combine ascent and descent.

  2. Run a long pre-launch ground baseline (≥ 30 min, \~10³ counts).

  3. If allowed, add a tethered-drone or tall-building calibration profile using the same firmware.

#### 4.4 UV analysis

* Use only samples where the payload is roughly sun-facing: `opt3001_lux` is in the top 20 % of its rolling 2 s window, **and** the tilt from `imu_accel_*` is < 30°.

* Compute the UV-B/UV-A ratio from these samples only.

* Do the regression against altitude, weighted by 1/variance per bin.

* Compare the result with the solar elevation at the frame timestamp, so the ratio is not confused with a change in sun angle (which is negligible over a few minutes).

#### 4.5 Uncertainty budget

Combine the following in quadrature for every binned profile:

* Sensor datasheet accuracy

* Bin standard error

* Altitude error (BMP581 ≈ ±0.5 m relative over the flight, plus temperature-assumption error in the hypsometric equation)

* Response-time smearing (τ × vertical speed)

The response-time smearing usually dominates for the gas sensors. Ascent at tens of m/s gives far worse vertical resolution than descent at \~5–10 m/s, so **descent is the primary science leg** for H1, H2 and H4.

---

### 5\. Ground-side analysis methods

#### 5.1 Correlation with altitude

* **Pearson r** (linear) and **Spearman ρ** (monotonic, robust to sensor non-linearity), computed on the descent leg for each science quantity vs `altitude_rel`.

* Autocorrelation in a time series inflates significance. The **effective sample size** is therefore estimated as _N_\_eff = _N_·(1 − r₁)/(1 + r₁), where r₁ is the lag-1 autocorrelation, and _N_\_eff is used for p-values.

* Bootstrap (block bootstrap, 5 s blocks) 95 % confidence intervals are reported for ρ.

#### 5.2 Boundary-layer detection

Use three independent estimators and report all three:

1. **θ-gradient method:** _z_\_BL is the lowest height where dθ/d_z_ > 3.5 K/km after smoothing with a 30 m running median. This is the parcel/gradient method used for radiosondes.

2. **Humidity method:** _z_\_BL is the height of the most negative d_q_/d_z_.

3. **Chemistry change-point:** fit a two-segment piecewise-linear model to humidity-corrected ln R_gas, `sgp41_voc_raw` and `ens160_tvoc`, searching for the breakpoint _z_\_chem on a 5 m grid. Compare with a single linear fit using BIC (ΔBIC > 6 means strong evidence for a step).

H2 is tested by checking whether _z_\_chem agrees with methods 1 and 2. If no θ inversion appears below apogee, the BL top is above the flight ceiling. That is recorded as an informative null result, together with the context: time of day, cloud base, and the nearest radiosonde or model BL height if available.

#### 5.3 CPM-vs-altitude fit

Follow §4.3: a Poisson maximum-likelihood fit on count increments, a likelihood-ratio test against a constant rate, and a profile-likelihood 68 %/90 % interval on _B_/_A_. As a cross-check, plot the bin-averaged CPM (50 m bins) with √_N_ error bars, with the fitted curve overlaid.

#### 5.4 Ascent/descent hysteresis

* For each gas channel, model the sensor as a first-order system _y_\_meas(_t_) = _y_\_true(_z_(_t_)) ∗ (1/τ)e^(−_t_/τ).

* Fit τ so that the ascent and descent profiles collapse onto one curve.

* Report τ, and deconvolve the descent profile with it before running the H1/H2 tests.

#### 5.5 Implementation

* Analysis scripts in Python (NumPy, SciPy, pandas, matplotlib) read the ground station's existing exports. The ground software already parses `bme688_gas_resistance`, `battery_current_ma` and `mission_state_code` as ordinary numeric fields, so the **ground station software needs no changes**.

* The same scripts run on the PRELAUNCH segment and on a bench test with a hand-moved "flight", so the pipeline is validated before launch day.

---

### 6\. Relevance to the UK CanSat Competition

* **Fits the competition structure.** The UK CanSat Competition requires every team to do the common **primary mission**: measure air temperature and air pressure and transmit them to the ground station during descent. Each team also designs its own **secondary mission**. AirChem-Rad uses the primary-mission measurements (BMP581/BME688 temperature and pressure) as its altitude backbone and adds a scientific question on top. Teams should check the current year's official guidelines for exact requirements (mass, size, telemetry rate, descent rate, recovery).

* **Scientific merit judged by the panel.** The mission has clear hypotheses with stated pass/fail criteria, an uncertainty budget, and a planned null-result interpretation. That shows the scientific method, rather than "we logged lots of sensors".

* **Real-world link.** Boundary-layer air quality affects urban pollution forecasting. Radiation-vs-altitude links to aviation dosimetry and cosmic-ray physics. UV-vs-altitude links to public-health UV index forecasts. All three are easy to explain at the presentation and in the outreach report.

* **Engineering maturity.** The firmware's flight-state machine, FRAM brownout recovery (no lost sequence numbers or flight phase after a reset), hardware watchdog and honest data omission show robust design. Judges also look at reliability and data integrity, not only the science.

* **Feasible on CanSat hardware.** It needs no extra hardware beyond the sensor suite, stays within the existing radio telemetry format, and its statistics are planned for a short, low-altitude flight. The limits (especially radiation count statistics) are stated up front, as judges expect.

* **Post-flight deliverable.** Products P1–P7 map directly onto the post-flight results section of the competition's final report and presentation.

---

### 7\. Operational requirements for the secondary mission

1. **Power-on ≥ 10 minutes before launch.** Lets the MOX sensors (BME688, ENS160, SGP41) complete their warm-up, gives the SGP41 Gas Index algorithm time to start, and allows a long radiation ground baseline. The firmware stays in PRELAUNCH and keeps transmitting during this time.

2. **Record a ≥ 30 min ground radiation baseline** with the same unit on launch day, at the launch site.

3. **Note the conditions:** time, cloud cover, wind and nearby emission sources (vehicles, generators) for the chemistry interpretation.

4. **Mounting:**

  * Keep the gas-sensor inlet clear of the parachute bay and of battery/regulator heat.

  * Mount the VEML6075/OPT3001 behind a UV-transparent window (PTFE diffuser or quartz). Ordinary acrylic/polycarbonate blocks UV-B.

5. **Recovery:** after landing, keep logging for ≥ 2 min in LANDED to get the second ground reference.