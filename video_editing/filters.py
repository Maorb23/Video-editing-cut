"""Version-pinned, allow-listed mappings from plan filters to MLT services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class FilterSpec:
    service: str
    properties: dict[str, tuple[type, ...]]
    transform: Callable[[dict[str, Any]], dict[str, str]]


def _identity(properties: dict[str, Any]) -> dict[str, str]:
    return {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in properties.items()}


def _brightness(properties: dict[str, Any]) -> dict[str, str]:
    """Map the plan's fractional brightness amount to MLT's multiplicative level.

    MLT's ``brightness`` filter uses 1.0 as neutral (values below 1 darken).
    Plans produced from natural language commonly express a positive amount
    such as ``0.35`` as a 35% increase, so convert fractional amounts to the
    corresponding MLT level while retaining already-native levels above 1.
    """
    value = properties["level"]
    level = 1.0 + value if -1 <= value <= 1 else value
    return {"level": str(level)}


FILTER_CATALOG_VERSION = "mlt-7.28-shotcut-24.06"
FILTERS: dict[str, FilterSpec] = {
    "brightness": FilterSpec("brightness", {"level": (int, float)}, _brightness),
    "contrast": FilterSpec("frei0r.contrast0r", {"contrast": (int, float)}, _identity),
    "saturation": FilterSpec("frei0r.saturat0r", {"saturation": (int, float)}, _identity),
    "blur": FilterSpec("frei0r.IIRblur", {"amount": (int, float)}, _identity),
    "sharpen": FilterSpec("frei0r.sharpness", {"amount": (int, float)}, _identity),
    "grayscale": FilterSpec("greyscale", {}, _identity),
    "sepia": FilterSpec("sepia", {"u": (int, float), "v": (int, float)}, _identity),
    "white_balance": FilterSpec("frei0r.colgate", {"temperature": (int, float)}, _identity),
}


OPERATION_SERVICES: dict[str, str] = {
    "transition": "luma",
    "caption": "dynamictext",
    "overlay": "affine",
    "transform": "affine",
    "volume": "volume",
    "fade_audio": "volume",
    "audio_mix": "mix",
    "speed": "timewarp",
    "chroma_key": "frei0r.bluescreen0r",
    "mask": "shape",
    "filter": "catalog",
}
