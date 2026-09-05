from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from service.config import Settings
from service.reliability import classify_retry
from service.repository import ClaimedJob
from service.worker import Worker
from video_editing.errors import ExternalToolError


class FakeWorkerRepository:
    def __init__(self, attempt: int) -> None:
        self.job = ClaimedJob("job_1", "edt_1", "plan", "vid_1", "edit", "video", "clip.mp4", "worker", attempt)
        self.retried = False
        self.failed = False

    def reconcile_exhausted(self, max_attempts: int) -> int:
        return 0

    def claim(self, worker_id: str, lease_seconds: int, max_attempts: int):
        job, self.job = self.job, None
        return job

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        return True

    def retry(self, job, error) -> bool:
        self.retried = True
        return True

    def fail(self, job, error) -> None:
        self.failed = True


class FailingWorker(Worker):
    def _plan(self, job) -> None:
        raise ExternalToolError("timed out", code="process_timeout")


class Phase3ReliabilityTests(unittest.TestCase):
    def test_only_known_transient_errors_retry_within_limit(self) -> None:
        self.assertTrue(classify_retry("process_timeout", 1, 3).retryable)
        self.assertFalse(classify_retry("plan_invalid", 1, 3).retryable)
        exhausted = classify_retry("process_timeout", 3, 3)
        self.assertFalse(exhausted.retryable)
        self.assertEqual(exhausted.reason, "attempt_limit_reached")

    def test_worker_attempt_limit_is_bounded(self) -> None:
        valid = Settings("db", "filesystem", Path("objects"), Path("jobs"), worker_max_attempts=10)
        valid.validate()
        for value in (0, 11):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings("db", "filesystem", valid.storage_root, valid.work_root, worker_max_attempts=value).validate()

    def test_worker_retries_transient_failure_then_stops_at_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings("db", "filesystem", root / "objects", root / "jobs", worker_max_attempts=3)
            first = FakeWorkerRepository(attempt=1)
            FailingWorker(first, object(), settings, worker_id="worker", toolchain=object()).run_once()
            self.assertTrue(first.retried)
            self.assertFalse(first.failed)

            last = FakeWorkerRepository(attempt=3)
            FailingWorker(last, object(), settings, worker_id="worker", toolchain=object()).run_once()
            self.assertFalse(last.retried)
            self.assertTrue(last.failed)


if __name__ == "__main__":
    unittest.main()
