"""Per-job workspace confinement and immutable artifact allocation."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from .errors import VideoEditingError


@dataclass(frozen=True)
class RenderAttempt:
    id: str
    directory: Path
    output: Path
    validation: Path


class JobWorkspace:
    """Own all generated artifacts beneath a fresh, caller-selected directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @classmethod
    def create(cls, root: Path) -> "JobWorkspace":
        candidate = root.expanduser()
        if candidate.exists():
            if not candidate.is_dir() or any(candidate.iterdir()):
                raise VideoEditingError(f"refusing to use nonempty job directory: {candidate}", code="output_exists")
        else:
            candidate.mkdir(parents=True, mode=0o700)
        workspace = cls(candidate)
        for name in ("input", "analysis", "work", "renders"):
            workspace.path(name).mkdir(mode=0o700)
        return workspace

    @classmethod
    def open(cls, root: Path) -> "JobWorkspace":
        """Open a previously created workspace without creating artifacts."""
        candidate = root.expanduser()
        if not candidate.is_dir() or candidate.is_symlink():
            raise VideoEditingError(f"job workspace not found: {candidate}", code="missing_workspace")
        workspace = cls(candidate)
        for name in ("input", "analysis", "work", "renders"):
            directory = workspace.path(name)
            if not directory.is_dir() or directory.is_symlink():
                raise VideoEditingError(f"invalid job workspace directory: {directory}", code="unsafe_path")
        return workspace

    def path(self, relative: str | Path) -> Path:
        text = str(relative)
        parsed = Path(text)
        if parsed.is_absolute() or PureWindowsPath(text).is_absolute() or urlsplit(text).scheme or ".." in parsed.parts:
            raise VideoEditingError(f"unsafe workspace path: {text}", code="unsafe_path")
        destination = self.root.joinpath(parsed)
        try:
            destination.resolve().relative_to(self.root)
        except ValueError as exc:
            raise VideoEditingError(f"path escapes job workspace: {text}", code="unsafe_path") from exc
        return destination

    def assert_confined(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise VideoEditingError(f"path escapes job workspace: {path}", code="unsafe_path") from exc
        return resolved

    def import_media(self, source: Path) -> Path:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise VideoEditingError(f"source media not found: {source}", code="missing_asset")
        suffix = source.suffix.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", source.suffix) else ".media"
        destination = self.path(f"input/source{suffix}")
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        destination.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return destination

    def allocate_render(self) -> RenderAttempt:
        attempt_id = f"render-{uuid.uuid4().hex}"
        directory = self.path(Path("renders") / attempt_id)
        directory.mkdir(mode=0o700)
        return RenderAttempt(attempt_id, directory, directory / "output.mp4", directory / "validation.json")

    def write_json(self, relative: str | Path, value: dict[str, Any]) -> Path:
        destination = self.path(relative)
        if destination.exists():
            raise VideoEditingError(f"refusing to overwrite artifact: {destination}", code="output_exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic no-overwrite publication, including racing worker attempts.
            os.link(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination
