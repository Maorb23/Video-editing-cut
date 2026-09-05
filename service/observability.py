"""Small structured event emitter suitable for container log collection."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


LOGGER = logging.getLogger("video_editing.worker")


def emit(event: str, **fields: Any) -> None:
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    LOGGER.info(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
