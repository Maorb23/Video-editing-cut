"""Deterministic MLT rendering with structured progress parsing."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, TextIO

from .errors import ExternalToolError, VideoEditingError
from .supervisor import CancellationToken, ProcessLimits, ProcessSupervisor


PROGRESS_RE = re.compile(r"(?:percentage|percent|progress)\D+(\d{1,3})", re.IGNORECASE)
PRESETS = {
    "preview": {"vcodec": "libx264", "crf": "28", "preset": "veryfast", "acodec": "aac", "ab": "128k", "pix_fmt": "yuv420p"},
    "final": {"vcodec": "libx264", "crf": "18", "preset": "medium", "acodec": "aac", "ab": "192k", "pix_fmt": "yuv420p"},
}


def _external_path(path: Path, binary: str) -> str:
    """Translate /mnt/<drive> paths embedded in Windows-program arguments."""
    resolved = path.resolve()
    parts = resolved.parts
    if binary.lower().endswith(".exe") and len(parts) >= 4 and parts[1] == "mnt" and len(parts[2]) == 1:
        return f"{parts[2].upper()}:\\" + "\\".join(parts[3:])
    return str(resolved)


def _project_metadata(project: Path) -> tuple[dict[str, str], dict[str, str]]:
    try:
        root = ET.parse(project).getroot()
    except (OSError, ET.ParseError) as exc:
        raise VideoEditingError(f"cannot read MLT project export metadata: {exc}", code="invalid_project") from exc
    export_prefix = "video-editing-skill:export."
    profile_prefix = "video-editing-skill:profile."
    properties = root.findall("./tractor/property")
    export = {
        str(item.get("name"))[len(export_prefix) :]: item.text or ""
        for item in properties if str(item.get("name", "")).startswith(export_prefix)
    }
    profile = {
        str(item.get("name"))[len(profile_prefix) :]: item.text or ""
        for item in properties if str(item.get("name", "")).startswith(profile_prefix)
    }
    return export, profile


def parse_progress(line: str) -> int | None:
    match = PROGRESS_RE.search(line)
    if not match:
        return None
    return min(100, int(match.group(1)))


def verify_filter_services(project: Path, binary: str, runner: ProcessSupervisor) -> None:
    """Require compiled filters to match an explicitly allow-listed runtime ABI."""
    verified_services: set[tuple[str, tuple[str, ...]]] = set()
    for node in ET.parse(project).getroot().findall('./producer/filter'):
        pin = node.find("./property[@name='video-editing-skill:service-version']")
        if pin is None:
            continue
        service = node.find("./property[@name='mlt_service']").text
        compatible = node.find("./property[@name='video-editing-skill:compatible-service-abis']")
        accepted = tuple(dict.fromkeys(
            value for value in (compatible.text.split(',') if compatible is not None and compatible.text else [pin.text])
            if value
        ))
        signature = (service, accepted)
        if signature in verified_services:
            continue
        query = runner.run([binary, '-query', f'filter={service}'])
        metadata = query.stdout + query.stderr
        reported = re.findall(r'^\s*version:\s*(\S+)\s*$', metadata, re.MULTILINE)
        if query.returncode or not any(
            version.startswith(abi) for version in reported for abi in accepted
        ):
            actual = ', '.join(reported) if reported else 'unavailable'
            raise VideoEditingError(
                f'{service} requires compatible ABI {", ".join(accepted)}; reported {actual}',
                code='unsupported_filter_version',
            )
        verified_services.add(signature)


def render(
    project: Path,
    output: Path,
    *,
    quality: str = "final",
    melt: str | None = None,
    progress_stream: TextIO = sys.stdout,
    export: dict[str, Any] | None = None,
    supervisor: ProcessSupervisor | None = None,
    timeout: float = 7200.0,
    no_progress_timeout: float = 180.0,
    cancellation: CancellationToken | None = None,
) -> None:
    if not project.is_file():
        raise VideoEditingError(f"MLT project not found: {project}", code="missing_file")
    if output.exists():
        raise VideoEditingError(f"refusing to overwrite existing render: {output}", code="output_exists")
    binary = melt or shutil.which("melt") or shutil.which("melt-7") or shutil.which("melt.exe")
    if not binary:
        raise VideoEditingError("melt is required; run check_environment.py for guidance", code="tool_unavailable")
    if quality not in PRESETS:
        raise VideoEditingError(f"unknown render quality: {quality}", code="invalid_quality")
    preset = dict(PRESETS[quality])
    compiled_export, compiled_profile = _project_metadata(project)
    settings = export if export is not None else compiled_export
    if settings.get("video_bitrate"):
        preset.pop("crf", None)
        preset["vb"] = str(settings["video_bitrate"])
    if settings.get("audio_bitrate"):
        preset["ab"] = str(settings["audio_bitrate"])
    preset["vcodec"] = str(settings.get("video_codec", preset["vcodec"]))
    preset["acodec"] = str(settings.get("audio_codec", preset["acodec"]))
    preset["pix_fmt"] = str(settings.get("pixel_format", preset["pix_fmt"]))
    movflags = str(settings.get("movflags", "+faststart"))
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial.mp4")
    arguments = [binary, _external_path(project, binary), "-progress", "-consumer", f"avformat:{_external_path(temporary, binary)}"]
    arguments.extend(f"{name}={value}" for name, value in preset.items())
    if compiled_profile.get("sample_rate"):
        arguments.append(f"frequency={compiled_profile['sample_rate']}")
    if compiled_profile.get("channels"):
        arguments.append(f"channels={compiled_profile['channels']}")
    arguments.extend(["f=mp4", f"movflags={movflags}"])
    output.parent.mkdir(parents=True, exist_ok=True)

    def progress(line: str) -> None:
        percent = parse_progress(line)
        if percent is not None:
            print(json.dumps({"event": "progress", "percent": percent}), file=progress_stream, flush=True)

    runner = supervisor or ProcessSupervisor(
        ProcessLimits(
            wall_timeout=timeout,
            no_progress_timeout=no_progress_timeout,
            max_output_bytes=256 * 1024,
        ),
        cancellation,
    )
    # Fail before rendering instead of silently accepting an unavailable hue filter.
    verify_filter_services(project, binary, runner)
    try:
        result = runner.run(
            arguments,
            cwd=project.resolve().parent,
            merge_stderr=True,
            on_line=progress,
            progress_predicate=lambda line: parse_progress(line) is not None,
            popen_factory=subprocess.Popen,
        )
        if result.returncode:
            detail = result.stdout.strip() or "no diagnostic output"
            raise ExternalToolError(f"melt exited with status {result.returncode}:\n{detail}", code="render_failed")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ExternalToolError("melt reported success but produced no output", code="render_missing_output")
        try:
            # link(2) is an atomic no-overwrite publish inside the output directory.
            # Unlike os.replace(), it cannot race into overwriting a user artifact.
            os.link(temporary, output)
        except FileExistsError as exc:
            raise VideoEditingError(f"refusing to overwrite existing render: {output}", code="output_exists") from exc
        temporary.unlink()
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    print(json.dumps({"event": "complete", "output": str(output)}), file=progress_stream, flush=True)
