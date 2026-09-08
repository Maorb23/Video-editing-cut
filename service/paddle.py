"""Small, dependency-free Paddle webhook verification helpers."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


class PaddleWebhookError(ValueError):
    pass


def verify_paddle_webhook(
    raw_body: bytes,
    signature_header: str | None,
    secret: str,
    *,
    now: int | None = None,
    tolerance_seconds: int = 5,
) -> dict[str, Any]:
    """Verify Paddle's ts/raw-body HMAC before parsing the event JSON."""
    if not raw_body or len(raw_body) > 1024 * 1024:
        raise PaddleWebhookError("invalid webhook body")
    parts: dict[str, list[str]] = {}
    for component in (signature_header or "").split(";"):
        key, separator, value = component.partition("=")
        if separator and key and value:
            parts.setdefault(key, []).append(value)
    timestamps = parts.get("ts", [])
    signatures = parts.get("h1", [])
    if len(timestamps) != 1 or not signatures:
        raise PaddleWebhookError("invalid Paddle-Signature header")
    try:
        timestamp = int(timestamps[0])
    except ValueError as exc:
        raise PaddleWebhookError("invalid webhook timestamp") from exc
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise PaddleWebhookError("expired webhook timestamp")
    signed = str(timestamp).encode("ascii") + b":" + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise PaddleWebhookError("invalid webhook signature")
    try:
        event = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaddleWebhookError("invalid webhook JSON") from exc
    if not isinstance(event, dict):
        raise PaddleWebhookError("webhook event must be an object")
    return event


def completed_transaction(event: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return event, transaction, internal-order, and purchased-price IDs."""
    if event.get("event_type") != "transaction.completed":
        raise PaddleWebhookError("event is not a completed transaction")
    event_id = event.get("event_id")
    data = event.get("data")
    if not isinstance(event_id, str) or not isinstance(data, dict) or data.get("status") != "completed":
        raise PaddleWebhookError("invalid completed transaction")
    transaction_id = data.get("id")
    custom = data.get("custom_data")
    items = data.get("items")
    if data.get("subscription_id") is not None:
        raise PaddleWebhookError("credit top-ups must use one-time prices")
    if not isinstance(transaction_id, str) or not transaction_id.startswith("txn_") or not isinstance(custom, dict):
        raise PaddleWebhookError("transaction identity is missing")
    order_id = custom.get("melvid_order_id")
    if not isinstance(order_id, str) or not order_id.startswith("ord_") or not isinstance(items, list) or len(items) != 1:
        raise PaddleWebhookError("transaction does not identify one Melvid order")
    item = items[0]
    price = item.get("price") if isinstance(item, dict) else None
    if not isinstance(price, dict) or item.get("quantity") != 1 or not isinstance(price.get("id"), str):
        raise PaddleWebhookError("transaction line item is invalid")
    return event_id, transaction_id, order_id, price["id"]
