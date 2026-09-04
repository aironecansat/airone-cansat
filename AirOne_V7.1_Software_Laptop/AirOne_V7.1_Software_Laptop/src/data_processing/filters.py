"""Signal-processing filter registry.

Every filter is a small stateful object exposing ``process(value, ts)`` and a
common metadata surface (``name``, ``window_size``, ``introduces_delay``,
``causal``, ``parameters``). Filters that require SciPy degrade gracefully when
SciPy is unavailable.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

try:  # SciPy is optional but recommended.
    from scipy.signal import butter, savgol_filter, sosfilt, sosfilt_zi

    _SCIPY = True
except Exception:  # noqa: BLE001
    _SCIPY = False
    logger.warning("SciPy unavailable: Savitzky-Golay and Butterworth filters disabled")


class BaseFilter:
    """Common interface for all filters."""

    name: str = "base"
    introduces_delay: bool = False
    causal: bool = True

    def __init__(self) -> None:
        self.window_size: int = 1
        self.parameters: Dict[str, Any] = {}

    def process(self, value: float, ts: float = 0.0) -> float:
        raise NotImplementedError("Subclasses must implement process()")

    def reset(self) -> None:  # pragma: no cover - trivial default
        pass

    def metadata(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "window_size": self.window_size,
            "introduces_delay": self.introduces_delay,
            "causal": self.causal,
            "parameters": dict(self.parameters),
        }


class NoFilter(BaseFilter):
    name = "none"

    def process(self, value: float, ts: float = 0.0) -> float:
        return value


class MovingAverageFilter(BaseFilter):
    name = "moving_average"

    def __init__(self, window: int = 5) -> None:
        super().__init__()
        if window < 1:
            raise ValueError("window must be >= 1")
        self.window_size = window
        self._buf: Deque[float] = deque(maxlen=window)
        self.parameters = {"window": window}

    def process(self, value: float, ts: float = 0.0) -> float:
        if math.isnan(value):
            return value
        self._buf.append(value)
        return float(sum(self._buf) / len(self._buf))

    def reset(self) -> None:
        self._buf.clear()


class ExponentialMovingAverageFilter(BaseFilter):
    name = "ema"

    def __init__(self, alpha: float = 0.3) -> None:
        super().__init__()
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha
        self._value: Optional[float] = None
        self.parameters = {"alpha": alpha}

    def process(self, value: float, ts: float = 0.0) -> float:
        if math.isnan(value):
            return value
        if self._value is None:
            self._value = value
        else:
            self._value = self.alpha * value + (1.0 - self.alpha) * self._value
        return float(self._value)

    def reset(self) -> None:
        self._value = None


class MedianFilter(BaseFilter):
    name = "median"

    def __init__(self, window: int = 5) -> None:
        super().__init__()
        if window < 1:
            raise ValueError("window must be >= 1")
        self.window_size = window
        self._buf: Deque[float] = deque(maxlen=window)
        self.parameters = {"window": window}

    def process(self, value: float, ts: float = 0.0) -> float:
        if math.isnan(value):
            return value
        self._buf.append(value)
        return float(np.median(list(self._buf)))

    def reset(self) -> None:
        self._buf.clear()


class HampelFilter(BaseFilter):
    """Hampel identifier: replace outliers exceeding n_sigma of the local MAD."""

    name = "hampel"

    def __init__(self, window: int = 7, n_sigma: float = 3.0) -> None:
        super().__init__()
        if window < 3:
            raise ValueError("Hampel window must be >= 3")
        self.window_size = window
        self.n_sigma = n_sigma
        self._buf: Deque[float] = deque(maxlen=window)
        self.parameters = {"window": window, "n_sigma": n_sigma}
        # 1.4826 scales MAD to be a consistent estimator of stddev for normal data.
        self._k = 1.4826

    def process(self, value: float, ts: float = 0.0) -> float:
        if math.isnan(value):
            return value
        self._buf.append(value)
        arr = np.asarray(self._buf, dtype=float)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        sigma = self._k * mad
        if sigma > 0 and abs(value - med) > self.n_sigma * sigma:
            return med  # Outlier: replace with local median.
        return value

    def reset(self) -> None:
        self._buf.clear()


class SavitzkyGolayFilter(BaseFilter):
    name = "savitzky_golay"
    introduces_delay = True

    def __init__(self, window: int = 7, polyorder: int = 2) -> None:
        super().__init__()
        if window % 2 == 0:
            raise ValueError("Savitzky-Golay window must be odd")
        if polyorder >= window:
            raise ValueError("polyorder must be < window")
        self.window_size = window
        self.polyorder = polyorder
        self._buf: Deque[float] = deque(maxlen=window)
        self.parameters = {"window": window, "polyorder": polyorder}

    def process(self, value: float, ts: float = 0.0) -> float:
        if math.isnan(value):
            return value
        self._buf.append(value)
        if len(self._buf) < self.window_size or not _SCIPY:
            # Not enough samples yet, or SciPy missing: fall back to mean.
            return float(np.mean(list(self._buf)))
        arr = np.asarray(self._buf, dtype=float)
        smoothed = savgol_filter(arr, self.window_size, self.polyorder)
        return float(smoothed[-1])

    def reset(self) -> None:
        self._buf.clear()


class ButterworthFilter(BaseFilter):
    """Causal Butterworth IIR filter using second-order sections."""

    name = "butterworth"

    def __init__(
        self,
        cutoff_hz: float = 5.0,
        fs_hz: float = 50.0,
        order: int = 2,
        btype: str = "low",
    ) -> None:
        super().__init__()
        self.cutoff_hz = cutoff_hz
        self.fs_hz = fs_hz
        self.order = order
        self.btype = btype
        self.window_size = order
        self.parameters = {
            "cutoff_hz": cutoff_hz,
            "fs_hz": fs_hz,
            "order": order,
            "btype": btype,
        }
        self._sos = None
        self._zi = None
        if _SCIPY:
            nyq = fs_hz / 2.0
            wn = min(max(cutoff_hz / nyq, 1e-6), 0.999999)
            self._sos = butter(order, wn, btype=btype, output="sos")
            self._zi = sosfilt_zi(self._sos)
            self._initialized = False

    def process(self, value: float, ts: float = 0.0) -> float:
        if math.isnan(value):
            return value
        if not _SCIPY or self._sos is None:
            return value  # Pass-through when SciPy unavailable.
        if not self._initialized:
            self._zi = self._zi * value
            self._initialized = True
        out, self._zi = sosfilt(self._sos, [value], zi=self._zi)
        return float(out[-1])

    def reset(self) -> None:
        if _SCIPY and self._sos is not None:
            self._zi = sosfilt_zi(self._sos)
            self._initialized = False


class KalmanFilter1D(BaseFilter):
    """Scalar Kalman filter for a single noisy measurement stream."""

    name = "kalman_1d"

    def __init__(self, process_noise: float = 1e-3, measurement_noise: float = 1e-1) -> None:
        super().__init__()
        self.q = process_noise
        self.r = measurement_noise
        self._x: Optional[float] = None
        self._p = 1.0
        self.parameters = {"process_noise": process_noise, "measurement_noise": measurement_noise}

    def process(self, value: float, ts: float = 0.0) -> float:
        if math.isnan(value):
            return value
        if self._x is None:
            self._x = value
            self._p = 1.0
            return value
        # Predict.
        self._p += self.q
        # Update.
        k = self._p / (self._p + self.r)
        self._x = self._x + k * (value - self._x)
        self._p = (1.0 - k) * self._p
        return float(self._x)

    def reset(self) -> None:
        self._x = None
        self._p = 1.0


class ExtendedKalmanFilter(BaseFilter):
    """Constant-velocity EKF estimating altitude and vertical velocity.

    State x = [altitude, velocity]. ``process`` accepts an altitude
    measurement and a timestamp (seconds) and returns the filtered altitude.
    The estimated velocity is available via :attr:`velocity`.
    """

    name = "ekf_altitude"

    def __init__(self, process_noise: float = 1e-2, measurement_noise: float = 1.0) -> None:
        super().__init__()
        self.q = process_noise
        self.r = measurement_noise
        self._x = np.zeros(2)  # [alt, vel]
        self._p = np.eye(2)
        self._last_ts: Optional[float] = None
        self._initialized = False
        self.parameters = {"process_noise": process_noise, "measurement_noise": measurement_noise}

    @property
    def velocity(self) -> float:
        return float(self._x[1])

    def process(self, value: float, ts: float = 0.0) -> float:
        if math.isnan(value):
            return value
        if not self._initialized:
            self._x = np.array([value, 0.0])
            self._p = np.eye(2)
            self._last_ts = ts
            self._initialized = True
            return value
        dt = ts - self._last_ts if self._last_ts is not None else 0.1
        if dt <= 0:
            dt = 0.1
        self._last_ts = ts
        # State transition (constant velocity).
        f = np.array([[1.0, dt], [0.0, 1.0]])
        self._x = f @ self._x
        g = np.array([[0.5 * dt * dt], [dt]])
        q = (g @ g.T) * self.q
        self._p = f @ self._p @ f.T + q
        # Measurement update (observe altitude only).
        h = np.array([[1.0, 0.0]])
        y = value - (h @ self._x)[0]
        s = (h @ self._p @ h.T)[0, 0] + self.r
        k = (self._p @ h.T) / s
        self._x = self._x + (k.flatten() * y)
        self._p = (np.eye(2) - k @ h) @ self._p
        return float(self._x[0])

    def reset(self) -> None:
        self._x = np.zeros(2)
        self._p = np.eye(2)
        self._last_ts = None
        self._initialized = False


# Registry ------------------------------------------------------------------
_FILTER_FACTORIES = {
    "none": NoFilter,
    "moving_average": MovingAverageFilter,
    "ema": ExponentialMovingAverageFilter,
    "median": MedianFilter,
    "hampel": HampelFilter,
    "savitzky_golay": SavitzkyGolayFilter,
    "butterworth": ButterworthFilter,
    "kalman_1d": KalmanFilter1D,
    "ekf_altitude": ExtendedKalmanFilter,
}


class FilterRegistry:
    """Holds per-sensor filter instances configurable by name."""

    def __init__(self) -> None:
        self._by_sensor: Dict[str, BaseFilter] = {}

    @staticmethod
    def available() -> List[str]:
        return sorted(_FILTER_FACTORIES.keys())

    @staticmethod
    def create(name: str, **kwargs: Any) -> BaseFilter:
        if name not in _FILTER_FACTORIES:
            raise KeyError(f"Unknown filter '{name}'. Available: {FilterRegistry.available()}")
        return _FILTER_FACTORIES[name](**kwargs)

    def configure(self, sensor_id: str, name: str, **kwargs: Any) -> BaseFilter:
        flt = self.create(name, **kwargs)
        self._by_sensor[sensor_id] = flt
        return flt

    def get(self, sensor_id: str) -> BaseFilter:
        return self._by_sensor.get(sensor_id) or NoFilter()

    def has(self, sensor_id: str) -> bool:
        return sensor_id in self._by_sensor

    def reset_all(self) -> None:
        for flt in self._by_sensor.values():
            flt.reset()
