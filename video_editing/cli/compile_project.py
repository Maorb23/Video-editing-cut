from __future__ import annotations

import argparse
from pathlib import Path

from ..mlt import write_mlt
from ..plan import load_plan
from .common import execute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a validated edit plan to Shotcut-compatible MLT XML.")
    parser.add_argument("edit_plan")
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-project")
    args = parser.parse_args(argv)

    def action() -> None:
        plan = load_plan(Path(args.edit_plan))
        output = Path(args.output)
        write_mlt(plan, output, base_project=Path(args.base_project) if args.base_project else None)
        print(output)
    return execute(action)


if __name__ == "__main__":
    raise SystemExit(main())

