from __future__ import annotations

import io
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
pytest.importorskip("multipart")

from service.app import create_app
from service.config import Settings
from service.repository import ConflictError, NotFoundError
from service.storage import FilesystemStorage


async def run_inline(function, *args, **kwargs):
    return function(*args, **kwargs)


class FakeRepository:
    def __init__(self) -> None:
        self.video = None
        self.edit = None
        self.plan = None
        self.artifacts = []

    def create_video(self, **values):
        self.video = {"id": values["video_id"], "state": "uploaded", "filename": values["filename"], **values}
        return self.video

    def create_edit(self, *, video_id, instruction):
        if not self.video or video_id != self.video["id"]:
            raise NotFoundError("video not found")
        self.edit = {"id": "edt_" + "2" * 32, "state": "analyzing", "created_at": datetime.now(timezone.utc), "progress": {"stage": "queued"}}
        return self.edit

    def get_edit(self, edit_id):
        if not self.edit or edit_id != self.edit["id"]:
            raise NotFoundError("edit not found")
        return self.edit

    def get_plan(self, edit_id):
        if not self.plan:
            raise NotFoundError("plan not available")
        return self.plan

    def approve(self, edit_id, plan_id):
        if not self.plan or plan_id != self.plan["id"]:
            raise NotFoundError("plan not found")
        self.edit["state"] = "approved"
        return self.edit

    def queue_render(self, edit_id):
        if self.edit["state"] != "approved":
            raise ConflictError("approval required")
        return self.edit

    def get_result(self, edit_id):
        if self.edit["state"] != "completed":
            raise ConflictError("validated result is not available")
        return self.edit, self.artifacts

    def get_artifact(self, edit_id, kind):
        return next(item for item in self.artifacts if item["kind"] == kind)


class ApiContractTests(IsolatedAsyncioTestCase):
    async def test_api_contract_and_approval_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings("unused", "filesystem", root / "objects", root / "jobs", max_upload_bytes=1024)
            repo = FakeRepository()
            storage = FilesystemStorage(settings.storage_root)
            transport = httpx.ASGITransport(app=create_app(settings, repo, storage, blocking_runner=run_inline))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                uploaded = await client.post("/v1/videos", files={"file": ("clip.mp4", b"media", "video/mp4")})
                self.assertEqual(uploaded.status_code, 201)
                video_id = uploaded.json()["id"]
                created = await client.post("/v1/edits", json={"video_id": video_id, "instruction": "Keep the action"})
                self.assertEqual(created.status_code, 202)
                edit_id = created.json()["id"]
                self.assertEqual((await client.post(f"/v1/edits/{edit_id}/render")).status_code, 409)
                self.assertEqual((await client.get(f"/v1/edits/{edit_id}/plan")).status_code, 404)
                repo.plan = {
                    "id": "pln_" + "3" * 32, "status": "proposed", "summary": "Short highlight",
                    "warnings": [], "document": {"version": "1.0"},
                }
                approved = await client.post(f"/v1/edits/{edit_id}/approve", json={"plan_id": repo.plan["id"]})
                self.assertEqual(approved.json()["state"], "approved")
                self.assertEqual((await client.post(f"/v1/edits/{edit_id}/render")).status_code, 202)
                self.assertEqual((await client.get(f"/v1/edits/{edit_id}/result")).status_code, 409)
