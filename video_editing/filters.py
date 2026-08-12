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


FILTER_CATALOG_VERSION = "mlt-7.28-shotcut-24.06"
FILTERS: dict[str, FilterSpec] = {
    "brightness": FilterSpec("brightness", {"level": (int, float)}, _identity),
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

