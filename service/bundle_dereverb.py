"""Image-build-only download and fingerprint verification of CPU artifacts."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.request import urlopen

from video_editing.dereverb_pin import EXECUTABLE_SHA256, EXECUTABLE_URL, MODEL_SHA256, MODEL_URL


def main() -> None:
    root = Path("/opt/dereverb")
    root.mkdir(parents=True, exist_ok=True)
    for name, url, expected in (("model.tar.gz", MODEL_URL, MODEL_SHA256),
                                ("deep-filter", EXECUTABLE_URL, EXECUTABLE_SHA256)):
        destination = root / name
        digest = hashlib.sha256()
        with urlopen(url, timeout=60) as response, destination.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        if "sha256:" + digest.hexdigest() != expected:
            destination.unlink()
            raise RuntimeError(f"fingerprint_mismatch: {name}")
        destination.chmod(0o555 if name == "deep-filter" else 0o444)
    print(f"Bundled CPU DeepFilterNet3 {MODEL_SHA256}")


if __name__ == "__main__":
    main()
