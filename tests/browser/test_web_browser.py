from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from service.app import create_app
from service.config import Settings
from service.storage import FilesystemStorage
from tests.service.test_api_contract import FakeRepository, run_inline


ROOT = Path(__file__).parents[2]


def browser_executable() -> str | None:
    return os.environ.get("VIDEO_EDIT_BROWSER_EXECUTABLE")


class WebBrowserTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        settings = Settings("unused", "filesystem", root / "objects", root / "jobs", max_upload_bytes=1024)
        application = create_app(settings, FakeRepository(), FilesystemStorage(settings.storage_root), blocking_runner=run_inline)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(5)
        port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(application, log_level="error"))
        self.server_thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.server_thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.02)
        if not self.server.started:
            self.fail("browser test server did not start")
        self.page_url = f"http://127.0.0.1:{port}/"
        self.playwright = sync_api.sync_playwright().start()
        executable = browser_executable()
        options = {"executable_path": executable} if executable else {}
        endpoint = os.environ.get("VIDEO_EDIT_BROWSER_CDP")
        self.browser = self.playwright.chromium.connect_over_cdp(endpoint) if endpoint else self.playwright.chromium.launch(headless=True, **options)
        self.page = self.browser.new_page()

    def tearDown(self) -> None:
        self.browser.close()
        self.playwright.stop()
        self.server.should_exit = True
        self.server_thread.join(timeout=5)
        self.socket.close()
        self.temporary_directory.cleanup()

    def install_api(self, *, edit_state: str = "awaiting_approval", upload_status: int = 201) -> None:
        script = f"""
        (() => {{
          const editId = 'edt_{'2' * 32}';
          const planId = 'pln_{'3' * 32}';
          let state = {edit_state!r};
          let iteration = 1;
          window.approvedPlans = [];
          window.fetch = async (path, options = {{}}) => {{
            const url = String(path);
            const json = (body, status = 200) => new Response(JSON.stringify(body), {{
              status, headers: {{'Content-Type': 'application/json'}}
            }});
            if (url === '/v1/videos') return json({{'id': 'vid_{'1' * 32}', 'state': 'uploaded', 'filename': 'clip.mp4'}}, {upload_status});
            if (url === '/v1/edits' && options.method === 'POST') return json({{'id': editId, 'state': 'analyzing', 'created_at': '2026-09-04T00:00:00Z'}});
            if (url.endsWith('/iterations')) return json({{iterations: Array.from({{length: iteration}}, (_, i) => ({{iteration: i + 1, plan_id: planId, preview_status: 'succeeded'}}))}});
            if (url.endsWith('/plan')) return json({{
              edit_id: editId, plan_id: planId, iteration: Number(url.split('/').at(-2)), plan_status: 'awaiting_approval', preview_status: 'succeeded', status: 'proposed', summary: 'Short highlight', warnings: ['Caption position needs review'], edit_plan: {{version: '1.0'}},
              preview_url: 'https://example.test/preview.mp4', poster_url: 'https://example.test/poster.png',
              decision_log: {{observations: [{{description: 'Subject on the right', confidence: 0.82, evidence: ['frame-580.png']}}], decisions: [{{request: 'focus', operation: 'transform', reason: 'Subject visible', confidence: 0.78}}], unsupported: ['Pitch shifting'], assumptions: ['Sampled evidence is sufficient']}}
            }});
            if (url.endsWith('/revise')) {{ iteration++; state = 'awaiting_approval'; return json({{id: editId, iteration, state}}); }}
            if (url.endsWith('/approve')) {{ window.approvedPlans.push(JSON.parse(options.body).plan_id); state = 'approved'; return json({{'id': editId, state, 'created_at': '2026-09-04T00:00:00Z'}}); }}
            if (url.endsWith('/render')) {{ state = 'rendering'; return json({{'id': editId, state, 'created_at': '2026-09-04T00:00:00Z'}}); }}
            if (url.endsWith('/result')) return json({{edit_id: editId, state: 'completed', video_url: 'https://example.test/output.mp4', artifacts: {{}}}});
            if (url.endsWith(editId)) return json({{'id': editId, state, iteration, 'created_at': '2026-09-04T00:00:00Z', progress: {{stage: state}}, error: state === 'failed' ? {{message: 'Renderer failed'}} : null}});
            return json({{error: {{message: 'Unexpected request'}}}}, 404);
          }};
        }})();
        """
        self.page.add_init_script(script)

    def create_edit(self) -> None:
        self.page.goto(self.page_url)
        self.page.set_input_files("#video-file", {"name": "clip.mp4", "mimeType": "video/mp4", "buffer": b"media"})
        self.page.fill("#instruction", "Keep the action")
        self.page.click("#create-edit")

    def test_upload_plan_approval_and_render_controls(self) -> None:
        self.install_api()
        self.create_edit()
        self.page.locator("#plan-panel").wait_for(state="visible")
        self.assertIn("Short highlight", self.page.locator("#plan-summary").inner_text())
        self.assertIn("Caption position needs review", self.page.locator("#plan-warnings").inner_text())
        self.page.click("#approve-plan")
        sync_api.expect(self.page.locator("#status-message")).to_contain_text("Rendering")

    def test_revisions_history_preview_and_public_decisions(self) -> None:
        self.install_api()
        self.create_edit()
        sync_api.expect(self.page.locator("#preview-video")).to_be_visible()
        sync_api.expect(self.page.locator("#observations")).to_contain_text("82% confidence")
        sync_api.expect(self.page.locator("#unsupported")).to_contain_text("Pitch shifting")
        self.page.fill("#revision-instruction", "Reduce zoom")
        self.page.click("#request-changes")
        sync_api.expect(self.page.locator("#iteration-selector")).to_have_value("2")
        self.page.select_option("#iteration-selector", "1")
        sync_api.expect(self.page.locator("#iteration-status")).to_contain_text("Iteration 001")
        self.page.click("#approve-plan")
        sync_api.expect(self.page.locator("#status-message")).to_contain_text("Rendering")
        self.assertEqual(len(self.page.evaluate("window.approvedPlans")), 1)

    def test_refresh_recovery_playback_download_feedback_and_accessibility(self) -> None:
        self.install_api(edit_state="completed")
        self.page.add_init_script("localStorage.setItem('video-editing-web-session-v1', JSON.stringify({editId: 'edt_' + '2'.repeat(32)}));")
        self.page.goto(self.page_url)
        self.page.locator("#result-panel").wait_for(state="visible")
        self.assertEqual(self.page.locator("#result-video").get_attribute("src"), "https://example.test/output.mp4")
        self.assertEqual(self.page.locator("#download-video").get_attribute("href"), "https://example.test/output.mp4")
        self.page.check("input[value='wrong_result']")
        self.page.fill("#feedback-comment", "Caption was late")
        self.page.click("#feedback-form button")
        events = self.page.evaluate("JSON.parse(localStorage.getItem('video-editing-web-events-v1'))")
        self.assertTrue(any(event["event"] == "feedback" and event["outcome"] == "wrong_result" for event in events))
        self.assertEqual(self.page.locator("label[for='video-file']").inner_text(), "Video file")
        self.assertEqual(self.page.locator("#create-edit-heading").evaluate("element => element.tagName"), "H1")

    def test_upload_limit_and_failed_job_are_presented(self) -> None:
        self.install_api(upload_status=413)
        self.create_edit()
        self.page.locator("#create-error").wait_for(state="visible")
        self.assertIn("request could not be completed", self.page.locator("#create-error").inner_text().lower())

        self.page = self.browser.new_page()
        self.install_api(edit_state="failed")
        self.page.add_init_script("localStorage.setItem('video-editing-web-session-v1', JSON.stringify({editId: 'edt_' + '2'.repeat(32)}));")
        self.page.goto(self.page_url)
        self.page.locator("#failure-panel").wait_for(state="visible")
        self.assertIn("Renderer failed", self.page.locator("#failure-message").inner_text())
