"""Run inside the rebuilt worker image with an isolated PostgreSQL database.

Uses the real HTTP API, database, storage, worker, FFmpeg, FFprobe and Melt.
Only the structured language-model response is deterministic fixture data.
No API keys or external model calls are required.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import array
import math
import subprocess
import threading
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET

import uvicorn

from service.app import create_app
from service.config import Settings
from service.engine import prepare_edit
from service.repository import PostgresRepository
from service.storage import FilesystemStorage
from service.worker import Worker
import service.worker as worker_module
from video_editing.planning import ModelResponse


class RevisionModel:
    def generate(self, **kwargs):
        context = json.loads(kwargs["input_text"])
        previous = context.get("previous_plan")
        analysis = context["analysis"]
        duration = analysis["source"]["duration_frames"]
        if previous:
            tracks, operations, export = (deepcopy(previous[k]) for k in ("tracks", "operations", "export"))
            if "reduce" in context["instruction"].lower():
                operations[0]["properties"]["level"] = 0.5
                operations[1]["keyframes"][1]["geometry"] = "-10%/-10%:120%x120%"
            else:
                operations.append({"id": "fade", "type": "fade_audio", "target": "c1", "direction": "out", "start": duration - 15, "duration": 15})
        else:
            tracks = [{"id": "v1", "kind": "video", "name": "Source", "muted": False, "hidden": False,
                       "clips": [{"id": "c1", "asset_id": "source", "timeline_start": 0, "source_in": 0, "duration": duration, "enabled": True}]}]
            operations = [
                {"id": "bright", "type": "filter", "target": "c1", "name": "brightness", "properties": {"level": 0.35}},
                {"id": "zoom", "type": "transform", "target": "c1", "start": 0, "duration": duration,
                 "keyframes": [{"frame": 0, "geometry": "0%/0%:100%x100%"}, {"frame": duration // 2, "geometry": "-25%/-25%:150%x150%"}]},
            ]
            export = {"format": "mp4", "video_codec": "libx264", "audio_codec": "aac", "audio_bitrate": "128k", "pixel_format": "yuv420p", "movflags": "+faststart"}
        unsupported = ["Pitch shifting is unsupported"] if "pitch" in context["instruction"].lower() else []
        return ModelResponse({"summary": context["instruction"], "tracks": tracks, "operations": operations, "export": export,
                              "unsupported": unsupported, "decision_log": {
                                  "observations": [{"type": "visual", "description": "The fixture contains a moving test pattern.",
                                                    "evidence": [analysis["observations"][0]["evidence_id"]], "confidence": 1.0}],
                                  "decisions": [{"request": context["instruction"], "operation": "transform",
                                                 "reason": "Use a crop to demonstrate the requested zoom strength.", "confidence": 1.0}],
                                  "unsupported": unsupported, "assumptions": ["Synthetic fixture validates mechanics, not semantic subject recognition."]}},
                             {"provider": "deterministic-iteration-test"})


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root = Path(os.environ.get("VIDEO_EDIT_E2E_ROOT", "/tmp/iteration-e2e")) / uuid.uuid4().hex
    root.mkdir(parents=True)
    database = os.environ["VIDEO_EDIT_TEST_DATABASE_URL"]
    repo = PostgresRepository(database)
    repo.migrate()
    settings = Settings(database, "filesystem", root / "objects", root / "jobs")
    storage = FilesystemStorage(settings.storage_root)
    source = root / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=duration=2:size=320x180:rate=30000/1001",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=48000", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(source)], check=True)
    original_digest = digest(source)
    worker_module.prepare_edit = lambda *args, **kwargs: prepare_edit(*args, model=RevisionModel(), max_analysis_frames=4, **kwargs)
    worker = Worker(repo, storage, settings, worker_id=f"e2e-{uuid.uuid4().hex}")
    app = create_app(settings, repo, storage)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started

    def api(path, body=None, *, data=None, headers=None):
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(base + path, data=data, headers=headers or {})
        with urllib.request.urlopen(request, timeout=30) as response:
            value = response.read()
            return json.loads(value) if "application/json" in response.headers.get("Content-Type", "") else value

    def run_stage(kind):
        assert worker.run_once(), f"no job for {kind}"
        current = repo.get_edit(edit_id)
        failed = [j for j in current["jobs"] if j["status"] == "failed"]
        assert not failed, failed
        print(json.dumps({"stage": kind, "iteration": current["current_iteration"], "state": current["state"], "preview": current["preview_status"]}), flush=True)

    def snapshot(directory):
        return {str(p.relative_to(directory)): digest(p) for p in directory.rglob("*") if p.is_file()}

    try:
        boundary = "iteration-test-boundary"
        upload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="source.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode()
                  + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode())
        video = api("/v1/videos", data=upload, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        edit = api("/v1/edits", {"video_id": video["id"], "instruction": "Brighten by 0.35 and zoom"})
        edit_id = edit["id"]
        assert edit["iteration"] == 1
        outputs = []
        frozen = {}
        metrics = []
        for number in (1, 2, 3):
            if number > 1:
                instruction = "Reduce zoom and brighten more" if number == 2 else "Add an audio fade-out and shift pitch"
                revised = api(f"/v1/edits/{edit_id}/revise", {"instruction": instruction})
                assert revised["iteration"] == number
            run_stage("planning" if number == 1 else "revision")
            run_stage("compilation")
            plan = api(f"/v1/edits/{edit_id}/plan")
            assert plan["iteration"] == number and plan["decision_log"]["decisions"]
            assert plan["edit_plan"]["profile"]["frame_rate"] == {"numerator": 30000, "denominator": 1001}
            run_stage("preview")
            run_stage("inspection")
            plan = api(f"/v1/edits/{edit_id}/plan")
            assert plan["preview_status"] == "succeeded"
            preview_bytes = api(plan["preview_url"])
            assert len(preview_bytes) > 1000 and preview_bytes != source.read_bytes()
            assert api(plan["poster_url"]).startswith(b"\x89PNG")
            report = api(plan["inspection_url"])
            assert report["audio"]["present"] and report["frames"]
            assert any(f["code"] == "transform_applied" for f in report["automated_findings"])
            workspace = root / "jobs" / edit_id / "iterations" / f"{number:03d}"
            outputs.append(workspace / "preview.mp4")
            tree = ET.parse(workspace / "project.mlt")
            level = tree.find("./producer/filter[@id='ves_filter_bright']/property[@name='level']").text
            assert level == ("1.35" if number == 1 else "1.5")
            rect = tree.find("./producer/filter[@id='ves_filter_zoom']/property[@name='transition.rect']").text
            assert ("150%x150%" if number == 1 else "120%x120%") in rect
            if number == 2:
                api(f"/v1/edits/{edit_id}/approve", {"plan_id": plan["plan_id"]})
                api(f"/v1/edits/{edit_id}/render", data=b"")
                run_stage("final rendering")
                result = api(f"/v1/edits/{edit_id}/result")
                assert len(api(result["video_url"])) > 1000
                assert (workspace / "result.json").is_file()
            if number == 3:
                assert plan["decision_log"]["unsupported"] == ["Pitch shifting is unsupported"]
                assert len(plan["edit_plan"]["operations"]) == 3
            for previous, hashes in frozen.items():
                assert snapshot(previous) == hashes, "previous artifacts changed"
            frozen[workspace] = snapshot(workspace)
            metrics.append({"iteration": number, "brightness_level": level, "transform_rect": rect, "preview_sha256": digest(outputs[-1])})
        assert len({digest(p) for p in outputs}) == 3
        assert digest(source) == original_digest
        assert digest(root / "objects" / "videos" / video["id"] / "source.mp4") == original_digest
        assert api(f"/v1/edits/{edit_id}")["active_iteration"] == 3
        history = api(f"/v1/edits/{edit_id}/iterations")["iterations"]
        assert [i["parent_iteration"] for i in history] == [None, 1, 2]
        # At frame zero zoom is identity, isolating brightness from geometry.
        def luma(path):
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "-"], check=True, capture_output=True).stdout
            return sum(raw) / len(raw)
        luminance = [luma(p) for p in [source, *outputs]]
        assert luminance[1] > luminance[0] + 5 and luminance[2] > luminance[1] + 2, luminance
        def audio_rms(path, start):
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path), "-t", "0.15", "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"], check=True, capture_output=True).stdout
            samples = array.array("f", raw)
            return math.sqrt(sum(v * v for v in samples) / len(samples))
        audio = [audio_rms(p, 0.2) for p in [source, *outputs]]
        assert all(value > 0.01 for value in audio), audio
        tail = [audio_rms(p, 1.8) for p in outputs]
        assert tail[2] < tail[1] * 0.6, tail
        summary = {"status": "passed", "edit_id": edit_id, "root": str(root), "source_sha256": original_digest,
                   "iterations": metrics, "frame_zero_luma": luminance, "audio_preserved": True,
                   "audio_rms": audio, "tail_audio_rms": tail,
                   "prior_artifacts_unchanged": True, "model": "deterministic fixture", "melt": str(worker._toolchain().melt)}
        with (root / "verification.json").open("x", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2)
        print(json.dumps(summary), flush=True)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


if __name__ == "__main__":
    main()
