"""Safe external process helpers with list-form arguments."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import ExternalToolError
from .supervisor import ProcessLimits, ProcessSupervisor


def run_checked(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 120.0,
    max_output_bytes: int = 1024 * 1024,
) -> subprocess.CompletedProcess[str]:
    supervised = ProcessSupervisor(ProcessLimits(
        wall_timeout=timeout,
        no_progress_timeout=timeout,
        max_output_bytes=max_output_bytes,
    )).run(arguments, cwd=cwd)
    result = subprocess.CompletedProcess(arguments, supervised.returncode, supervised.stdout, supervised.stderr)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise ExternalToolError(
            f"{arguments[0]} exited with status {result.returncode}: {detail}",
            code="external_tool_failed",
        )
    return result
