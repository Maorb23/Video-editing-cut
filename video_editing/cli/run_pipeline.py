from __future__ import annotations

import argparse
from fractions import Fraction
from pathlib import Path

from ..pipeline import run_pipeline
from ..adaptive_silence import SilenceSettings
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
    parser.add_argument("--minimum-silence-seconds", type=float, default=.25)
    parser.add_argument("--speech-padding-seconds", type=float, default=.12)
    parser.add_argument("--silence-threshold-min-db", type=float, default=-60.)
    parser.add_argument("--silence-threshold-max-db", type=float, default=-30.)
    parser.add_argument("--silence-calibration-margin-db", type=float, default=8.)
    parser.add_argument("--silence-fallback-threshold-db", type=float, default=-50.)
    parser.add_argument("--max-repair-attempts", type=int, default=2)
    parser.add_argument("--process-timeout", type=float, default=7200.0)
    parser.add_argument("--no-progress-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    def action() -> None:
        try:
            rate = Fraction(args.frame_rate)
        except (ValueError, ZeroDivisionError) as exc:
            parser.error(f"invalid --frame-rate: {exc}")
        silence_settings = SilenceSettings(
            minimum_silence_seconds=args.minimum_silence_seconds,
            speech_padding_seconds=args.speech_padding_seconds,
            threshold_min_db=args.silence_threshold_min_db,
            threshold_max_db=args.silence_threshold_max_db,
            calibration_margin_db=args.silence_calibration_margin_db,
            fallback_threshold_db=args.silence_fallback_threshold_db,
        )
        result = run_pipeline(
            Path(args.video), args.instruction, Path(args.output_dir),
            model_name=args.model, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, melt=args.melt,
            frame_rate=rate, max_analysis_frames=args.max_analysis_frames,
            silence_settings=silence_settings,
            max_repair_attempts=args.max_repair_attempts, process_timeout=args.process_timeout,
            no_progress_timeout=args.no_progress_timeout,
        )
        print(result.manifest)

    return execute(action, json_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
