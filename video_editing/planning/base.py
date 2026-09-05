from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..plan import ValidatedPlan


@dataclass(frozen=True)
class ModelResponse:
    data: dict[str, Any]
    provenance: dict[str, Any]


class StructuredModel(Protocol):
    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
        images: tuple[Path, ...] = (),
    ) -> ModelResponse: ...


@dataclass(frozen=True)
class PlanResult:
    plan: ValidatedPlan
    summary: str
    attempts: tuple[dict[str, Any], ...]
    decision_log: dict[str, Any] = field(default_factory=dict)
