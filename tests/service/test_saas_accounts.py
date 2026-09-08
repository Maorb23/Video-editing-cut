from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
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

    async def test_paddle_checkout_requires_verified_matching_webhook_and_is_idempotent(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        prices = {"small": "pri_" + "a" * 26, "medium": "pri_" + "b" * 26, "large": "pri_" + "c" * 26}
        secret = "pdl_ntfset_test_secret"
        settings = Settings("unused", "filesystem", root / "objects", root / "jobs", max_upload_bytes=1024,
            paddle_environment="sandbox", paddle_client_token="test_client_token", paddle_webhook_secret=secret,
            paddle_price_starter=prices["small"], paddle_price_creator=prices["medium"], paddle_price_studio=prices["large"])
        repo = FakeRepository()
        app = create_app(settings, repo, FilesystemStorage(settings.storage_root), blocking_runner=run_inline, authentication=repo)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                await client.post("/v1/auth/register", json={"email": "pay@example.com", "password": "password123"})
                packages = (await client.get("/v1/billing/packages")).json()
                self.assertEqual(packages["mode"], "paddle")
                self.assertEqual([item["price_minor"] for item in packages["packages"]], [500, 1000, 2000])
                checkout = await client.post("/v1/billing/paddle/checkouts", json={"package_key": "small"})
                self.assertEqual(checkout.status_code, 201)
                order_id = checkout.json()["order"]["id"]
                timestamp = int(time.time())
                event = {"event_id": "evt_test", "event_type": "transaction.completed", "data": {
                    "id": "txn_test", "status": "completed", "subscription_id": None, "currency_code": "USD",
                    "custom_data": {"melvid_order_id": order_id},
                    "items": [{"quantity": 1, "price": {"id": prices["small"]}}],
                }}
                raw = json.dumps(event, separators=(",", ":")).encode()
                signature = hmac.new(secret.encode(), str(timestamp).encode() + b":" + raw, hashlib.sha256).hexdigest()
                headers = {"Paddle-Signature": f"ts={timestamp};h1={signature}", "Content-Type": "application/json"}
                first = await client.post("/v1/billing/paddle/webhook", content=raw, headers=headers)
                replay = await client.post("/v1/billing/paddle/webhook", content=raw, headers=headers)
                self.assertEqual((first.status_code, replay.status_code), (200, 200))
                self.assertEqual((await client.get("/v1/billing/credits")).json()["balance"], 450)
                self.assertEqual(len([item for item in repo.ledger[next(iter(repo.ledger))] if item["reason"] == "purchase"]), 1)
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

    async def test_staff_edits_are_explicitly_unmetered(self) -> None:
        temporary, repo, client = await self.make_client()
        try:
            async with client:
                await client.post("/v1/auth/register", json={"email": "admin@example.com", "password": "password123"})
                repo.users["admin@example.com"]["is_staff"] = True
                account = (await client.get("/v1/account")).json()
                self.assertTrue(account["user"]["is_admin"])
                self.assertEqual(account["credits"]["mode"], "unmetered")
                video = await client.post("/v1/videos", files={"file": ("clip.mp4", b"media", "video/mp4")})
                edit = await client.post("/v1/edits", json={"video_id": video.json()["id"], "instruction": "Keep the action"})
                self.assertEqual(edit.status_code, 202)
                self.assertTrue(repo.edit["billing_exempt"])
        finally:
            temporary.cleanup()
