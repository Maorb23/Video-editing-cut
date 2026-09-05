"""Standalone Phase 0 orchestration: video + instruction -> validated result."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, TextIO

from .analysis import AnalysisProvider, FrameAnalysisProvider
from .artifacts import artifact_record, validate_compiled_mlt, validate_rendered_video
from .errors import VideoEditingError
from .inspect import inspect
from .mlt import write_mlt
from .planning import EditPlanner, OpenAIResponsesModel, StructuredModel
from .plan import read_json
from .render import render
from .supervisor import CancellationToken, ProcessLimits, ProcessSupervisor, Toolchain
from .workspace import JobWorkspace, RenderAttempt


@dataclass(frozen=True)
class PipelineResult:
    workspace: Path
    edit_plan: Path
    project: Path
    output: Path
    manifest: Path


def run_pipeline(
    video: Path,
    instruction: str,
    job_directory: Path,
    *,
    model: StructuredModel | None = None,
    model_name: str | None = None,
    analyzer: AnalysisProvider | None = None,
    toolchain: Toolchain | None = None,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    melt: str | None = None,
    frame_rate: Fraction = Fraction(30, 1),
    max_analysis_frames: int = 12,
    max_repair_attempts: int = 2,
    process_timeout: float = 7200.0,
    no_progress_timeout: float = 180.0,
    max_diagnostic_bytes: int = 256 * 1024,
    cancellation: CancellationToken | None = None,
    progress_stream: TextIO | None = None,
) -> PipelineResult:
    """Run the complete standalone workflow without any Codex dependency."""
    workspace = JobWorkspace.create(job_directory)
    stage = "workspace"
    attempt: RenderAttempt | None = None
    try:
        token = cancellation or CancellationToken()
        supervisor = ProcessSupervisor(ProcessLimits(
            wall_timeout=process_timeout,
            no_progress_timeout=no_progress_timeout,
            max_output_bytes=max_diagnostic_bytes,
        ), token)
        selected_tools = toolchain or Toolchain.resolve(ffmpeg=ffmpeg, ffprobe=ffprobe, melt=melt)
        stage = "toolchain"
        versions = selected_tools.versions(supervisor)

        stage = "ingest"
        source = workspace.import_media(video)
        source_relative = source.relative_to(workspace.root).as_posix()

        stage = "analysis"
        selected_analyzer = analyzer or FrameAnalysisProvider(frame_rate=frame_rate, max_samples=max_analysis_frames)
        analysis = selected_analyzer.analyze(source, workspace, selected_tools, supervisor)

        stage = "planning"
        selected_model = model or OpenAIResponsesModel(model=model_name or os.environ.get("VIDEO_EDIT_MODEL", "gpt-5.6"))
        planner = EditPlanner(selected_model, max_repair_attempts=max_repair_attempts)
        plan_path = workspace.path("edit-plan.json")
        planned = planner.plan(instruction, analysis, plan_path=plan_path, source_relative=source_relative)
        workspace.write_json("edit-plan.json", planned.plan.data)
        decisions_path = workspace.write_json("decisions.json", planned.decision_log)
        workspace.write_json("work/resolved-timeline.json", {
            "version": "1.0", "tracks": list(planned.plan.resolved_tracks),
        })

        stage = "compile"
        project = workspace.path("project.mlt")
        write_mlt(planned.plan, project)
        compilation = validate_compiled_mlt(project, planned.plan, workspace.root)

        stage = "render"
        attempt = workspace.allocate_render()
        render(
            project,
            attempt.output,
            quality="final",
            melt=str(selected_tools.melt),
            progress_stream=progress_stream or io.StringIO(),
            export=planned.plan.data["export"],
            supervisor=supervisor,
            cancellation=token,
        )

        stage = "validate_render"
        validation = validate_rendered_video(attempt.output, planned.plan, selected_tools, supervisor, workspace.root)
        workspace.write_json(attempt.validation.relative_to(workspace.root), validation)

        inspection_path: Path | None = None
        if any(operation.get("enabled", True) and operation.get("type") == "transform" for operation in planned.plan.data["operations"]):
            stage = "validate_effects"
            proxy = workspace.path(str(analysis.data["proxy"]))
            inspection_path = inspect(
                attempt.output,
                plan_path,
                workspace.path("review"),
                ffmpeg=str(selected_tools.ffmpeg),
                ffprobe=str(selected_tools.ffprobe),
                comparison_sources={"source": proxy},
            )
            inspection_data = read_json(inspection_path)
            failures = [item for item in inspection_data.get("automated_findings", []) if item.get("status") == "fail"]
            if failures:
                raise VideoEditingError("rendered transform evidence failed automated conformance", code="render_effect_validation_failed")

        stage = "manifest"
        manifest_data: dict[str, Any] = {
            "version": "1.0",
            "status": "completed",
            "instruction": instruction,
            "summary": planned.summary,
            "timeline_policy": analysis.data["timeline_policy"],
            "toolchain": versions,
            "planning": {"attempts": list(planned.attempts)},
            "decision_log": planned.decision_log,
            "compilation_validation": compilation,
            "render_validation": validation,
            "accepted_render_id": attempt.id,
            "artifacts": {
                "decisions": artifact_record(decisions_path, workspace.root),
                "analysis": artifact_record(analysis.path, workspace.root),
                "source_copy": artifact_record(source, workspace.root),
                "edit_plan": artifact_record(plan_path, workspace.root),
                "resolved_timeline": artifact_record(workspace.path("work/resolved-timeline.json"), workspace.root),
                "project": artifact_record(project, workspace.root),
                "video": artifact_record(attempt.output, workspace.root),
                "render_validation": artifact_record(attempt.validation, workspace.root),
                **({"inspection": artifact_record(inspection_path, workspace.root)} if inspection_path is not None else {}),
            },
        }
        manifest = workspace.write_json("result.json", manifest_data)
        return PipelineResult(workspace.root, plan_path, project, attempt.output, manifest)
    except BaseException as exc:
        failure = {
            "version": "1.0",
            "status": "failed",
            "stage": stage,
            "error": getattr(exc, "code", "unexpected_error"),
            "message": str(exc),
        }
        if attempt is not None:
            attempt_data: dict[str, Any] = {
                "id": attempt.id,
                "status": "failed",
                "stage": stage,
            }
            if attempt.output.is_file():
                attempt_data["output"] = artifact_record(attempt.output, workspace.root)
            try:
                workspace.write_json(attempt.validation.relative_to(workspace.root), attempt_data)
            except (OSError, VideoEditingError):
                pass
            failure["render_attempt"] = attempt_data
        try:
            workspace.write_json("failure.json", failure)
        except (OSError, VideoEditingError):
            pass
        if isinstance(exc, (VideoEditingError, KeyboardInterrupt)):
            raise
        raise VideoEditingError(f"pipeline failed during {stage}: {exc}", code="pipeline_failed") from exc
