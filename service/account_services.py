from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol
from urllib import parse, request


log = logging.getLogger(__name__)


class CaptchaVerifier(Protocol):
    def verify(self, token: str | None, remote_ip: str | None = None) -> bool: ...


class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, text: str) -> None: ...


class TurnstileVerifier:
    def __init__(self, secret_key: str | None) -> None:
        self.secret_key = secret_key

    def verify(self, token: str | None, remote_ip: str | None = None) -> bool:
        if not self.secret_key:
            return True
        if not token:
            return False
        body = {"secret": self.secret_key, "response": token}
        if remote_ip:
            body["remoteip"] = remote_ip
        req = request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=parse.urlencode(body).encode("utf-8"),
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            return bool(json.loads(response.read().decode("utf-8")).get("success"))


class TransactionalEmailSender:
    """Provider-neutral sender. An HTTP relay can later be replaced by an SDK."""

    def __init__(self, endpoint: str | None, api_key: str | None, from_email: str, provider: str = "generic") -> None:
        self.endpoint, self.api_key, self.from_email, self.provider = endpoint, api_key, from_email, provider

    def send(self, *, to: str, subject: str, text: str) -> None:
        if not self.endpoint:
            log.info("transactional_email_suppressed to=%s subject=%s", to, subject)
            return
        recipients = [to] if self.provider.lower() == "resend" else to
        payload = json.dumps({"from": self.from_email, "to": recipients, "subject": subject, "text": text}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with request.urlopen(request.Request(self.endpoint, data=payload, headers=headers, method="POST"), timeout=8):
            pass


class RateLimiter(Protocol):
    def allow(self, key: str, limit: int, window_seconds: int) -> bool: ...


@dataclass
class MemoryRateLimiter:
    """Process-local development fallback; production should configure Redis."""
    buckets: dict[str, list[float]]

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        values = [value for value in self.buckets.get(key, []) if value > now - window_seconds]
        if len(values) >= limit:
            self.buckets[key] = values
            return False
        values.append(now)
        self.buckets[key] = values
        return True


class RedisRateLimiter:
    def __init__(self, url: str) -> None:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("Redis rate limiting requires the 'redis' service dependency") from exc
        self.client = redis.Redis.from_url(url)

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        bucket = f"melvid:rate:{key}:{int(time.time()) // window_seconds}"
        with self.client.pipeline() as pipe:
            pipe.incr(bucket)
            pipe.expire(bucket, window_seconds + 1)
            count, _ = pipe.execute()
        return int(count) <= limit


def build_rate_limiter(redis_url: str | None) -> RateLimiter:
    return RedisRateLimiter(redis_url) if redis_url else MemoryRateLimiter({})
