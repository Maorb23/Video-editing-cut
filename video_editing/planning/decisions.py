"""Public rationale schema. Deliberately contains no private reasoning fields."""
from __future__ import annotations

from typing import Any

from ..errors import VideoEditingError


def decision_schema() -> dict[str, Any]:
    text = {"type": "string", "maxLength": 2048}
    confidence = {"type": "number", "minimum": 0, "maximum": 1}

    def record(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    schema = record({
        "observations": {"type": "array", "maxItems": 64, "items": record({
            "type": {"type": "string", "enum": ["visual", "audio", "metadata"]},
            "description": text, "evidence": {"type": "array", "items": text, "maxItems": 24}, "confidence": confidence,
        })},
        "decisions": {"type": "array", "maxItems": 128, "items": record({
            "request": text, "operation": text, "reason": text, "confidence": confidence,
        })},
        "unsupported": {"type": "array", "items": text, "maxItems": 32},
        "assumptions": {"type": "array", "items": text, "maxItems": 32},
    })
    schema["properties"]["measurements"] = {"type": "array", "maxItems": 256, "items": record({
        "type": {"type": "string", "enum": ["silence_interval", "eq_gain", "reverb_tail", "dereverb"]},
        "operation_id": text, "value": text, "unit": text,
        "evidence": {"type": "array", "items": text, "maxItems": 24},
    })}
    schema["required"].append("measurements")
    return schema


def validate_decisions(value: Any, evidence: set[str]) -> dict[str, Any]:
    original = value
    if isinstance(value, dict) and "measurements" not in value:
        value = {**value, "measurements": []}
    def check(item, schema):
        kind = schema["type"]
        if kind == "object":
            valid = (isinstance(item, dict) and set(schema.get("required", ())) <= set(item)
                     and set(item) <= set(schema["properties"]))
            if valid:
                for key, child in item.items():
                    check(child, schema["properties"][key])
        elif kind == "array":
            valid = isinstance(item, list) and len(item) <= schema["maxItems"]
            if valid:
                for child in item:
                    check(child, schema["items"])
        elif kind == "number":
            valid = type(item) in (int, float) and 0 <= item <= 1
        else:
            valid = isinstance(item, str) and len(item) <= schema.get("maxLength", 2048) and ("enum" not in schema or item in schema["enum"])
        if not valid:
            raise VideoEditingError("invalid public decision log", code="model_invalid_response")
    check(value, decision_schema())
    for observation in value["observations"]:
        if not set(observation["evidence"]) <= evidence:
            raise VideoEditingError("decision log references unknown evidence", code="model_invalid_response")
    for measurement in value.get("measurements", []):
        if not set(measurement["evidence"]) <= evidence:
            raise VideoEditingError("decision log references unknown evidence", code="model_invalid_response")
    return original
