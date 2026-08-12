"""Cross-platform discovery of the external video toolchain."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _shotcut_candidates() -> list[Path]:
    system = platform.system()
    candidates: list[Path] = []
    if system == "Windows":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base:
                candidates.extend([Path(base) / "Shotcut" / "melt.exe", Path(base) / "Shotcut" / "melt-7.exe"])
    elif system == "Darwin":
        candidates.extend([Path("/Applications/Shotcut.app/Contents/MacOS/melt"), Path.home() / "Applications/Shotcut.app/Contents/MacOS/melt"])
    else:
        candidates.extend([Path("/usr/bin/melt-7"), Path("/usr/local/bin/melt-7"), Path("/snap/shotcut/current/usr/bin/melt")])
    return candidates


def _find(names: list[str], candidates: list[Path] | None = None) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
    for candidate in candidates or []:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _version(path: str | None) -> str | None:
    if path is None:
        return None
    for flag in ("--version", "-version"):
        try:
            result = subprocess.run([path, flag], text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = result.stdout.strip().splitlines()
        if output:
            return output[0]
    return None


def check_environment() -> dict[str, Any]:
    melt = _find(["melt", "melt-7", "melt.exe"], _shotcut_candidates())
    ffmpeg = _find(["ffmpeg", "ffmpeg.exe"])
    ffprobe = _find(["ffprobe", "ffprobe.exe"])
    tools = {
        "melt": {"path": melt, "version": _version(melt), "required_for": ["compile validation", "render"]},
        "ffmpeg": {"path": ffmpeg, "version": _version(ffmpeg), "required_for": ["inspection frames", "audio analysis"]},
        "ffprobe": {"path": ffprobe, "version": _version(ffprobe), "required_for": ["media probing", "inspection metadata"]},
    }
    return {
        "ok": all(tools[name]["path"] for name in ("melt", "ffmpeg", "ffprobe")),
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "tools": tools,
        "guidance": "Install Shotcut/MLT and FFmpeg using the platform vendor; this project never installs them automatically.",
    }

