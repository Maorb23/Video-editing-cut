"""Transactional iteration and HTTP contracts against an isolated PostgreSQL DB."""
from __future__ import annotations

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from service.app import create_app
from service.config import Settings
from service.repository import ConflictError
from service.storage import FilesystemStorage
from tests.integration.test_phase1_postgres import repository, _edit, DATABASE_URL

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="VIDEO_EDIT_TEST_DATABASE_URL is required")

LOG = {"observations": [{"type": "visual", "description": "Subject on the right", "evidence": ["analysis/frames/sample-001.jpg"], "confidence": 0.82}],
       "decisions": [{"request": "focus", "operation": "transform", "reason": "Crop toward the visible subject", "confidence": 0.78}],
       "unsupported": ["Pitch shifting"], "assumptions": ["Sampled frames cover the subject"]}


def planned(repo, edit_id, number):
    job = repo.claim("planner", 300)
    assert job.iteration == number and job.kind in {"plan", "revision"}
    plan_id = repo.complete_plan(job, summary="Reviewable plan", document={"version": "1.0", "n": number}, warnings=LOG["unsupported"],
                                 workspace_key=f"{edit_id}/iterations/{number:03d}", decision_log=LOG)
    compilation = repo.claim("compiler", 300)
    assert compilation.kind == "compilation"
    repo.complete_compilation(compilation)
    return plan_id


def previewed(repo, storage, edit_id, number):
    job = repo.claim("previewer", 300)
    assert job.kind == "preview" and job.iteration == number
    assert repo.get_plan(edit_id, number)["preview_status"] == "running"
    repo.complete_preview_render(job)
    inspection = repo.claim("inspector", 300)
    assert inspection.kind == "inspection"
    artifacts = []
    for kind in ("preview", "poster", "inspection"):
        stored = storage.put(f"{edit_id}/{number}/{kind}", io.BytesIO(b"0123456789"))
        artifacts.append({"kind": kind, "storage_key": stored.key, "sha256": stored.sha256, "size": stored.size})
    repo.complete_render(inspection, artifacts)


def test_revision_history_decisions_download_and_selected_approval(repository, tmp_path):
    storage = FilesystemStorage(tmp_path / "objects")
    client = TestClient(create_app(Settings(DATABASE_URL, "filesystem", tmp_path / "objects", tmp_path / "jobs"), repository, storage))
    video = client.post("/v1/videos", files={"file": ("source.mp4", b"source", "video/mp4")}).json()
    created = client.post("/v1/edits", json={"video_id": video["id"], "instruction": "brighten and zoom"})
    assert created.status_code == 202 and created.json()["iteration"] == 1
    edit_id = created.json()["id"]
    assert client.post(f"/v1/edits/{edit_id}/revise", json={"instruction": "too early"}).status_code == 409
    first = planned(repository, edit_id, 1)
    previewed(repository, storage, edit_id, 1)
    before = repository.get_plan(edit_id, 1)
    response = client.get(f"/v1/edits/{edit_id}/plan").json()
    assert response["decision_log"] == LOG and response["preview_status"] == "succeeded"
    download = client.get(response["preview_url"])
    assert download.content == b"0123456789" and download.headers["content-type"] == "video/mp4"
    ranged = client.get(response["preview_url"], headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206 and ranged.content == b"2345"
    assert client.get(response["preview_url"], headers={"Range": "bytes=-3"}).content == b"789"
    assert client.get(response["preview_url"], headers={"Range": "bytes=90-"}).status_code == 416
    for number in (2, 3):
        revised = client.post(f"/v1/edits/{edit_id}/revise", json={"instruction": f"revision {number}"})
        assert revised.status_code == 202 and revised.json()["iteration"] == number
        planned(repository, edit_id, number)
        previewed(repository, storage, edit_id, number)
    assert repository.get_plan(edit_id, 1)["document"] == before["document"]
    assert repository.get_plan(edit_id, 1)["sha256"] == before["sha256"]
    history = client.get(f"/v1/edits/{edit_id}/iterations").json()["iterations"]
    assert [i["parent_iteration"] for i in history] == [None, 1, 2]
    assert repository.get_edit(edit_id)["active_iteration"] == 3
    assert client.post(f"/v1/edits/{edit_id}/approve", json={"plan_id": first}).status_code == 200
    assert client.post(f"/v1/edits/{edit_id}/render").status_code == 202
    render = repository.claim("renderer", 300)
    assert render.kind == "render" and render.iteration == 1
    repository.complete_render(render, [{"kind": "video", "storage_key": f"{edit_id}/final1", "size": 1, "sha256": "0" * 64}])
    assert repository.get_edit(edit_id)["approved_iteration"] == 1
    source = storage.open(f"videos/{video['id']}/source.mp4")
    with source:
        assert hashlib.sha256(source.read()).hexdigest() == hashlib.sha256(b"source").hexdigest()


def test_racing_revisions_and_database_immutability(repository):
    edit = _edit(repository)
    planned(repository, edit["id"], 1)
    def revise():
        try:
            return repository.revise(edit["id"], "another change")["current_iteration"]
        except ConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: revise(), range(2)), key=str) == [2, "conflict"]
    with pytest.raises(Exception, match="plan content is immutable"), repository._connect() as c:
        c.execute("UPDATE plans SET document='{}' WHERE edit_id=%s", (edit["id"],))
    with pytest.raises(Exception), repository._connect() as c:
        c.execute("INSERT INTO iterations(edit_id,iteration,parent_iteration,instruction) VALUES (%s,3,9,'bad')", (edit["id"],))


def test_old_preview_completion_and_failure_do_not_clobber_revision(repository, tmp_path):
    edit = _edit(repository)
    planned(repository, edit["id"], 1)
    old = repository.claim("slow-preview", 300)
    repository.revise(edit["id"], "less zoom")
    planned(repository, edit["id"], 2)
    previewed(repository, FilesystemStorage(tmp_path), edit["id"], 2)
    repository.complete_preview_render(old)
    old_inspection = repository.claim("late-inspector", 300)
    repository.complete_render(old_inspection, [])
    assert repository.get_edit(edit["id"])["active_iteration"] == 2
    assert repository.get_edit(edit["id"])["state"] == "awaiting_approval"


def test_preview_failure_is_separate_from_plan_and_retry_is_fenced(repository):
    edit = _edit(repository)
    planned(repository, edit["id"], 1)
    job = repository.claim("previewer", 300)
    repository.fail(job, {"code": "render_failed", "message": "Melt failed"})
    assert repository.get_edit(edit["id"])["state"] == "awaiting_approval"
    assert repository.get_plan(edit["id"])["preview_status"] == "failed"
    assert repository.retry(job, {"code": "process_timeout"}) is False


def test_approval_cannot_queue_a_final_render_before_preview_inspection(repository):
    edit = _edit(repository)
    plan_id = planned(repository, edit["id"], 1)
    assert repository.get_plan(edit["id"])["preview_status"] == "queued"
    with pytest.raises(ConflictError, match="preview inspection"):
        repository.approve(edit["id"], plan_id)
