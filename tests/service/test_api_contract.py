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
        self.users = {}
        self.sessions = {}
        self.profiles = {}
        self.ledger = {}

    def register(self, *, email, password):
        if email in self.users:
            raise ConflictError("an account with that email already exists")
        user = {"id": "usr_" + str(len(self.users) + 1) * 32, "email": email}
        self.users[email] = user
        return user

    def create_session(self, *, email, password):
        if email not in self.users:
            raise NotFoundError("invalid email or password")
        token = f"token-{email}"
        self.sessions[token] = self.users[email]
        return self.users[email], token

    def get_session_user(self, token):
        if token not in self.sessions:
            raise NotFoundError("session not found")
        return self.sessions[token]

    def delete_session(self, token): self.sessions.pop(token, None)

    def issue_token(self, user, purpose): return f"{purpose}:{user['id']}:{user['email']}"
    def read_token(self, token, purpose, max_age):
        prefix, user_id, email = token.split(":", 2)
        if prefix != purpose: raise NotFoundError("link is invalid")
        return {"id": user_id, "email": email}
    def find_user(self, email): return self.users.get(email)
    def reset_password(self, user_id, password): self.reset_user_id = user_id
    def ensure_profile(self, user_id):
        self.profiles.setdefault(user_id, {"user_id": user_id, "avatar_key": "camera", "email_verified_at": None})
        self.ledger.setdefault(user_id, [{"id": "crd_welcome", "amount": 200, "reason": "promotion", "edit_id": None, "payment_reference": None, "metadata": {}, "created_at": datetime.now(timezone.utc)}])
        return self.profiles[user_id]
    def get_profile(self, user_id): return self.ensure_profile(user_id)
    def set_avatar(self, user_id, avatar_key): self.ensure_profile(user_id)["avatar_key"] = avatar_key; return self.profiles[user_id]
    def verify_email(self, user_id): self.ensure_profile(user_id)["email_verified_at"] = datetime.now(timezone.utc); return self.profiles[user_id]
    def credit_summary(self, user_id):
        self.ensure_profile(user_id); return {"balance": sum(x["amount"] for x in self.ledger[user_id]), "entries": self.ledger[user_id]}
    def create_mock_top_up(self, user_id, package, succeed):
        self.ensure_profile(user_id)
        if succeed: self.ledger[user_id].append({"id": "crd_topup", "amount": package["credits"], "reason": "purchase", "edit_id": None, "payment_reference": "ord_mock", "metadata": {}, "created_at": datetime.now(timezone.utc)})
        return {"id": "ord_mock", "package_key": package["key"], "status": "succeeded" if succeed else "failed"}
    def list_projects(self, user_id): return [] if not self.edit or self.edit.get("user_id") != user_id else [{**self.edit, "filename": self.video["filename"], "instruction": "Keep the action", "updated_at": self.edit["created_at"], "current_iteration": 1, "active_iteration": None, "approved_iteration": None, "credits_used": 0, "iterations": []}]

    def create_video(self, **values):
        self.video = {"id": values["video_id"], "state": "uploaded", "filename": values["filename"], **values}
        return self.video

    def create_edit(self, *, video_id, instruction, user_id=None):
        if not self.video or video_id != self.video["id"] or (user_id and self.video.get("user_id") != user_id):
            raise NotFoundError("video not found")
        self.edit = {"id": "edt_" + "2" * 32, "state": "analyzing", "created_at": datetime.now(timezone.utc), "progress": {"stage": "queued"}, "user_id": user_id}
        return self.edit

    def get_edit(self, edit_id, user_id=None):
        if not self.edit or edit_id != self.edit["id"] or (user_id and self.edit.get("user_id") != user_id):
            raise NotFoundError("edit not found")
        return self.edit

    def get_plan(self, edit_id, iteration=None, user_id=None):
        if not self.plan:
            raise NotFoundError("plan not available")
        return self.plan

    def approve(self, edit_id, plan_id, user_id=None):
        if not self.plan or plan_id != self.plan["id"]:
            raise NotFoundError("plan not found")
        self.edit["state"] = "approved"
        return self.edit

    def queue_render(self, edit_id, user_id=None):
        if self.edit["state"] != "approved":
            raise ConflictError("approval required")
        return self.edit

    def get_result(self, edit_id, user_id=None):
        if self.edit["state"] != "completed":
            raise ConflictError("validated result is not available")
        return self.edit, self.artifacts

    def get_artifact(self, edit_id, kind, iteration=None, user_id=None):
        return next(item for item in self.artifacts if item["kind"] == kind)


class ApiContractTests(IsolatedAsyncioTestCase):
    async def test_api_contract_and_approval_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings("unused", "filesystem", root / "objects", root / "jobs", max_upload_bytes=1024, session_cookie_secure=False)
            repo = FakeRepository()
            storage = FilesystemStorage(settings.storage_root)
            transport = httpx.ASGITransport(app=create_app(settings, repo, storage, blocking_runner=run_inline, authentication=repo))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                authenticated = await client.post("/v1/auth/register", json={"email": "test@example.com", "password": "password123"})
                self.assertEqual(authenticated.status_code, 201)
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
