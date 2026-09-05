from __future__ import annotations

import io
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient

from service.app import create_app
from service.config import Settings
from service.engine import PreparedEdit
from service.repository import ConflictError, PostgresRepository
from service.storage import FilesystemStorage
from service.worker import Worker
from video_editing.pipeline import PipelineResult
from video_editing.workspace import JobWorkspace


DATABASE_URL = os.environ.get("VIDEO_EDIT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="VIDEO_EDIT_TEST_DATABASE_URL is required")


@pytest.fixture()
def repository():
    repo = PostgresRepository(DATABASE_URL)
    repo.migrate()
    with repo._connect() as connection:
        connection.execute("TRUNCATE results, artifacts, jobs, approvals, plans, edits, videos CASCADE")
    return repo


def _edit(repository):
    video = repository.create_video(
        filename="clip.mp4", content_type="video/mp4", storage_key=f"test/{os.urandom(8).hex()}",
        size=5, sha256=hashlib.sha256(b"source").hexdigest(),
    )
    return repository.create_edit(video_id=video["id"], instruction="Keep the action")


def test_state_transitions_approval_gate_and_single_claim(repository):
    edit = _edit(repository)
    with pytest.raises(ConflictError):
        repository.queue_render(edit["id"])
    first = repository.claim("worker-a", 300)
    assert first and first.kind == "plan"
    assert repository.claim("worker-b", 300) is None
    plan_id = repository.complete_plan(
        first, summary="Highlight", document={"version": "1.0"}, warnings=[], workspace_key=f"{edit['id']}/plan-1",
    )
    compilation = repository.claim("compiler", 300)
    repository.complete_compilation(compilation)
    assert repository.get_edit(edit["id"])["state"] == "awaiting_approval"
    repository.approve(edit["id"], plan_id)
    repository.queue_render(edit["id"])
    preview = repository.claim("previewer", 300)
    assert preview.kind == "preview"
    repository.fail(preview, {"code": "fixture_preview_skipped"})
    render = repository.claim("worker-b", 300)
    assert render and render.kind == "render"
    assert repository.get_edit(edit["id"])["state"] == "rendering"


def test_expired_lease_is_recovered_after_restart(repository):
    edit = _edit(repository)
    claimed = repository.claim("dead-worker", 300)
    with repository._connect() as connection:
        connection.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (claimed.id,))
    recovered = repository.claim("replacement-worker", 300)
    assert recovered and recovered.id == claimed.id and recovered.edit_id == edit["id"]


def test_retry_history_and_exhausted_worker_are_reconciled(repository):
    edit = _edit(repository)
    first = repository.claim("worker-a", 300, 2)
    assert first and repository.retry(first, {"code": "process_timeout", "retryable": True})
    second = repository.claim("worker-b", 300, 2)
    assert second and second.id == first.id and second.attempts == 2
    with repository._connect() as connection:
        connection.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (second.id,))
    assert repository.reconcile_exhausted(2) == 1
    assert repository.get_edit(edit["id"])["state"] == "failed"
    with repository._connect() as connection:
        attempts = connection.execute(
            "SELECT attempt,status FROM job_attempts WHERE job_id=%s ORDER BY attempt", (first.id,),
        ).fetchall()
    assert [(row["attempt"], row["status"]) for row in attempts] == [(1, "retrying"), (2, "lost")]


def test_complete_upload_plan_approve_render_result(repository, monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = Settings(DATABASE_URL, "filesystem", root / "objects", root / "jobs")
        storage = FilesystemStorage(settings.storage_root)

        def fake_prepare(source, instruction, workspace, **_kwargs):
            job = JobWorkspace.create(workspace)
            job.import_media(source)
            plan = job.write_json("edit-plan.json", {"version": "1.0", "instruction": instruction})
            job.write_json("work/prepared.json", {"summary": "Highlight"})
            job.write_json("decisions.json", {"observations": [], "decisions": [], "unsupported": [], "assumptions": []})
            return PreparedEdit(job.root, plan, "Highlight")

        def fake_render(workspace, **_kwargs):
            job = JobWorkspace.open(workspace)
            project = job.path("project.mlt")
            project.write_text("<mlt/>", encoding="utf-8")
            attempt = job.allocate_render()
            attempt.output.write_bytes(b"validated-video")
            manifest = job.write_json("result.json", {
                "status": "completed",
                "artifacts": {
                    "video": {"path": attempt.output.relative_to(job.root).as_posix()},
                    "project": {"path": "project.mlt"},
                },
            })
            return PipelineResult(job.root, job.path("edit-plan.json"), project, attempt.output, manifest)

        monkeypatch.setattr("service.worker.prepare_edit", fake_prepare)
        monkeypatch.setattr("service.worker.render_prepared_edit", fake_render)
        client = TestClient(create_app(settings, repository, storage))
        video = client.post("/v1/videos", files={"file": ("clip.mp4", b"source", "video/mp4")}).json()
        edit = client.post("/v1/edits", json={"video_id": video["id"], "instruction": "Keep action"}).json()
        worker = Worker(repository, storage, settings, worker_id="integration-worker", toolchain=object())
        assert worker.run_once()
        compile_job = repository.claim("fixture-compiler", 300)
        repository.complete_compilation(compile_job)
        preview_job = repository.claim("fixture-preview", 300)
        repository.fail(preview_job, {"code": "fixture_preview_skipped"})
        plan = client.get(f"/v1/edits/{edit['id']}/plan").json()
        assert client.post(f"/v1/edits/{edit['id']}/approve", json={"plan_id": plan["plan_id"]}).status_code == 200
        assert client.post(f"/v1/edits/{edit['id']}/render").status_code == 202
        # This contract fixture uses a minimal fake plan; the real engine is
        # covered by the container iteration end-to-end test.
        monkeypatch.setattr(worker, "_workspace", lambda _job, **_kwargs: settings.work_root / repository.get_plan(edit["id"])["workspace_key"])
        assert worker.run_once()
        result = client.get(f"/v1/edits/{edit['id']}/result")
        assert result.status_code == 200
        assert result.json()["state"] == "completed"
        assert client.get(result.json()["video_url"]).content == b"validated-video"
