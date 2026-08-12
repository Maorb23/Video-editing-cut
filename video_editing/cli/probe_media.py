from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..errors import VideoEditingError
from ..probe import create_manifest
from .common import execute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe media and create a fingerprinted manifest.")
    parser.add_argument("assets", nargs="+")
    parser.add_argument("--output", required=True)
    parser.add_argument("--ffprobe")
    args = parser.parse_args(argv)

    def action() -> None:
        output = Path(args.output)
        if output.exists():
            raise VideoEditingError(f"refusing to overwrite manifest: {output}", code="output_exists")
        manifest = create_manifest([Path(value) for value in args.assets], ffprobe=args.ffprobe)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(output)
    return execute(action)


if __name__ == "__main__":
    raise SystemExit(main())

