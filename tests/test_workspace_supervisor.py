from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

from video_editing.errors import ExternalToolError, VideoEditingError
from video_editing.supervisor import CancellationToken, ProcessLimits, ProcessSupervisor
from video_editing.workspace import JobWorkspace


class WorkspaceTests(unittest.TestCase):
    def test_paths_are_confined_and_nonempty_jobs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            workspace = JobWorkspace.create(parent / "job")
            for value in ("../escape", "/absolute", "C:\\escape\\file", "https://example.test/a"):
                with self.subTest(value=value), self.assertRaisesRegex(VideoEditingError, "unsafe"):
                    workspace.path(value)
            workspace.path("owned.txt").write_text("owned", encoding="utf-8")
            with self.assertRaises(VideoEditingError):
                JobWorkspace.create(workspace.root)

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            workspace = JobWorkspace.create(parent / "job")
            outside = parent / "outside"
            outside.mkdir()
            try:
                workspace.path("link").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks are unavailable")
            with self.assertRaises(VideoEditingError):
                workspace.path("link/escaped.txt")


class SupervisorTests(unittest.TestCase):
    def test_wall_timeout_terminates_process(self) -> None:
        supervisor = ProcessSupervisor(ProcessLimits(wall_timeout=0.2, no_progress_timeout=None, terminate_grace=0.1))
        with self.assertRaises(ExternalToolError) as context:
            supervisor.run([sys.executable, "-c", "import time; time.sleep(10)"])
        self.assertEqual(context.exception.code, "process_timeout")

    def test_no_progress_timeout_and_bounded_output(self) -> None:
        supervisor = ProcessSupervisor(ProcessLimits(wall_timeout=5, no_progress_timeout=0.2, max_output_bytes=128, terminate_grace=0.1))
        with self.assertRaises(ExternalToolError) as context:
            supervisor.run([sys.executable, "-c", "import sys,time; print('x'*1000, flush=True); time.sleep(10)"])
        self.assertEqual(context.exception.code, "process_no_progress")

        completed = ProcessSupervisor(ProcessLimits(wall_timeout=5, no_progress_timeout=2, max_output_bytes=64)).run(
            [sys.executable, "-c", "print('a'*1000)"]
        )
        self.assertTrue(completed.output_truncated)
        self.assertLessEqual(len(completed.stdout.encode("utf-8")), 64)

    def test_progress_predicate_does_not_treat_noisy_logs_as_progress(self) -> None:
        supervisor = ProcessSupervisor(ProcessLimits(wall_timeout=5, no_progress_timeout=0.2, terminate_grace=0.1))
        with self.assertRaises(ExternalToolError) as context:
            supervisor.run(
                [sys.executable, "-c", "import time\nwhile True: print('diagnostic', flush=True); time.sleep(.02)"],
                progress_predicate=lambda line: "progress=" in line,
            )
        self.assertEqual(context.exception.code, "process_no_progress")

    def test_cancellation_terminates_process(self) -> None:
        token = CancellationToken()
        supervisor = ProcessSupervisor(ProcessLimits(wall_timeout=5, no_progress_timeout=None, terminate_grace=0.1), token)
        timer = threading.Timer(0.1, token.cancel)
        timer.start()
        try:
            with self.assertRaises(ExternalToolError) as context:
                supervisor.run([sys.executable, "-c", "import time; time.sleep(10)"])
        finally:
            timer.cancel()
        self.assertEqual(context.exception.code, "process_cancelled")


if __name__ == "__main__":
    unittest.main()
