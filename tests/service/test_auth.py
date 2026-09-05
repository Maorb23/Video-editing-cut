from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
pytest.importorskip("multipart")

from service.app import create_app
from service.config import Settings
from service.storage import FilesystemStorage
from tests.service.test_api_contract import FakeRepository, run_inline


class AuthenticationTests(IsolatedAsyncioTestCase):
    async def test_registration_login_logout_and_owner_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings("unused", "filesystem", root / "objects", root / "jobs", max_upload_bytes=1024, session_cookie_secure=False)
            repo = FakeRepository()
            transport = httpx.ASGITransport(app=create_app(settings, repo, FilesystemStorage(settings.storage_root), blocking_runner=run_inline, authentication=repo))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as owner, httpx.AsyncClient(transport=transport, base_url="http://testserver") as other:
                self.assertEqual((await owner.get("/v1/auth/me")).status_code, 401)
                registered = await owner.post("/v1/auth/register", json={"email": "owner@example.com", "password": "password123"})
                self.assertEqual(registered.status_code, 201)
                self.assertIn("httponly", registered.headers["set-cookie"].lower())
                self.assertNotIn("secure", registered.headers["set-cookie"].lower())
                self.assertEqual((await owner.get("/v1/auth/me")).json()["email"], "owner@example.com")
                video = await owner.post("/v1/videos", files={"file": ("clip.mp4", b"media", "video/mp4")})
                edit = await owner.post("/v1/edits", json={"video_id": video.json()["id"], "instruction": "Keep the action"})
                edit_id = edit.json()["id"]
                self.assertEqual((await owner.get(f"/v1/edits/{edit_id}")).status_code, 200)
                self.assertEqual((await other.post("/v1/auth/register", json={"email": "other@example.com", "password": "password123"})).status_code, 201)
                self.assertEqual((await other.get(f"/v1/edits/{edit_id}")).status_code, 404)
                self.assertEqual((await owner.post("/v1/auth/logout")).status_code, 204)
                self.assertEqual((await owner.get("/v1/auth/me")).status_code, 401)
                logged_in = await owner.post("/v1/auth/login", json={"email": "owner@example.com", "password": "password123"})
                self.assertEqual(logged_in.status_code, 200)
                self.assertEqual((await owner.get("/v1/auth/me")).status_code, 200)

    async def test_secure_cookie_can_be_enabled_for_https_deployments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings("unused", "filesystem", root / "objects", root / "jobs", max_upload_bytes=1024, session_cookie_secure=True)
            repo = FakeRepository()
            transport = httpx.ASGITransport(app=create_app(settings, repo, FilesystemStorage(settings.storage_root), blocking_runner=run_inline, authentication=repo))
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
                response = await client.post("/v1/auth/register", json={"email": "secure@example.com", "password": "password123"})
                self.assertIn("secure", response.headers["set-cookie"].lower())
                self.assertEqual((await client.get("/v1/auth/me")).status_code, 200)
