"""Bounded, cancellable supervision for local media-tool processes."""

from __future__ import annotations

import io
import os
import queue
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, TextIO

from .errors import ExternalToolError


@dataclass(frozen=True)
class ProcessLimits:
    wall_timeout: float = 600.0
    no_progress_timeout: float | None = 120.0
    max_output_bytes: int = 256 * 1024
    terminate_grace: float = 2.0

    def __post_init__(self) -> None:
        if self.wall_timeout <= 0:
            raise ValueError("wall_timeout must be positive")
        if self.no_progress_timeout is not None and self.no_progress_timeout <= 0:
            raise ValueError("no_progress_timeout must be positive")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")


class CancellationToken:
    """Thread-safe cancellation hook shared by a pipeline and its child tools."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class ProcessResult:
    arguments: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    output_truncated: bool


@dataclass(frozen=True)
class Toolchain:
    """Absolute, immutable executable selection for one pipeline run."""

    ffmpeg: Path
    ffprobe: Path
    melt: Path

    @classmethod
    def resolve(cls, *, ffmpeg: str | None = None, ffprobe: str | None = None, melt: str | None = None) -> "Toolchain":
        def find(value: str | None, names: tuple[str, ...], label: str) -> Path:
            selected = value or next((found for name in names if (found := shutil.which(name))), None)
            if selected is None:
                raise ExternalToolError(f"{label} executable is required", code="tool_unavailable")
            path = Path(selected).expanduser().resolve()
            if not path.is_file():
                raise ExternalToolError(f"{label} executable not found: {path}", code="tool_unavailable")
            if os.name != "nt" and not os.access(path, os.X_OK):
                raise ExternalToolError(f"{label} is not executable: {path}", code="tool_unavailable")
            return path

        return cls(
            ffmpeg=find(ffmpeg, ("ffmpeg", "ffmpeg.exe"), "ffmpeg"),
            ffprobe=find(ffprobe, ("ffprobe", "ffprobe.exe"), "ffprobe"),
            melt=find(melt, ("melt", "melt-7", "melt.exe"), "melt"),
        )

    def versions(self, supervisor: "ProcessSupervisor") -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for name, path in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe), ("melt", self.melt)):
            checked = supervisor.run([str(path), "--version"])
            if checked.returncode:
                checked = supervisor.run([str(path), "-version"])
            if checked.returncode:
                raise ExternalToolError(f"cannot query {name} version: {checked.stderr or checked.stdout}", code="tool_unavailable")
            first_line = (checked.stdout or checked.stderr).strip().splitlines()
            result[name] = {"path": str(path), "version": first_line[0] if first_line else "unknown"}
        return result


class _BoundedText:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.value = ""
        self.truncated = False

    def append(self, value: str) -> None:
        self.value += value
        encoded = self.value.encode("utf-8", errors="replace")
        if len(encoded) <= self.limit:
            return
        self.truncated = True
        encoded = encoded[-self.limit :]
        while encoded and (encoded[0] & 0xC0) == 0x80:
            encoded = encoded[1:]
        self.value = encoded.decode("utf-8", errors="replace")


class ProcessSupervisor:
    """Run list-form commands with deadlines, bounded diagnostics, and tree cleanup."""

    def __init__(self, limits: ProcessLimits | None = None, cancellation: CancellationToken | None = None) -> None:
        self.limits = limits or ProcessLimits()
        self.cancellation = cancellation or CancellationToken()

    def _stop(self, process: subprocess.Popen[str]) -> None:
        try:
            if os.name == "posix" and isinstance(process.pid, int):
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=self.limits.terminate_grace)
            return
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired, TypeError):
            pass
        try:
            if os.name == "posix" and isinstance(process.pid, int):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError, TypeError):
            pass
        try:
            process.wait(timeout=self.limits.terminate_grace)
        except (OSError, subprocess.TimeoutExpired, TypeError):
            pass

    def run(
        self,
        arguments: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        merge_stderr: bool = False,
        on_line: Callable[[str], None] | None = None,
        progress_predicate: Callable[[str], bool] | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> ProcessResult:
        if not arguments or not all(isinstance(value, str) and value for value in arguments):
            raise ValueError("arguments must be a nonempty list of nonempty strings")
        creation: dict[str, object] = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
        started = time.monotonic()
        try:
            process = popen_factory(
                arguments,
                cwd=cwd,
                env=None if env is None else dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **creation,
            )
        except OSError as exc:
            raise ExternalToolError(f"cannot execute {arguments[0]}: {exc}", code="tool_unavailable") from exc

        messages: queue.Queue[tuple[str, str | BaseException | None]] = queue.Queue(maxsize=256)
        reader_stop = threading.Event()

        def send(item: tuple[str, str | BaseException | None]) -> None:
            while not reader_stop.is_set():
                try:
                    messages.put(item, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def read_stream(name: str, stream: TextIO | None) -> None:
            if stream is not None:
                try:
                    if isinstance(stream, io.TextIOBase):
                        while chunk := stream.readline(4096):
                            send((name, chunk))
                    else:
                        for line in stream:
                            send((name, line))
                except BaseException as exc:
                    send(("exception", exc))
                finally:
                    try:
                        stream.close()
                    except (AttributeError, OSError):
                        pass
            send((name, None))

        streams = [("stdout", process.stdout)]
        if not merge_stderr:
            streams.append(("stderr", process.stderr))
        threads = [threading.Thread(target=read_stream, args=item, daemon=True) for item in streams]
        for thread in threads:
            thread.start()

        stdout = _BoundedText(self.limits.max_output_bytes)
        stderr = _BoundedText(self.limits.max_output_bytes)
        completed_streams = 0
        last_progress = started
        failure: ExternalToolError | None = None
        try:
            while completed_streams < len(streams):
                now = time.monotonic()
                if self.cancellation.cancelled:
                    failure = ExternalToolError(f"{arguments[0]} was cancelled", code="process_cancelled")
                    break
                if now - started > self.limits.wall_timeout:
                    failure = ExternalToolError(f"{arguments[0]} exceeded the {self.limits.wall_timeout:g}s timeout", code="process_timeout")
                    break
                if self.limits.no_progress_timeout is not None and now - last_progress > self.limits.no_progress_timeout:
                    failure = ExternalToolError(
                        f"{arguments[0]} made no progress for {self.limits.no_progress_timeout:g}s",
                        code="process_no_progress",
                    )
                    break
                try:
                    name, line = messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    completed_streams += 1
                    continue
                if name == "exception":
                    assert isinstance(line, BaseException)
                    raise line
                assert isinstance(line, str)
                target = stdout if name == "stdout" else stderr
                target.append(line)
                if progress_predicate is None or progress_predicate(line):
                    last_progress = time.monotonic()
                if on_line is not None:
                    on_line(line)
            if failure is not None:
                self._stop(process)
                raise failure
            remaining = max(0.01, self.limits.wall_timeout - (time.monotonic() - started))
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                self._stop(process)
                raise ExternalToolError(f"{arguments[0]} exceeded the {self.limits.wall_timeout:g}s timeout", code="process_timeout") from exc
        except BaseException:
            reader_stop.set()
            self._stop(process)
            raise
        reader_stop.set()
        return ProcessResult(
            arguments=tuple(arguments),
            returncode=returncode,
            stdout=stdout.value,
            stderr=stderr.value,
            duration_seconds=time.monotonic() - started,
            output_truncated=stdout.truncated or stderr.truncated,
        )
