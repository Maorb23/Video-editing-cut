"""Curated immutable audio preprocessing stages."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from .errors import VideoEditingError
from .process import run_checked


DEREVERB_DEPENDENCY = "deepfilternet==0.5.6"
DEREVERB_MODEL = "deepfilternet3-local"


def dereverb_preflight(*, executable: str | None = None) -> dict[str, Any]:
    binary = executable or shutil.which("deepFilter") or shutil.which("deep-filter")
    return {"available": binary is not None, "executable": binary, "dependency": DEREVERB_DEPENDENCY,
            "model": DEREVERB_MODEL}


def create_dereverberated_asset(source: Path, destination: Path, *, model_path: Path,
                                model_sha256: str, executable: str | None = None) -> Path:
    """Run the pinned local model and atomically publish a new audio asset."""
    state = dereverb_preflight(executable=executable)
    if not state["available"]:
        raise VideoEditingError(f"dereverberation requires optional {DEREVERB_DEPENDENCY}", code="dereverb_unavailable")
    if destination.exists():
        raise VideoEditingError(f"refusing to overwrite derived audio: {destination}", code="output_exists")
    actual = "sha256:" + hashlib.sha256(model_path.read_bytes()).hexdigest()
    if actual != model_sha256:
        raise VideoEditingError("dereverberation model fingerprint mismatch", code="fingerprint_mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial.wav")
    try:
        run_checked([str(state["executable"]), "--model-base-dir", str(model_path.parent),
                     "--output-dir", str(temporary.parent), str(source)], timeout=3600)
        produced = temporary.parent / source.with_suffix(".wav").name
        if produced != temporary and produced.is_file():
            os.replace(produced, temporary)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise VideoEditingError("dereverberation produced no audio", code="derived_asset_missing")
        os.link(temporary, destination)
        temporary.unlink()
    finally:
        if temporary.exists(): temporary.unlink()
    return destination
