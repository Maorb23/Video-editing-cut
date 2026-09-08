from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import unittest

from service.config import Settings
from service.paddle import PaddleWebhookError, completed_transaction, verify_paddle_webhook


class PaddleBillingTests(unittest.TestCase):
    def event(self) -> bytes:
        return json.dumps({"event_id": "evt_1", "event_type": "transaction.completed", "data": {
            "id": "txn_1", "status": "completed", "subscription_id": None,
            "custom_data": {"melvid_order_id": "ord_" + "1" * 32},
            "items": [{"quantity": 1, "price": {"id": "pri_" + "a" * 26}}],
        }}, separators=(",", ":")).encode()

    def test_signature_uses_raw_body_supports_rotation_and_rejects_replay(self):
        body, secret, timestamp = self.event(), "secret", 1000
        valid = hmac.new(secret.encode(), b"1000:" + body, hashlib.sha256).hexdigest()
        event = verify_paddle_webhook(body, f"ts=1000;h1=old;h1={valid}", secret, now=1004)
        self.assertEqual(completed_transaction(event)[1:], ("txn_1", "ord_" + "1" * 32, "pri_" + "a" * 26))
        for changed_body, header, now in ((body + b" ", f"ts=1000;h1={valid}", 1004),
                                          (body, f"ts=1000;h1={valid}", 1006),
                                          (body, None, 1000)):
            with self.subTest(now=now, header=header), self.assertRaises(PaddleWebhookError):
                verify_paddle_webhook(changed_body, header, secret, now=now)

    def test_configuration_is_all_or_nothing_and_environment_bound(self):
        base = dict(database_url="db", storage_backend="filesystem", storage_root=Path("objects"), work_root=Path("jobs"))
        self.assertEqual(Settings(**base).payment_mode, "mock")
        self.assertEqual(Settings(**base, session_cookie_secure=True).payment_mode, "disabled")
        with self.assertRaises(ValueError):
            Settings(**base, paddle_client_token="test_only").validate()
        with self.assertRaises(ValueError):
            Settings(**base, paddle_environment="production", paddle_client_token="test_token",
                paddle_webhook_secret="secret", paddle_price_starter="pri_" + "a" * 26,
                paddle_price_creator="pri_" + "b" * 26, paddle_price_studio="pri_" + "c" * 26).validate()


if __name__ == "__main__":
    unittest.main()
