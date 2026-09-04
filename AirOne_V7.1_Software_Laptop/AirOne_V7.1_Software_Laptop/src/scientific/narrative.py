"""Mission narrative generation for the judge / presentation mode.

Turns a set of :class:`ScientificResult` objects into a structured, honest,
human-readable narrative. The narrative never overstates confidence: invalid
results are reported as UNAVAILABLE with their reason, correlations are labelled
as associations, and every quantitative claim carries its uncertainty.
"""
from __future__ import annotations

from typing import Dict, List

from .base import ResultType, ScientificResult


def _fmt(result: ScientificResult) -> str:
    return result.summary()


def build_narrative(
    results: Dict[str, ScientificResult],
    mission_state: str = "UNKNOWN",
    mission_id: str = "default",
) -> Dict[str, object]:
    """Return a structured narrative dict with the judge-mode sections.

    Sections follow the 2-minute mission-understanding layout:
    QUESTION / MEASUREMENTS / RESULT / EVIDENCE / INTERPRETATION /
    CONFIDENCE / LIMITATIONS.
    """

    valid = {k: r for k, r in results.items() if r.valid and r.value is not None}
    invalid = {k: r for k, r in results.items() if not (r.valid and r.value is not None)}

    measurements: List[str] = [_fmt(r) for r in valid.values() if r.result_type in (ResultType.MEASURED, ResultType.DERIVED)]
    interpretations: List[str] = [_fmt(r) for r in valid.values() if r.result_type in (ResultType.INFERRED, ResultType.MODELLED, ResultType.PREDICTED)]
    correlations: List[str] = [_fmt(r) for r in valid.values() if r.result_type == ResultType.CORRELATION]

    # Aggregate limitations (deduplicated, order-preserving).
    limitations: List[str] = []
    for r in results.values():
        for lim in r.limitations:
            if lim not in limitations:
                limitations.append(lim)

    # Confidence summary: mean confidence of valid quantitative results.
    conf_values = [r.confidence for r in valid.values()]
    mean_conf = sum(conf_values) / len(conf_values) if conf_values else 0.0

    result_headline = _headline(valid)

    return {
        "mission_id": mission_id,
        "mission_state": mission_state,
        "question": (
            "What are the atmospheric, radiation and environmental conditions "
            "through the CanSat's ascent and descent, and what can we reliably "
            "conclude from them?"
        ),
        "measurements": measurements or ["No fully-valid direct measurements in this window."],
        "result": result_headline,
        "evidence": [
            f"{r.analysis_name}: method={r.method}; n={r.n_samples}"
            for r in valid.values()
        ] or ["No supporting evidence available yet."],
        "interpretation": interpretations or ["No model-based interpretations available yet."],
        "correlations": correlations or ["No correlations computed (need >=3 concurrent samples)."],
        "confidence": {
            "mean_confidence": round(mean_conf, 3),
            "valid_results": len(valid),
            "unavailable_results": len(invalid),
        },
        "limitations": limitations or ["None recorded."],
        "unavailable": {k: (r.limitations[0] if r.limitations else "unavailable") for k, r in invalid.items()},
        "disclaimer": (
            "All correlations describe association only and do not imply "
            "causation. Modelled and predicted values depend on the stated "
            "assumptions. Unavailable results are shown explicitly rather than "
            "substituted with fabricated values."
        ),
    }


def _headline(valid: Dict[str, ScientificResult]) -> str:
    """Pick a concise headline from the most decision-relevant results."""

    priority = [
        "descent_rate", "terminal_velocity", "atmospheric_stability",
        "temperature_inversion", "environmental_hazard_index", "uv_index",
    ]
    for key in priority:
        if key in valid:
            return valid[key].summary()
    if valid:
        return next(iter(valid.values())).summary()
    return "Insufficient valid data to state a headline result."
