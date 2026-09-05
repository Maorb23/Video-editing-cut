from __future__ import annotations

import argparse
from fractions import Fraction
from pathlib import Path

from ..pipeline import run_pipeline
from .common import execute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run video + natural-language instruction through the standalone editing pipeline.")
    parser.add_argument("video")
    parser.add_argument("instruction")
    parser.add_argument("--output-dir", required=True, help="Fresh job directory; existing nonempty directories are refused.")
    parser.add_argument("--model", default=None, help="OpenAI model ID; defaults to VIDEO_EDIT_MODEL or gpt-5.6.")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--melt")
    parser.add_argument("--frame-rate", default="30/1", help="CFR project rate as numerator/denominator.")
    parser.add_argument("--max-analysis-frames", type=int, default=12)
    parser.add_argument("--max-repair-attempts", type=int, default=2)
    parser.add_argument("--process-timeout", type=float, default=7200.0)
    parser.add_argument("--no-progress-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    def action() -> None:
        try:
            rate = Fraction(args.frame_rate)
        except (ValueError, ZeroDivisionError) as exc:
            parser.error(f"invalid --frame-rate: {exc}")
        result = run_pipeline(
            Path(args.video), args.instruction, Path(args.output_dir),
            model_name=args.model, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, melt=args.melt,
            frame_rate=rate, max_analysis_frames=args.max_analysis_frames,
            max_repair_attempts=args.max_repair_attempts, process_timeout=args.process_timeout,
            no_progress_timeout=args.no_progress_timeout,
        )
        print(result.manifest)

    return execute(action, json_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

