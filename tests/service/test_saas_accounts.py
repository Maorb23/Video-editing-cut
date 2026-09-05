from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from service.app import create_app
from service.config import Settings
from service.storage import FilesystemStorage
from tests.service.test_api_contract import FakeRepository, run_inline


class SaasAccountTests(IsolatedAsyncioTestCase):
    async def make_client(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        settings = Settings("unused", "filesystem", root / "objects", root / "jobs", max_upload_bytes=1024)
        repo = FakeRepository()
        app = create_app(settings, repo, FilesystemStorage(settings.storage_root), blocking_runner=run_inline, authentication=repo)
        return temporary, repo, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    async def test_verification_avatar_dashboard_and_protected_routes(self) -> None:
        temporary, repo, client = await self.make_client()
        try:
            async with client:
                self.assertEqual((await client.get("/v1/account")).status_code, 401)
                user = (await client.post("/v1/auth/register", json={"email": "a@example.com", "password": "password123"})).json()
                self.assertFalse(user["email_verified"])
                token = repo.issue_token(user, "verify-email")
                self.assertEqual((await client.post("/v1/auth/verify-email", json={"token": token})).status_code, 200)
                changed = await client.put("/v1/account/avatar", json={"avatar_key": "clapperboard"})
                self.assertEqual(changed.json()["avatar_key"], "clapperboard")
                account = (await client.get("/v1/account")).json()
                self.assertTrue(account["user"]["email_verified"])
                self.assertEqual(account["credits"]["balance"], 200)
                forgot = await client.post("/v1/auth/forgot-password", json={"email": "missing@example.com"})
                self.assertEqual(forgot.status_code, 202)
                reset_token = repo.issue_token(user, "password-reset")
                self.assertEqual((await client.post("/v1/auth/reset-password", json={"token": reset_token, "password": "replacement123"})).status_code, 200)
        finally:
            temporary.cleanup()

    async def test_mock_top_up_is_server_priced_and_failure_adds_no_credit(self) -> None:
        temporary, _, client = await self.make_client()
        try:
            async with client:
                await client.post("/v1/auth/register", json={"email": "b@example.com", "password": "password123"})
                rejected = await client.post("/v1/billing/mock-top-ups", json={"package_key": "small", "simulate": "success", "credits": 999999})
                self.assertEqual(rejected.status_code, 422)
                failed = await client.post("/v1/billing/mock-top-ups", json={"package_key": "small", "simulate": "failure"})
                self.assertEqual(failed.json()["credits"]["balance"], 200)
                success = await client.post("/v1/billing/mock-top-ups", json={"package_key": "small", "simulate": "success"})
                self.assertEqual(success.json()["credits"]["balance"], 450)
        finally:
            temporary.cleanup()

    async def test_signup_rate_limit(self) -> None:
        temporary, _, client = await self.make_client()
        try:
            async with client:
                statuses = []
                for index in range(6):
                    response = await client.post("/v1/auth/register", json={"email": f"rate{index}@example.com", "password": "password123"})
                    statuses.append(response.status_code)
                self.assertEqual(statuses[-1], 429)
        finally:
            temporary.cleanup()
