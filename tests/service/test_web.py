from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from service.app import create_app
from service.config import Settings
from service.storage import FilesystemStorage
from tests.service.test_api_contract import FakeRepository, run_inline


class WebApplicationTests(IsolatedAsyncioTestCase):
    async def test_home_page_and_assets_expose_the_approval_gated_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings("unused", "filesystem", root / "objects", root / "jobs", max_upload_bytes=1024)
            transport = httpx.ASGITransport(app=create_app(settings, FakeRepository(), FilesystemStorage(settings.storage_root), blocking_runner=run_inline))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                page = await client.get("/")
                script = await client.get("/web/app.js")
                stylesheet = await client.get("/web/styles.css")

        self.assertEqual(page.status_code, 200)
        self.assertIn('id="create-edit-form"', page.text)
        self.assertIn('id="approve-plan"', page.text)
        self.assertIn('id="render-video"', page.text)
        self.assertIn('id="feedback-form"', page.text)
        self.assertIn('id="create-error"', page.text)
        self.assertNotIn("kicker", page.text.lower())
        self.assertEqual(script.status_code, 200)
        self.assertIn("awaiting_approval", script.text)
        self.assertIn("localStorage", script.text)
        self.assertIn("/v1/edits/", script.text)
        self.assertEqual(stylesheet.status_code, 200)
