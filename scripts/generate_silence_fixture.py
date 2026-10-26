#!/usr/bin/env python3
"""Reproduce the historical 1-second, 16-kHz, mono PCM silence WAV.

The output matches bin/silence.wav byte-for-byte, including its 44-byte
RIFF header. No audio recording, third-party binary or library is used.
Default mode is non-mutating. Pass --write to restore the tracked fixture.
"""
from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "bin" / "silence.wav"
EXPECTED_GIT_BLOB_SHA1 = "3f1a79e7e4dab5becbac15d0e10360e6d77ed322"
EXPECTED_SHA256 = "643f8a8dc8bd9c19225afffad2becfec5426180b3749cb208abdf1a6c8354efc"
SAMPLE_RATE = 16_000
DURATION_SECONDS = 1
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


def render_silence_wav() -> bytes:
    """Return precisely 32,044 bytes of pure PCM digital silence."""
    raw_frames = bytes(SAMPLE_RATE * DURATION_SECONDS * SAMPLE_WIDTH_BYTES)
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(raw_frames)
    result = out.getvalue()
    if len(result) != 44 + len(raw_frames):
        raise RuntimeError("Unexpected WAV header size")
    return result


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="explicitly restore the generated fixture")
    args = parser.parse_args(argv)
    data = render_silence_wav()
    if git_blob_sha1(data) != EXPECTED_GIT_BLOB_SHA1:
        print("Generated WAV differs from the recorded Git blob", file=sys.stderr)
        return 2
    if hashlib.sha256(data).hexdigest() != EXPECTED_SHA256:
        print("Generated WAV differs from the recorded SHA-256", file=sys.stderr)
        return 2
    if FIXTURE.is_symlink():
        print("Refusing a symlink fixture path", file=sys.stderr)
        return 2
    if args.write:
        FIXTURE.write_bytes(data)
    elif not FIXTURE.is_file() or FIXTURE.read_bytes() != data:
        print("Tracked fixture does not match independent generation", file=sys.stderr)
        return 1
    print(f"Verified reproducible PCM silence: {len(data)} bytes; "
          f"git blob {EXPECTED_GIT_BLOB_SHA1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
