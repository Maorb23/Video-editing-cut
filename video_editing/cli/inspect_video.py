from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..inspect import check_inspection, inspect
from .common import execute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract frame-exact visual/audio evidence or verify a completed inspection pass.")
    parser.add_argument("video", nargs="?")
    parser.add_argument("--edit-plan")
    parser.add_argument("--output-dir")
    parser.add_argument("--check-record")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    args = parser.parse_args(argv)

    def action() -> None:
        if args.check_record:
            if args.video or args.edit_plan or args.output_dir:
                parser.error("--check-record cannot be combined with inspection inputs")
            print(json.dumps(check_inspection(Path(args.check_record)), indent=2, ensure_ascii=False))
            return
        if not args.video or not args.edit_plan or not args.output_dir:
            parser.error("video, --edit-plan, and --output-dir are required for a new inspection")
        result = inspect(Path(args.video), Path(args.edit_plan), Path(args.output_dir), ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
        print(result)
    return execute(action)


if __name__ == "__main__":
    raise SystemExit(main())
