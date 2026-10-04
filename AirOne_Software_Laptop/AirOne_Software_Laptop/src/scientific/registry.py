"""Analysis registry: a catalogue of named scientific analyses.

Each analysis is a callable ``fn(series: SeriesBundle, **params) -> ScientificResult``.
The registry lets the API and the worker discover analyses, group them by
category, and run them by name. Registration is explicit — there is no magic
auto-discovery that could silently include a broken analysis.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .base import ScientificResult

logger = logging.getLogger(__name__)

AnalysisFn = Callable[..., ScientificResult]


@dataclass
class AnalysisSpec:
    name: str
    category: str
    fn: AnalysisFn
    description: str = ""
    required_fields: List[str] = field(default_factory=list)
    min_samples: int = 1


class AnalysisRegistry:
    """Holds analysis specs keyed by name."""

    def __init__(self) -> None:
        self._specs: Dict[str, AnalysisSpec] = {}

    def register(
        self,
        name: str,
        category: str,
        fn: AnalysisFn,
        description: str = "",
        required_fields: Optional[List[str]] = None,
        min_samples: int = 1,
    ) -> None:
        if name in self._specs:
            raise ValueError(f"Analysis '{name}' already registered")
        self._specs[name] = AnalysisSpec(
            name=name,
            category=category,
            fn=fn,
            description=description,
            required_fields=required_fields or [],
            min_samples=min_samples,
        )

    def has(self, name: str) -> bool:
        return name in self._specs

    def get(self, name: str) -> Optional[AnalysisSpec]:
        return self._specs.get(name)

    def names(self) -> List[str]:
        return sorted(self._specs)

    def categories(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for spec in self._specs.values():
            out.setdefault(spec.category, []).append(spec.name)
        for names in out.values():
            names.sort()
        return out

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": s.name,
                "category": s.category,
                "description": s.description,
                "required_fields": s.required_fields,
                "min_samples": s.min_samples,
            }
            for s in sorted(self._specs.values(), key=lambda s: (s.category, s.name))
        ]

    def run(self, name: str, series: Any, **params: Any) -> ScientificResult:
        spec = self._specs.get(name)
        if spec is None:
            return ScientificResult.insufficient(
                name, f"unknown analysis '{name}'", category="general"
            )
        try:
            return spec.fn(series, **params)
        except Exception as exc:  # noqa: BLE001 - never let one analysis crash others
            logger.exception("Analysis %s failed", name)
            return ScientificResult.insufficient(
                name, f"analysis raised {type(exc).__name__}: {exc}",
                category=spec.category,
            )


def build_default_registry() -> AnalysisRegistry:
    """Construct a registry populated with all built-in analyses.

    Imports are local to avoid a circular import at module load time.
    """

    reg = AnalysisRegistry()
    from . import (
        atmospheric,
        composite,
        descent,
        gases,
        gnss,
        magnetic,
        power,
        radiation,
        uv_solar,
    )

    for module in (
        atmospheric,
        radiation,
        gases,
        uv_solar,
        magnetic,
        gnss,
        descent,
        power,
        composite,
    ):
        module.register_all(reg)
    return reg
