"""Curated immutable audio preprocessing stages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from .errors import Issue, PlanValidationError, VideoEditingError
from .process import run_checked
from .probe import fingerprint
from .dereverb_pin import MODEL, MODEL_SHA256, EXECUTABLE_SHA256, VERSION

if TYPE_CHECKING:
    from .supervisor import Toolchain
    from .workspace import JobWorkspace


DEREVERB_DEPENDENCY = "deep-filter==0.5.6 (CPU Tract)"
DEREVERB_MODEL = "deepfilternet3-local"


def wants_dereverb(instruction: str) -> bool:
    text = re.sub(r"\b(?:do not|don't|never)\s+(?:remove|reduce|clean|dereverberate)[^.;!?]*", "", instruction.lower())
    return bool(re.search(r"\bdereverberat(?:e|ion|ing)\b|\b(?:remove|reduce|clean(?: up)?)\s+(?:(?:the|room|audio|background)\s+)*(?:echo|reverb(?:eration)?)\b", text))


def dereverb_preflight(*, executable: str | None = None, model_path: Path | None = None,
                      model_sha256: str = MODEL_SHA256,
                      executable_sha256: str = EXECUTABLE_SHA256) -> dict[str, Any]:
    binary = executable or os.environ.get("VIDEO_EDIT_DEREVERB_EXECUTABLE", "/opt/dereverb/deep-filter")
    model = model_path or Path(os.environ.get("VIDEO_EDIT_DEREVERB_MODEL", "/opt/dereverb/model.tar.gz"))
    found = shutil.which(binary)
    if not found or not model.is_file() or not model_sha256 or not executable_sha256:
        raise VideoEditingError("pinned CPU dereverb executable, model and fingerprints are required", code="dereverb_unavailable")
    try:
        actual_model, actual_executable = fingerprint(model), fingerprint(Path(found))
    except OSError as exc:
        raise VideoEditingError("dereverb model or executable cannot be read", code="dereverb_unavailable") from exc
    if actual_model != model_sha256 or actual_executable != executable_sha256:
        raise VideoEditingError("dereverb model or executable fingerprint mismatch", code="fingerprint_mismatch")
    try:
        version = run_checked([found, "--version"], timeout=10, max_output_bytes=4096).stdout.strip()
    except (OSError, VideoEditingError) as exc:
        raise VideoEditingError("pinned dereverb executable could not start", code="dereverb_unavailable") from exc
    if version != VERSION:
        raise VideoEditingError("dereverb executable version mismatch", code="fingerprint_mismatch")
    return {"available": True, "executable": found, "executable_version": version,
            "executable_sha256": executable_sha256, "model_path": str(model),
            "model": MODEL, "model_sha256": model_sha256, "device": "cpu"}


def wav_metadata(path: Path) -> dict[str, int]:
    try:
        with wave.open(str(path), "rb") as audio:
            metadata = {"sample_rate": audio.getframerate(), "channels": audio.getnchannels(),
                        "samples": audio.getnframes(), "sample_width": audio.getsampwidth()}
            if not metadata["samples"]:
                raise ValueError("empty audio")
            actual = 0
            while chunk := audio.readframes(65536):
                actual += len(chunk)
            if actual != metadata["samples"] * metadata["channels"] * metadata["sample_width"]:
                raise ValueError("truncated audio")
            return metadata
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        raise VideoEditingError("dereverb produced missing, empty or invalid PCM audio", code="derived_asset_missing") from exc


def validate_audio_metadata(actual: dict, expected: dict) -> None:
    issues = [Issue(f"audio_{field}_mismatch", f"$.audio.{field}", f"expected {expected[field]}, got {actual.get(field)}")
              for field in ("sample_rate", "channels", "samples") if actual.get(field) != expected[field]]
    if issues:
        raise PlanValidationError(issues)


def _pcm_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with wave.open(str(path), "rb") as audio:
        while chunk := audio.readframes(65536):
            digest.update(chunk)
    return digest.hexdigest()


def create_dereverberated_asset(source: Path, destination: Path, *, model_path: Path | None = None,
                                model_sha256: str = MODEL_SHA256, executable: str | None = None,
                                executable_sha256: str = EXECUTABLE_SHA256, ffmpeg: str = "ffmpeg",
                                timeout: float = 3600, max_diagnostic_bytes: int = 65536) -> Path:
    """Atomically publish a new directory containing the WAV and manifest.

    The destination parent must not exist and must be allocated inside the job.
    Explicit pin arguments allow independently tested future adapters and fixtures.
    """
    state = dereverb_preflight(executable=executable, model_path=model_path,
                               model_sha256=model_sha256, executable_sha256=executable_sha256)
    if destination.parent.exists() or destination.resolve() == source.resolve():
        raise VideoEditingError("refusing to overwrite a derived asset directory", code="output_exists")
    destination.parent.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    input_hash = fingerprint(source)
    def run(args: list[str]) -> None:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise VideoEditingError("CPU dereverb processing timed out", code="process_timeout")
        run_checked(args, timeout=remaining, max_output_bytes=max_diagnostic_bytes)
    with tempfile.TemporaryDirectory(prefix=".dereverb-", dir=destination.parent.parent) as temporary:
        root = Path(temporary)
        extracted = root / "source.wav"
        run([ffmpeg, "-v", "error", "-nostdin", "-n", "-copyts", "-start_at_zero", "-i", str(source),
             "-map", "0:a:0", "-vn", "-af", "aresample=async=1:first_pts=0", "-c:a", "pcm_s16le", str(extracted)])
        metadata = wav_metadata(extracted)
        prepared = root / "input.wav"
        run([ffmpeg, "-v", "error", "-nostdin", "-n", "-i", str(extracted), "-af",
             "aresample=48000,apad=pad_len=1920", "-c:a", "pcm_s16le", str(prepared)])
        processing = wav_metadata(prepared)
        output_dir = root / "processed"
        run([state["executable"], "--model", state["model_path"], "--compensate-delay",
             "--output-dir", str(output_dir), str(prepared)])
        raw = output_dir / prepared.name
        checked = root / "checked.wav"
        if not raw.is_file() or raw.stat().st_size <= 44:
            raise VideoEditingError("dereverb produced no audio", code="derived_asset_missing")
        try:
            run([ffmpeg, "-v", "error", "-nostdin", "-n", "-i", str(raw), "-c:a", "pcm_s16le", str(checked)])
        except VideoEditingError as exc:
            if exc.code == "external_tool_failed":
                raise VideoEditingError("dereverb output is invalid audio", code="derived_asset_missing") from exc
            raise
        validate_audio_metadata(wav_metadata(checked), {**processing, "samples": processing["samples"] - 1440})
        publish = root / "publish"
        publish.mkdir()
        normalized = publish / destination.name
        run([ffmpeg, "-v", "error", "-nostdin", "-n", "-i", str(checked), "-af",
             f"aresample={metadata['sample_rate']},atrim=end_sample={metadata['samples']}",
             "-c:a", "pcm_s16le", str(normalized)])
        validate_audio_metadata(wav_metadata(normalized), metadata)
        output_hash = fingerprint(normalized)
        if _pcm_fingerprint(normalized) == _pcm_fingerprint(extracted):
            raise VideoEditingError("dereverb returned unchanged audio", code="dereverb_unchanged")
        if (fingerprint(source) != input_hash or fingerprint(Path(state["model_path"])) != model_sha256
                or fingerprint(Path(state["executable"])) != executable_sha256):
            raise VideoEditingError("dereverb input fingerprint changed during processing", code="fingerprint_mismatch")
        manifest = {"version": "1.0", "input_sha256": input_hash, "output_sha256": output_hash,
                    "extracted_audio_sha256": fingerprint(extracted), "model_output_sha256": fingerprint(raw),
                    **{key: state[key] for key in ("model", "model_sha256", "executable_version", "executable_sha256", "device")},
                    "executable": Path(state["executable"]).name, "audio": metadata,
                    "processing_seconds": round(time.monotonic() - started, 6), "delay_samples_48000": 1440}
        (publish / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        for path in publish.iterdir():
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
            path.chmod(0o444)
        if destination.parent.exists():
            raise VideoEditingError("derived asset directory already exists", code="output_exists")
        publish.rename(destination.parent)
    return destination


def prepare_dereverb(instruction: str, source: Path, workspace: JobWorkspace, tools: Toolchain, *,
                     previous_plan: dict | None = None, progress: Callable[[str], None] | None = None,
                     timeout: float = 3600, max_diagnostic_bytes: int = 65536) -> dict | None:
    prior = any(op.get("type") == "dereverb" and op.get("enabled", True)
                for op in (previous_plan or {}).get("operations", []))
    if not wants_dereverb(instruction) and not prior:
        return None
    if progress:
        progress("Cleaning room echo")
    destination = workspace.path("derived/dereverb/audio.wav")
    create_dereverberated_asset(source, destination, ffmpeg=str(tools.ffmpeg),
                               timeout=min(timeout, 3600), max_diagnostic_bytes=min(max_diagnostic_bytes, 65536))
    manifest = json.loads(destination.with_name("manifest.json").read_text(encoding="utf-8"))
    return {"asset_path": destination.relative_to(workspace.root).as_posix(), "manifest": manifest}
