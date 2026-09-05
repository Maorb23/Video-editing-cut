"""Bounded retry policy for Phase 3 worker execution."""

from __future__ import annotations

from dataclasses import dataclass


TRANSIENT_ERROR_CODES = frozenset({
    "model_timeout",
    "provider_rate_limited",
    "object_store_unavailable",
    "process_timeout",
    "process_no_progress",
    "worker_lost",
})


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    reason: str


def classify_retry(code: str, attempt: int, max_attempts: int) -> RetryDecision:
    if attempt >= max_attempts:
        return RetryDecision(False, "attempt_limit_reached")
    if code not in TRANSIENT_ERROR_CODES:
        return RetryDecision(False, "permanent_error")
    return RetryDecision(True, "transient_error")
