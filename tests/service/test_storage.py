from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from service.storage import FilesystemStorage, S3Storage


class FakeS3:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, Metadata):
        assert IfNoneMatch == "*"
        identity = (Bucket, Key)
        if identity in self.values:
            raise RuntimeError("precondition failed")
        self.values[identity] = Body.read()
        self.metadata = Metadata

    def head_object(self, *, Bucket, Key):
        value = self.values[(Bucket, Key)]
        return {"ContentLength": len(value), "Metadata": self.metadata}

    def download_file(self, bucket, key, destination):
        Path(destination).write_bytes(self.values[(bucket, key)])

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.values[(Bucket, Key)])}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        return f"signed://{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"


class StorageParityTests(unittest.TestCase):
    def test_filesystem_and_s3_preserve_identical_bytes_and_digest(self) -> None:
        payload = b"video-bytes" * 1024
        with tempfile.TemporaryDirectory() as directory:
            filesystem = FilesystemStorage(Path(directory))
            s3 = S3Storage("bucket", client=FakeS3())
            for storage in (filesystem, s3):
                stored = storage.put("videos/vid/source.mp4", io.BytesIO(payload), max_bytes=len(payload))
                self.assertEqual(stored.size, len(payload))
                with storage.open(stored.key) as stream:
                    self.assertEqual(stream.read(), payload)

    def test_storage_refuses_overwrite_traversal_and_oversize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = FilesystemStorage(Path(directory))
            storage.put("safe/object", io.BytesIO(b"first"))
            repeated = storage.put("safe/object", io.BytesIO(b"first"))
            self.assertEqual(repeated.size, 5)
            with self.assertRaises(FileExistsError):
                storage.put("safe/object", io.BytesIO(b"second"))
            with self.assertRaises(ValueError):
                storage.put("../escape", io.BytesIO(b"bad"))
            with self.assertRaises(ValueError):
                storage.put("safe/large", io.BytesIO(b"1234"), max_bytes=3)
            self.assertFalse((Path(directory) / "safe/large").exists())


if __name__ == "__main__":
    unittest.main()
