from __future__ import annotations

import argparse
import json

from ..environment import check_environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover MLT, FFmpeg, and FFprobe without installing anything.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    result = check_environment()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for name, tool in result["tools"].items():
            print(f"{name}: {tool['path'] or 'NOT FOUND'}")
            if tool["version"]:
                print(f"  {tool['version']}")
        print(result["guidance"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

