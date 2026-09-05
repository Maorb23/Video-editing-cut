from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..supervisor import ProcessSupervisor, Toolchain
from ..workspace import JobWorkspace


@dataclass(frozen=True)
class AnalysisArtifact:
    data: dict[str, Any]
    path: Path
    frame_paths: tuple[Path, ...]


class AnalysisProvider(Protocol):
    @property
    def version(self) -> str: ...

    def analyze(
        self,
        source: Path,
        workspace: JobWorkspace,
        toolchain: Toolchain,
        supervisor: ProcessSupervisor,
    ) -> AnalysisArtifact: ...

