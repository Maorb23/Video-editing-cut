from __future__ import annotations

import argparse
from pathlib import Path

from ..render import render
from .common import execute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render an MLT project to MP4 through melt.")
    parser.add_argument("project")
    parser.add_argument("--output", required=True)
    parser.add_argument("--quality", choices=("preview", "final"), default="final")
    parser.add_argument("--melt")
    args = parser.parse_args(argv)
    return execute(lambda: render(Path(args.project), Path(args.output), quality=args.quality, melt=args.melt))


if __name__ == "__main__":
    raise SystemExit(main())

