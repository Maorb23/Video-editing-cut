#!/usr/bin/env python3
"""Deterministic CPU CLI fixture. Not an acoustic dereverberation model."""
import argparse
import array
from pathlib import Path
import sys
import time
import wave

if "--version" in sys.argv:
    print("deep_filter 0.5.6")
    raise SystemExit(0)
parser = argparse.ArgumentParser()
parser.add_argument("--model", required=True)
parser.add_argument("--compensate-delay", action="store_true", required=True)
parser.add_argument("--output-dir", required=True)
parser.add_argument("source")
args = parser.parse_args()
mode = Path(args.model).read_text(encoding="utf-8").strip()
if mode == "missing":
    raise SystemExit(0)
if mode == "timeout":
    time.sleep(10)
if mode == "noisy":
    print("x" * 200000, file=sys.stderr)
source = Path(args.source)
output = Path(args.output_dir) / source.name
output.parent.mkdir(parents=True, exist_ok=True)
with wave.open(str(source), "rb") as reader:
    channels, rate = reader.getnchannels(), reader.getframerate()
    samples = array.array("h", reader.readframes(reader.getnframes() - 1440))
    if sys.byteorder != "little":
        samples.byteswap()
    samples = array.array("h", (int(sample / 2) for sample in samples))
    if sys.byteorder != "little":
        samples.byteswap()
with wave.open(str(output), "wb") as writer:
    writer.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
    writer.writeframes(samples.tobytes())
