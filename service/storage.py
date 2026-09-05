from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    sha256: str


def _safe_key(key: str) -> PurePosixPath:
    path = PurePosixPath(key)
    if path.is_absolute() or not path.parts or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise ValueError("unsafe storage key")
    return path


class Storage(Protocol):
    def put(self, key: str, source: BinaryIO, *, max_bytes: int | None = None) -> StoredObject: ...
    def put_path(self, key: str, source: Path) -> StoredObject: ...
    def download(self, key: str, destination: Path) -> None: ...
    def open(self, key: str) -> BinaryIO: ...
    def url(self, key: str, *, expires_seconds: int = 900) -> str | None: ...


class FilesystemStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = self.root.joinpath(*_safe_key(key).parts)
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ValueError("storage key escapes root") from exc
        return path

    def put(self, key: str, source: BinaryIO, *, max_bytes: int | None = None) -> StoredObject:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ValueError("upload exceeds configured byte limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                existing = hashlib.sha256()
                existing_size = 0
                with destination.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        existing_size += len(chunk)
                        existing.update(chunk)
                if existing_size != size or existing.hexdigest() != digest.hexdigest():
                    raise
            destination.chmod(0o444)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredObject(key, size, digest.hexdigest())

    def put_path(self, key: str, source: Path) -> StoredObject:
        with source.open("rb") as stream:
            return self.put(key, stream)

    def download(self, key: str, destination: Path) -> None:
        source = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as incoming, temporary.open("xb") as output:
                shutil.copyfileobj(incoming, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def url(self, key: str, *, expires_seconds: int = 900) -> str | None:
        _safe_key(key)
        return None


class S3Storage:
    def __init__(self, bucket: str, *, endpoint_url: str | None = None, region: str | None = None, client: object | None = None) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("S3 storage requires the 's3' project extra") from exc
            client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
        self.client = client
        self.bucket = bucket

    def put(self, key: str, source: BinaryIO, *, max_bytes: int | None = None) -> StoredObject:
        _safe_key(key)
        digest = hashlib.sha256()
        size = 0
        with tempfile.TemporaryFile() as temporary:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise ValueError("upload exceeds configured byte limit")
                digest.update(chunk)
                temporary.write(chunk)
            temporary.seek(0)
            try:
                self.client.put_object(
                    Bucket=self.bucket, Key=key, Body=temporary, IfNoneMatch="*",
                    Metadata={"sha256": digest.hexdigest()},
                )
            except Exception:
                existing = self.client.head_object(Bucket=self.bucket, Key=key)
                if existing.get("ContentLength") != size or existing.get("Metadata", {}).get("sha256") != digest.hexdigest():
                    raise
        return StoredObject(key, size, digest.hexdigest())

    def put_path(self, key: str, source: Path) -> StoredObject:
        with source.open("rb") as stream:
            return self.put(key, stream)

    def download(self, key: str, destination: Path) -> None:
        _safe_key(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.client.download_file(self.bucket, key, str(temporary))
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def open(self, key: str) -> BinaryIO:
        _safe_key(key)
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]

    def url(self, key: str, *, expires_seconds: int = 900) -> str | None:
        _safe_key(key)
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_seconds,
        )


def build_storage(settings: object) -> Storage:
    if settings.storage_backend == "filesystem":
        return FilesystemStorage(settings.storage_root)
    return S3Storage(
        settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
    )
