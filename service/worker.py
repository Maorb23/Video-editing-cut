from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from video_editing.plan import read_json
from video_editing.plan import validate_plan
from video_editing.mlt import write_mlt
from video_editing.artifacts import validate_compiled_mlt
from video_editing.inspect import inspect
from video_editing.errors import VideoEditingError
from video_editing.workspace import JobWorkspace
from video_editing.audio import dereverb_preflight
from video_editing.adaptive_silence import SilenceSettings
from video_editing.supervisor import Toolchain

from .config import Settings
from .engine import prepare_edit, render_prepared_edit
from .observability import emit
from .reliability import classify_retry
from .repository import ClaimedJob, ConflictError, PostgresRepository, canonical_digest
from .storage import Storage, build_storage


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


class Worker:
    def __init__(self, repository: PostgresRepository, storage: Storage, settings: Settings, *, worker_id: str | None = None, toolchain: Toolchain | None = None) -> None:
        self.repository = repository
        self.storage = storage
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.toolchain = toolchain
        self.settings.work_root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _heartbeats(self, job: ClaimedJob) -> Iterator[None]:
        stopped = threading.Event()
        interval = max(10.0, self.settings.worker_lease_seconds / 3)

        def beat() -> None:
            while not stopped.wait(interval):
                if not self.repository.heartbeat(job.id, self.worker_id, self.settings.worker_lease_seconds):
                    return

        thread = threading.Thread(target=beat, name=f"heartbeat-{job.id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)

    def run_once(self) -> bool:
        exhausted = self.repository.reconcile_exhausted(self.settings.worker_max_attempts)
        if exhausted:
            emit("jobs_reconciled", worker_id=self.worker_id, count=exhausted)
        job = self.repository.claim(self.worker_id, self.settings.worker_lease_seconds, self.settings.worker_max_attempts)
        if job is None:
            return False
        emit("job_started", job_id=job.id, edit_id=job.edit_id, iteration=job.iteration, kind=job.kind, attempt=job.attempts, worker_id=self.worker_id)
        started = time.monotonic()
        try:
            with self._heartbeats(job):
                if job.kind in {"plan", "revision"}:
                    self._plan(job)
                elif job.kind == "compilation":
                    self._compile(job)
                elif job.kind == "preview":
                    self._preview(job)
                elif job.kind == "inspection":
                    self._inspect(job)
                elif job.kind == "render":
                    self._render(job)
                else:
                    raise RuntimeError(f"unknown job kind: {job.kind}")
        except BaseException as exc:
            code = getattr(exc, "code", "worker_failed")
            decision = classify_retry(code, job.attempts, self.settings.worker_max_attempts)
            error = {
                "code": code,
                "stage": job.kind,
                "message": str(exc)[:4096],
                "retryable": decision.retryable,
                "retry_reason": decision.reason,
                "details": {"issues": [issue.as_dict() for issue in getattr(exc, "issues", [])]},
            }
            retried = decision.retryable and self.repository.retry(job, error)
            if not retried:
                self.repository.fail(job, error)
            emit(
                "job_retrying" if retried else "job_failed", job_id=job.id, edit_id=job.edit_id,
                kind=job.kind, iteration=job.iteration, attempt=job.attempts, duration_seconds=round(time.monotonic() - started, 3), code=code,
            )
            if isinstance(exc, KeyboardInterrupt):
                raise
        else:
            emit(
                "job_succeeded", job_id=job.id, edit_id=job.edit_id, kind=job.kind,
                iteration=job.iteration, attempt=job.attempts, duration_seconds=round(time.monotonic() - started, 3),
            )
        return True

    def _plan(self, job: ClaimedJob) -> None:
        workspace_key = f"{job.edit_id}/iterations/{job.iteration:03d}"
        if job.attempts > 1:
            workspace_key += f"/attempts/{job.attempts:03d}"
        workspace = self.settings.work_root / workspace_key
        download_root = self.settings.work_root / "_downloads"
        download_root.mkdir(mode=0o700, exist_ok=True)
        suffix = Path(job.filename).suffix.lower()
        if not suffix or len(suffix) > 11 or not suffix[1:].isalnum():
            suffix = ".media"
        source = download_root / f"{job.id}-{job.attempts}{suffix}"
        try:
            if not source.exists():
                self.storage.download(job.storage_key, source)
                source.chmod(0o400)
            if job.source_sha256 and _digest(source) != job.source_sha256:
                raise ConflictError("downloaded source fingerprint does not match uploaded media")
            parent = self.repository.get_plan(job.edit_id, job.parent_iteration) if job.parent_iteration else None
            original = self.repository.get_plan(job.edit_id, 1)["instruction"] if parent else job.instruction
            prepared = prepare_edit(source, job.instruction, workspace, toolchain=self._toolchain(),
                                    previous_plan=parent["document"] if parent else None, original_instruction=original,
                                    silence_settings=SilenceSettings(
                                        minimum_silence_seconds=self.settings.minimum_silence_seconds,
                                        speech_padding_seconds=self.settings.speech_padding_seconds,
                                        threshold_min_db=self.settings.silence_threshold_min_db,
                                        threshold_max_db=self.settings.silence_threshold_max_db,
                                        calibration_margin_db=self.settings.silence_calibration_margin_db,
                                        fallback_threshold_db=self.settings.silence_fallback_threshold_db,
                                    ),
                                    progress=lambda message: self.repository.update_plan_progress(job, message))
            document = read_json(prepared.edit_plan)
            decisions = read_json(workspace / "decisions.json")
            self.repository.complete_plan(
                job, summary=prepared.summary, document=document, warnings=decisions["unsupported"],
                workspace_key=workspace_key, decision_log=decisions,
            )
        finally:
            source.unlink(missing_ok=True)

    def _workspace(self, job: ClaimedJob, *, approved: bool = False) -> Path:
        database_plan = self.repository.get_plan(job.edit_id, job.iteration)
        workspace_key = str(database_plan["workspace_key"])
        workspace = self.settings.work_root.joinpath(*Path(workspace_key).parts)
        try:
            workspace.resolve().relative_to(self.settings.work_root.resolve())
        except ValueError as exc:
            raise ConflictError("stored workspace key escapes the worker root") from exc
        workspace_plan = read_json(workspace / "edit-plan.json")
        if (approved and database_plan["status"] != "approved") or canonical_digest(workspace_plan) != database_plan["sha256"]:
            raise ConflictError("workspace plan does not match the immutable approved plan")
        validate_plan(workspace_plan, source=workspace / "edit-plan.json", check_files=True, require_confined_paths=True)
        return workspace

    def _compile(self, job: ClaimedJob) -> None:
        workspace = self._workspace(job)
        plan_path = workspace / "edit-plan.json"
        plan = validate_plan(read_json(plan_path), source=plan_path, check_files=True, require_confined_paths=True)
        project = workspace / "project.mlt"
        if not project.exists():
            write_mlt(plan, project)
        validate_compiled_mlt(project, plan, workspace)
        self.repository.complete_compilation(job)

    def _preview(self, job: ClaimedJob) -> None:
        workspace = self._workspace(job)
        result = render_prepared_edit(workspace, toolchain=self._toolchain(), quality="preview", inspect_output=False)
        self._publish(result.output, workspace / "preview.mp4")
        self.repository.complete_preview_render(job)

    @staticmethod
    def _publish(source: Path, destination: Path) -> None:
        try:
            os.link(source, destination)
        except FileExistsError:
            if _digest(source) != _digest(destination):
                raise ConflictError("immutable artifact already exists with different content")

    def _inspect(self, job: ClaimedJob) -> None:
        workspace = self._workspace(job)
        tools = self._toolchain()
        report = workspace / "inspection.json"
        if not report.exists():
            prepared = read_json(workspace / "work" / "prepared.json")
            analysis = read_json(workspace / prepared["analysis"])
            inspected = inspect(workspace / "preview.mp4", workspace / "edit-plan.json", workspace / "review",
                                ffmpeg=str(tools.ffmpeg), ffprobe=str(tools.ffprobe),
                                comparison_sources={"source": workspace / analysis["proxy"]})
            findings = read_json(inspected)
            if any(item.get("status") == "fail" for item in findings.get("automated_findings", [])):
                raise VideoEditingError("preview failed effect inspection", code="render_effect_validation_failed")
            self._publish(inspected, report)
        poster = workspace / "poster.png"
        if not poster.exists():
            frames = sorted((workspace / "review").glob("passes/*/frames/*.png"))
            if not frames:
                raise VideoEditingError("inspection produced no poster frame", code="inspection_failed")
            self._publish(frames[len(frames) // 2], poster)
        self.repository.complete_render(job, self._store(job, {
            "preview": workspace / "preview.mp4", "poster": poster, "inspection": report,
        }))
        self._latest(job)

    def _latest(self, job: ClaimedJob) -> None:
        # This is the only mutable pointer. All referenced artifacts remain immutable.
        # Serialize with the edit row so an older completion cannot regress it.
        with self.repository._connect() as connection:
            row = connection.execute("SELECT current_iteration,active_iteration FROM edits WHERE id=%s FOR UPDATE", (job.edit_id,)).fetchone()
            root = self.settings.work_root / job.edit_id
            temporary = root / f".latest-{uuid.uuid4().hex}.json"
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(row, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, root / "latest.json")
            finally:
                temporary.unlink(missing_ok=True)

    def _render(self, job: ClaimedJob) -> None:
        workspace = self._workspace(job, approved=True)
        result = render_prepared_edit(workspace, toolchain=self._toolchain())
        manifest = read_json(result.manifest)
        paths: dict[str, Path] = {
            "edit_plan": result.edit_plan,
            "mlt": result.project,
            "video": result.output,
            "manifest": result.manifest,
        }
        inspection = manifest["artifacts"].get("inspection")
        if inspection:
            paths["final_inspection"] = workspace / inspection["path"]
            paths["manifest"] = result.manifest
        paths["decisions"] = workspace / "decisions.json"
        self.repository.complete_render(job, self._store(job, paths))
        self._latest(job)

    def _store(self, job: ClaimedJob, paths: dict[str, Path]) -> list[dict]:
        artifacts = []
        for kind, path in paths.items():
            digest = _digest(path)
            suffix = path.suffix.lower() or ".bin"
            key = f"edits/{job.edit_id}/iterations/{job.iteration:03d}/{kind}-{digest}{suffix}"
            stored = self.storage.put_path(key, path)
            artifacts.append({
                "kind": kind, "storage_key": stored.key, "size": stored.size,
                "sha256": stored.sha256, "metadata": {"validated": kind == "video"},
            })
        return artifacts

    def _toolchain(self) -> Toolchain:
        if self.toolchain is None:
            self.toolchain = Toolchain.resolve(
                ffmpeg=self.settings.ffmpeg, ffprobe=self.settings.ffprobe, melt=self.settings.melt,
            )
        return self.toolchain

    def run_forever(self, poll_seconds: float = 1.0) -> None:
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Run the single Phase 1 database worker")
    parser.add_argument("--once", action="store_true", help="claim at most one job and exit")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    settings.validate()
    capability = dereverb_preflight()
    emit("dereverb_ready", model=capability["model"], model_sha256=capability["model_sha256"],
         executable_version=capability["executable_version"], device="cpu")
    repository = PostgresRepository(settings.database_url)
    worker = Worker(repository, build_storage(settings), settings)
    if args.once:
        worker.run_once()
    else:
        worker.run_forever(args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
