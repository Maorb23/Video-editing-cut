from __future__ import annotations

import argparse
from pathlib import Path

from ..viewer import prepare_viewer, serve
from .common import execute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a local, review-only video viewer.")
    parser.add_argument("video")
    parser.add_argument("--project", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--viewer-dir", default="review/viewer")
    args = parser.parse_args(argv)
    template = Path(__file__).resolve().parents[2] / "plugins/video-editing-skill/skills/video-editing-skill/assets/viewer.html"

    def action() -> None:
        directory = Path(args.viewer_dir)
        prepare_viewer(Path(args.video), Path(args.project), Path(args.plan), template, directory)
        serve(directory, host=args.host, port=args.port, open_browser=not args.no_open)
    return execute(action)


if __name__ == "__main__":
    raise SystemExit(main())
