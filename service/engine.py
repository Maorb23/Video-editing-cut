"""Two-stage adapter over the Phase 0 components for approval-gated service jobs."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from video_editing.analysis import AnalysisProvider, FrameAnalysisProvider
from video_editing.analysis.frames import source_frame_rate
from video_editing.adaptive_silence import SilenceSettings
from video_editing.audio import prepare_dereverb
from video_editing.artifacts import artifact_record, validate_compiled_mlt, validate_rendered_video
from video_editing.errors import VideoEditingError
from video_editing.inspect import inspect
from video_editing.mlt import write_mlt
from video_editing.pipeline import PipelineResult
from video_editing.plan import read_json, validate_plan
from video_editing.planning import EditPlanner, OpenAIResponsesModel, StructuredModel
from video_editing.render import render
from video_editing.supervisor import CancellationToken, ProcessLimits, ProcessSupervisor, Toolchain
from video_editing.workspace import JobWorkspace, RenderAttempt


@dataclass(frozen=True)
class PreparedEdit:
    workspace: Path
    edit_plan: Path
    summary: str


def _runtime(
    *, toolchain: Toolchain | None, process_timeout: float, no_progress_timeout: float,
    max_diagnostic_bytes: int, cancellation: CancellationToken | None,
) -> tuple[CancellationToken, ProcessSupervisor, Toolchain, dict[str, str]]:
    token = cancellation or CancellationToken()
    supervisor = ProcessSupervisor(ProcessLimits(
        wall_timeout=process_timeout,
        no_progress_timeout=no_progress_timeout,
        max_output_bytes=max_diagnostic_bytes,
    ), token)
    tools = toolchain or Toolchain.resolve()
    return token, supervisor, tools, tools.versions(supervisor)


def prepare_edit(
    video: Path,
    instruction: str,
    job_directory: Path,
    *,
    model: StructuredModel | None = None,
    model_name: str | None = None,
    analyzer: AnalysisProvider | None = None,
    toolchain: Toolchain | None = None,
    frame_rate: Fraction | None = None,
    previous_plan: dict[str, Any] | None = None,
    original_instruction: str | None = None,
    max_analysis_frames: int = 12,
    silence_settings: SilenceSettings | None = None,
    max_repair_attempts: int = 2,
    process_timeout: float = 7200.0,
    no_progress_timeout: float = 180.0,
    max_diagnostic_bytes: int = 256 * 1024,
    cancellation: CancellationToken | None = None,
    progress: Callable[[str], None] | None = None,
) -> PreparedEdit:
    prepared_path = job_directory / "work" / "prepared.json"
    if prepared_path.is_file():
        workspace = JobWorkspace.open(job_directory)
        prepared = read_json(prepared_path)
        plan_path = workspace.path("edit-plan.json")
        validate_plan(read_json(plan_path), source=plan_path, check_files=True, require_confined_paths=True)
        return PreparedEdit(workspace.root, plan_path, str(prepared["summary"]))

    workspace = JobWorkspace.create(job_directory)
    stage = "toolchain"
    try:
        _token, supervisor, tools, versions = _runtime(
            toolchain=toolchain, process_timeout=process_timeout, no_progress_timeout=no_progress_timeout,
            max_diagnostic_bytes=max_diagnostic_bytes, cancellation=cancellation,
        )
        stage = "ingest"
        source = workspace.import_media(video)
        source_relative = source.relative_to(workspace.root).as_posix()
        stage = "dereverb"
        dereverb = prepare_dereverb(instruction, source, workspace, tools, previous_plan=previous_plan, progress=progress,
                                    timeout=process_timeout, max_diagnostic_bytes=max_diagnostic_bytes)
        stage = "analysis"
        configured_silence = silence_settings or SilenceSettings()
        selected_analyzer = analyzer or FrameAnalysisProvider(
            frame_rate=frame_rate, max_samples=max_analysis_frames, silence_settings=configured_silence,
        )
        analysis = selected_analyzer.analyze(source, workspace, tools, supervisor)
        if frame_rate is None:
            source_rate = source_frame_rate(analysis.data["source"]["video"])
            policy_rate = analysis.data["timeline_policy"]["frame_rate"]
            if source_rate != Fraction(policy_rate["numerator"], policy_rate["denominator"]):
                raise VideoEditingError("analysis did not preserve the source frame rate", code="frame_rate_mismatch")
        stage = "planning"
        if progress:
            progress("Creating a proposed plan")
        selected_model = model or OpenAIResponsesModel(model=model_name or os.environ.get("VIDEO_EDIT_MODEL", "gpt-5.6"))
        planned = EditPlanner(selected_model, max_repair_attempts=max_repair_attempts).plan(
            instruction, analysis, plan_path=workspace.path("edit-plan.json"), source_relative=source_relative,
            previous_plan=previous_plan, original_instruction=original_instruction, allow_unsupported=True, dereverb=dereverb,
        )
        plan_path = workspace.write_json("edit-plan.json", planned.plan.data)
        workspace.write_json("decisions.json", planned.decision_log)
        workspace.write_json("work/resolved-timeline.json", {"version": "1.0", "tracks": list(planned.plan.resolved_tracks)})
        workspace.write_json("work/prepared.json", {
            "version": "1.0", "instruction": instruction, "summary": planned.summary,
            "analysis": analysis.path.relative_to(workspace.root).as_posix(),
            "planning": {"attempts": list(planned.attempts)}, "toolchain": versions,
        })
        return PreparedEdit(workspace.root, plan_path, planned.summary)
    except BaseException as exc:
        try:
            workspace.write_json("failure.json", {
                "version": "1.0", "status": "failed", "stage": stage,
                "error": getattr(exc, "code", "unexpected_error"), "message": str(exc),
            })
        except (OSError, VideoEditingError):
            pass
        if isinstance(exc, (VideoEditingError, KeyboardInterrupt)):
            raise
        raise VideoEditingError(f"pipeline failed during {stage}: {exc}", code="pipeline_failed") from exc


def render_prepared_edit(
    job_directory: Path,
    *,
    toolchain: Toolchain | None = None,
    process_timeout: float = 7200.0,
    no_progress_timeout: float = 180.0,
    max_diagnostic_bytes: int = 256 * 1024,
    cancellation: CancellationToken | None = None,
    quality: str = "final",
    inspect_output: bool = True,
) -> PipelineResult:
    workspace = JobWorkspace.open(job_directory)
    manifest_name = "preview-result.json" if quality == "preview" else "result.json"
    existing_manifest = workspace.path(manifest_name)
    if existing_manifest.is_file():
        manifest_data = read_json(existing_manifest)
        artifacts = manifest_data.get("artifacts", {})
        output = workspace.path(artifacts["video"]["path"])
        project = workspace.path(artifacts["project"]["path"])
        if manifest_data.get("status") != "completed" or not output.is_file() or not project.is_file():
            raise VideoEditingError("existing result manifest is incomplete", code="invalid_result")
        return PipelineResult(workspace.root, workspace.path("edit-plan.json"), project, output, existing_manifest)

    stage = "toolchain"
    attempt: RenderAttempt | None = None
    try:
        token, supervisor, tools, current_versions = _runtime(
            toolchain=toolchain, process_timeout=process_timeout, no_progress_timeout=no_progress_timeout,
            max_diagnostic_bytes=max_diagnostic_bytes, cancellation=cancellation,
        )
        prepared = read_json(workspace.path("work/prepared.json"))
        plan_path = workspace.path("edit-plan.json")
        plan = validate_plan(read_json(plan_path), source=plan_path, check_files=True, require_confined_paths=True)
        stage = "compile"
        project = workspace.path("project.mlt")
        if not project.exists():
            write_mlt(plan, project)
        compilation = validate_compiled_mlt(project, plan, workspace.root)
        stage = "render"
        attempt = workspace.allocate_render()
        render(
            project, attempt.output, quality=quality, melt=str(tools.melt), progress_stream=io.StringIO(),
            export=plan.data["export"], supervisor=supervisor, cancellation=token,
        )
        stage = "validate_render"
        validation = validate_rendered_video(attempt.output, plan, tools, supervisor, workspace.root)
        workspace.write_json(attempt.validation.relative_to(workspace.root), validation)
        analysis_path = workspace.path(str(prepared["analysis"]))
        analysis_data = read_json(analysis_path)
        inspection_path: Path | None = None
        if inspect_output:
            stage = "validate_effects"
            inspection_path = inspect(
                attempt.output, plan_path, attempt.directory / "review", ffmpeg=str(tools.ffmpeg),
                ffprobe=str(tools.ffprobe), comparison_sources={"source": workspace.path(str(analysis_data["proxy"]))},
            )
            failures = [item for item in read_json(inspection_path).get("automated_findings", []) if item.get("status") == "fail"]
            if failures:
                raise VideoEditingError("rendered transform evidence failed automated conformance", code="render_effect_validation_failed")
        stage = "manifest"
        source_asset = workspace.path(plan.data["assets"][0]["path"])
        manifest: dict[str, Any] = {
            "version": "1.0", "status": "completed", "instruction": prepared["instruction"],
            "summary": prepared["summary"], "timeline_policy": analysis_data["timeline_policy"],
            "toolchain": {"planned": prepared["toolchain"], "rendered": current_versions},
            "planning": prepared["planning"], "compilation_validation": compilation,
            "dereverb_provenance": plan.data.get("analysis", {}).get("dereverb"),
            "render_validation": validation, "accepted_render_id": attempt.id,
            "artifacts": {
                "analysis": artifact_record(analysis_path, workspace.root),
                "source_copy": artifact_record(source_asset, workspace.root),
                "edit_plan": artifact_record(plan_path, workspace.root),
                "resolved_timeline": artifact_record(workspace.path("work/resolved-timeline.json"), workspace.root),
                "project": artifact_record(project, workspace.root), "video": artifact_record(attempt.output, workspace.root),
                "render_validation": artifact_record(attempt.validation, workspace.root),
                **({"inspection": artifact_record(inspection_path, workspace.root)} if inspection_path else {}),
            },
        }
        result_path = workspace.write_json(manifest_name, manifest)
        return PipelineResult(workspace.root, plan_path, project, attempt.output, result_path)
    except BaseException as exc:
        failure: dict[str, Any] = {
            "version": "1.0", "status": "failed", "stage": stage,
            "error": getattr(exc, "code", "unexpected_error"), "message": str(exc),
        }
        if attempt is not None:
            failure["render_attempt"] = {"id": attempt.id, "status": "failed", "stage": stage}
        failure_path = workspace.path("failure.json")
        if not failure_path.exists():
            try:
                workspace.write_json("failure.json", failure)
            except (OSError, VideoEditingError):
                pass
        if isinstance(exc, (VideoEditingError, KeyboardInterrupt)):
            raise
        raise VideoEditingError(f"pipeline failed during {stage}: {exc}", code="pipeline_failed") from exc
