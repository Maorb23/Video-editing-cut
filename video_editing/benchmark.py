"""Small, provider-neutral instruction-following benchmark harness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .analysis import AnalysisArtifact
from .errors import VideoEditingError
from .planning import EditPlanner, StructuredModel


@dataclass(frozen=True)
class BenchmarkResult:
    passed: int
    total: int
    cases: tuple[dict[str, Any], ...]

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total else 0.0


def load_benchmark(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VideoEditingError(f"invalid benchmark JSON on line {line_number}: {exc}", code="invalid_benchmark") from exc
        if (
            not isinstance(case, dict)
            or set(case) != {"id", "instruction", "expected_operations"}
            or not isinstance(case["id"], str)
            or not isinstance(case["instruction"], str)
            or not isinstance(case["expected_operations"], list)
            or not all(isinstance(item, str) for item in case["expected_operations"])
            or case["id"] in identifiers
        ):
            raise VideoEditingError(f"invalid benchmark case on line {line_number}", code="invalid_benchmark")
        identifiers.add(case["id"])
        cases.append(case)
    if not cases:
        raise VideoEditingError("benchmark contains no cases", code="invalid_benchmark")
    return cases


def run_instruction_benchmark(
    model: StructuredModel,
    manifest: Path,
    analysis: AnalysisArtifact,
    *,
    plan_directory: Path,
    source_relative: str,
) -> BenchmarkResult:
    results: list[dict[str, Any]] = []
    planner = EditPlanner(model, max_repair_attempts=0)
    for case in load_benchmark(manifest):
        planned = planner.plan(
            case["instruction"], analysis,
            plan_path=plan_directory / f"{case['id']}.json",
            source_relative=source_relative,
        )
        actual = [
            operation["type"] for operation in planned.plan.data["operations"]
            if operation.get("enabled", True)
        ]
        passed = actual == case["expected_operations"]
        results.append({"id": case["id"], "passed": passed, "expected_operations": case["expected_operations"], "actual_operations": actual})
    return BenchmarkResult(sum(1 for item in results if item["passed"]), len(results), tuple(results))

