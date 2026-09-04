"""Foundational types for the AirOne V7.1 scientific analysis engine.

Every scientific output is a :class:`ScientificResult`. It carries not only a
value but the *epistemic* metadata that makes the result trustworthy to a
judge or a scientist: the method used, the inputs, the assumptions, the
quantified uncertainty, a confidence estimate, and explicit limitations.

Two hard rules are encoded structurally:

1. **Correlation is not causation.** Any analysis that measures association
   between variables must use :data:`ResultType.CORRELATION` and *will* have a
   causation caveat injected automatically (see :meth:`ScientificResult.finalize`).
2. **Never fabricate.** When the input data are insufficient or invalid, an
   analysis returns ``ScientificResult.insufficient(...)`` — a result with
   ``valid=False`` and ``value=None``. It never guesses a plausible number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from ..core.models import utcnow


class ResultType(str, Enum):
    """The epistemic category of a scientific result.

    The ordering here reflects decreasing directness of evidence:

    * ``MEASURED``   - value read (nearly) directly from a sensor.
    * ``DERIVED``    - deterministic physics from measured values.
    * ``MODELLED``   - output of a physical/empirical model with assumptions.
    * ``PREDICTED``  - extrapolation forward in time.
    * ``INFERRED``   - conclusion drawn indirectly from several signals.
    * ``CORRELATION``- statistical association ONLY (never causal).
    * ``OBSERVATION``- qualitative note / detected event.
    """

    MEASURED = "MEASURED"
    DERIVED = "DERIVED"
    MODELLED = "MODELLED"
    PREDICTED = "PREDICTED"
    INFERRED = "INFERRED"
    CORRELATION = "CORRELATION"
    OBSERVATION = "OBSERVATION"


_CAUSATION_CAVEAT = "Correlation does not imply causation."


@dataclass
class ScientificResult:
    """A single, fully-provenanced scientific result."""

    analysis_name: str
    value: Optional[Union[float, int, str, List[Any], Dict[str, Any]]]
    unit: str = ""
    method: str = ""
    result_type: ResultType = ResultType.DERIVED
    inputs: Dict[str, Any] = field(default_factory=dict)
    assumptions: List[str] = field(default_factory=list)
    uncertainty: Optional[float] = None
    confidence: float = 0.0
    limitations: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=utcnow)
    valid: bool = True
    n_samples: int = 0
    category: str = "general"
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A correlation result MUST carry the causation caveat.
        if self.result_type == ResultType.CORRELATION:
            if _CAUSATION_CAVEAT not in self.limitations:
                self.limitations.append(_CAUSATION_CAVEAT)
        # Confidence is a probability-like number in [0, 1].
        if self.confidence < 0.0:
            self.confidence = 0.0
        elif self.confidence > 1.0:
            self.confidence = 1.0

    # -- constructors for degraded states --------------------------------
    @classmethod
    def insufficient(
        cls,
        analysis_name: str,
        reason: str,
        n_samples: int = 0,
        category: str = "general",
        method: str = "",
    ) -> "ScientificResult":
        """Return an explicitly invalid result. NEVER fabricates a value."""

        return cls(
            analysis_name=analysis_name,
            value=None,
            method=method,
            result_type=ResultType.OBSERVATION,
            uncertainty=None,
            confidence=0.0,
            limitations=[f"Insufficient/invalid data: {reason}"],
            valid=False,
            n_samples=n_samples,
            category=category,
        )

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d["result_type"] = self.result_type.value
        # Sanitise non-finite floats so JSON stays valid.
        if isinstance(self.value, float) and not math.isfinite(self.value):
            d["value"] = None
        if isinstance(self.uncertainty, float) and not math.isfinite(self.uncertainty):
            d["uncertainty"] = None
        return d

    def summary(self) -> str:
        """One-line human summary suitable for a log or narrative."""

        if not self.valid or self.value is None:
            reason = self.limitations[0] if self.limitations else "no data"
            return f"{self.analysis_name}: UNAVAILABLE ({reason})"
        val = self.value
        if isinstance(val, float):
            val = f"{val:.4g}"
        unc = ""
        if self.uncertainty is not None and math.isfinite(self.uncertainty):
            unc = f" ± {self.uncertainty:.3g}"
        unit = f" {self.unit}" if self.unit else ""
        return f"{self.analysis_name}: {val}{unc}{unit} [{self.result_type.value}]"
