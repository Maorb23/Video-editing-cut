from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from ..errors import PlanValidationError, VideoEditingError


def execute(action: Callable[[], Any], *, json_errors: bool = False) -> int:
    try:
        action()
        return 0
    except PlanValidationError as exc:
        payload = {"ok": False, "error": exc.code, "message": str(exc), "issues": [item.as_dict() for item in exc.issues]}
    except VideoEditingError as exc:
        payload = {"ok": False, "error": exc.code, "message": str(exc)}
    except KeyboardInterrupt:
        payload = {"ok": False, "error": "interrupted", "message": "operation interrupted"}
    if json_errors:
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
    else:
        print(f"error [{payload['error']}]: {payload['message']}", file=sys.stderr)
        for issue in payload.get("issues", []):
            print(f"  {issue['path']}: {issue['message']} ({issue['code']})", file=sys.stderr)
    return 2

