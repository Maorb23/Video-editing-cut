"""Frame-accurate rational time conversion."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .errors import VideoEditingError


def frame_rate(numerator: int, denominator: int) -> Fraction:
    if isinstance(numerator, bool) or isinstance(denominator, bool):
        raise VideoEditingError("frame-rate values must be integers", code="invalid_frame_rate")
    if numerator <= 0 or denominator <= 0:
        raise VideoEditingError("frame rate numerator and denominator must be positive", code="invalid_frame_rate")
    return Fraction(numerator, denominator)


def parse_seconds(value: Any) -> Fraction:
    """Parse seconds without passing through binary floating point."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise VideoEditingError("seconds must be a number or rational string", code="invalid_time")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value))
    if isinstance(value, str):
        try:
            return Fraction(value.strip())
        except (ValueError, ZeroDivisionError) as exc:
            raise VideoEditingError(f"invalid seconds value: {value!r}", code="invalid_time") from exc
    raise VideoEditingError(f"unsupported seconds value: {value!r}", code="invalid_time")


def seconds_to_frames(value: Any, numerator: int, denominator: int, *, rounding: str = "nearest") -> int:
    exact = parse_seconds(value) * frame_rate(numerator, denominator)
    if rounding == "floor":
        return exact.numerator // exact.denominator
    if rounding == "ceil":
        return -(-exact.numerator // exact.denominator)
    if rounding != "nearest":
        raise VideoEditingError(f"unsupported rounding mode: {rounding}", code="invalid_rounding")
    quotient, remainder = divmod(abs(exact.numerator), exact.denominator)
    rounded = quotient + (1 if remainder * 2 >= exact.denominator else 0)
    return rounded if exact >= 0 else -rounded


def frames_to_mlt_time(frames: int, numerator: int, denominator: int) -> str:
    """Return an exact MLT clock string at microsecond precision."""
    if frames < 0:
        raise VideoEditingError("frame count cannot be negative", code="invalid_time")
    seconds = Fraction(frames * denominator, numerator)
    hours, remainder = divmod(seconds, 3600)
    minutes, remainder = divmod(remainder, 60)
    micros = (remainder * 1_000_000).__round__()
    whole_seconds, microseconds = divmod(micros, 1_000_000)
    return f"{int(hours):02d}:{int(minutes):02d}:{whole_seconds:02d}.{microseconds:06d}"
