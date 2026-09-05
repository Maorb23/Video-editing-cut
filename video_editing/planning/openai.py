"""Dependency-free OpenAI Responses API structured-output adapter."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..errors import ExternalToolError, VideoEditingError
from .base import ModelResponse


class OpenAIResponsesModel:
    MAX_IMAGES = 24
    MAX_IMAGE_BYTES = 2 * 1024 * 1024
    MAX_INPUT_BYTES = 4 * 1024 * 1024
    MAX_SCHEMA_BYTES = 256 * 1024
    MAX_OUTPUT_TOKENS = 16_384
    MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        endpoint: str = "https://api.openai.com/v1/responses",
        timeout: float = 180.0,
        max_output_tokens: int = 16384,
    ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a nonempty string")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not 1 <= max_output_tokens <= self.MAX_OUTPUT_TOKENS:
            raise ValueError(f"max_output_tokens must be from 1 through {self.MAX_OUTPUT_TOKENS}")
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        if not self.api_key:
            raise VideoEditingError("OPENAI_API_KEY is required for the OpenAI planning provider", code="model_credentials_missing")

    @classmethod
    def _image(cls, path: Path) -> dict[str, str]:
        if not path.is_file():
            raise VideoEditingError(f"analysis image is missing: {path}", code="model_input_invalid")
        if path.stat().st_size > cls.MAX_IMAGE_BYTES:
            raise VideoEditingError(f"analysis image exceeds the {cls.MAX_IMAGE_BYTES} byte limit: {path}", code="model_input_too_large")
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}", "detail": "low"}

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str) and direct:
            return direct
        for item in response.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "refusal":
                    raise VideoEditingError(f"model refused the planning request: {content.get('refusal', '')}", code="model_refusal")
                if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return content["text"]
        raise VideoEditingError("model response contained no structured output", code="model_invalid_response")

    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
        images: tuple[Path, ...] = (),
    ) -> ModelResponse:
        if not isinstance(instructions, str) or not isinstance(input_text, str):
            raise VideoEditingError("model instructions and input must be text", code="model_input_invalid")
        if len(images) > self.MAX_IMAGES:
            raise VideoEditingError(f"analysis contains more than {self.MAX_IMAGES} images", code="model_input_too_large")
        if len(json.dumps(schema, separators=(",", ":")).encode("utf-8")) > self.MAX_SCHEMA_BYTES:
            raise VideoEditingError("model schema exceeds the configured size limit", code="model_input_too_large")
        content: list[dict[str, Any]] = [{"type": "input_text", "text": input_text}]
        content.extend(self._image(path) for path in images)
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": [{"role": "user", "content": content}],
            "text": {"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
            "tools": [],
            "store": False,
            "max_output_tokens": self.max_output_tokens,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(body) > self.MAX_INPUT_BYTES:
            raise VideoEditingError("model request exceeds the configured size limit", code="model_input_too_large")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", errors="replace")
            raise ExternalToolError(f"OpenAI Responses API returned HTTP {exc.code}: {detail}", code="model_request_failed") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExternalToolError(f"OpenAI Responses API request failed: {exc}", code="model_request_failed") from exc
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise VideoEditingError("model response exceeded 4 MiB", code="model_invalid_response")
        try:
            response_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VideoEditingError("model provider returned invalid JSON", code="model_invalid_response") from exc
        if response_data.get("status") not in (None, "completed"):
            raise VideoEditingError(f"model response did not complete: {response_data.get('status')}", code="model_incomplete")
        text = self._output_text(response_data)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VideoEditingError("model structured output was not JSON", code="model_invalid_response") from exc
        if not isinstance(data, dict):
            raise VideoEditingError("model structured output must be an object", code="model_invalid_response")
        return ModelResponse(data, {
            "provider": "openai-responses",
            "model": response_data.get("model", self.model),
            "response_id": response_data.get("id"),
            "usage": response_data.get("usage"),
            "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
