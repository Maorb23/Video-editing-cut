"""Deterministic MLT rendering with structured progress parsing."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO

from .errors import ExternalToolError, VideoEditingError


PROGRESS_RE = re.compile(r"(?:percentage|percent|progress)\D+(\d{1,3})", re.IGNORECASE)
PRESETS = {
    "preview": {"vcodec": "libx264", "crf": "28", "preset": "veryfast", "acodec": "aac", "ab": "128k", "pix_fmt": "yuv420p"},
    "final": {"vcodec": "libx264", "crf": "18", "preset": "medium", "acodec": "aac", "ab": "192k", "pix_fmt": "yuv420p"},
}


def parse_progress(line: str) -> int | None:
    match = PROGRESS_RE.search(line)
    if not match:
        return None
    return min(100, int(match.group(1)))


def render(project: Path, output: Path, *, quality: str = "final", melt: str | None = None, progress_stream: TextIO = sys.stdout) -> None:
    if not project.is_file():
        raise VideoEditingError(f"MLT project not found: {project}", code="missing_file")
    if output.exists():
        raise VideoEditingError(f"refusing to overwrite existing render: {output}", code="output_exists")
    binary = melt or shutil.which("melt") or shutil.which("melt-7") or shutil.which("melt.exe")
    if not binary:
        raise VideoEditingError("melt is required; run check_environment.py for guidance", code="tool_unavailable")
    preset = PRESETS[quality]
    temporary = output.with_name(f".{output.name}.partial.mp4")
    arguments = [binary, str(project), "-progress", "-consumer", f"avformat:{temporary}"]
    arguments.extend(f"{name}={value}" for name, value in preset.items())
    arguments.extend(["f=mp4", "movflags=+faststart"])
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExternalToolError(f"cannot execute melt: {exc}", code="tool_unavailable") from exc
    diagnostics: list[str] = []
    assert process.stdout is not None
    try:
        for line in process.stdout:
            diagnostics.append(line.rstrip())
            percent = parse_progress(line)
            if percent is not None:
                print(json.dumps({"event": "progress", "percent": percent}), file=progress_stream, flush=True)
        return_code = process.wait()
    except BaseException:
        process.terminate()
        process.wait()
        if temporary.exists():
            temporary.unlink()
        raise
    if return_code:
        if temporary.exists():
            temporary.unlink()
        detail = "\n".join(diagnostics[-30:])
        raise ExternalToolError(f"melt exited with status {return_code}:\n{detail}", code="render_failed")
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise ExternalToolError("melt reported success but produced no output", code="render_missing_output")
    os.replace(temporary, output)
    print(json.dumps({"event": "complete", "output": str(output)}), file=progress_stream, flush=True)
