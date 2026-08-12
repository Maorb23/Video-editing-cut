"""Safe external process helpers with list-form arguments."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

from .errors import ExternalToolError


def run_checked(arguments: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments, cwd=cwd, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except OSError as exc:
        raise ExternalToolError(f"cannot execute {arguments[0]}: {exc}", code="tool_unavailable") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise ExternalToolError(
            f"{arguments[0]} exited with status {result.returncode}: {detail}",
            code="external_tool_failed",
        )
    return result

