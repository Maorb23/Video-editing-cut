from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..plan import load_plan
from .common import execute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strictly validate an edit-plan JSON file.")
    parser.add_argument("edit_plan")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-check-files", action="store_true", help="schema-only validation")
    args = parser.parse_args(argv)

    def action() -> None:
        load_plan(Path(args.edit_plan), check_files=not args.no_check_files)
        if args.json:
            print(json.dumps({"ok": True, "path": str(Path(args.edit_plan))}))
        else:
            print(f"valid: {args.edit_plan}")
    return execute(action, json_errors=args.json)


if __name__ == "__main__":
    raise SystemExit(main())

