"""A local review-only HTTP viewer."""

from __future__ import annotations

import functools
import http.server
import shutil
import threading
import webbrowser
from pathlib import Path

from .errors import VideoEditingError


def prepare_viewer(video: Path, project: Path, plan: Path, template: Path, destination: Path) -> None:
    for source in (video, project, plan, template):
        if not source.is_file():
            raise VideoEditingError(f"viewer input not found: {source}", code="missing_file")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, destination / "video.mp4")
    shutil.copy2(project, destination / "project.mlt")
    shutil.copy2(plan, destination / "plan.md")
    shutil.copy2(template, destination / "index.html")


def serve(directory: Path, *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with http.server.ThreadingHTTPServer((host, port), handler) as server:
        address = f"http://{host}:{server.server_port}/"
        print(address, flush=True)
        if open_browser:
            threading.Timer(0.25, webbrowser.open, args=(address,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

