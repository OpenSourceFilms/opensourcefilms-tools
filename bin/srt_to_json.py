#!/usr/bin/env python3
# /workspace/pipeline/bin/srt_to_json.py
#
# Usage:
#   srt_to_json.py <media_id> <engine> <in.srt> <out.json>
#
# Writes:
#   {
#     "schema_version": 1,
#     "engine": "...",
#     "media_id": "...",
#     "segments": [{"start": float, "end": float, "text": str}, ...],
#     "source_srt": "..."
#   }

import json
import re
import sys
from pathlib import Path


def ts_to_seconds(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def main():
    if len(sys.argv) != 5:
        print("usage: srt_to_json.py <media_id> <engine> <in.srt> <out.json>", file=sys.stderr)
        return 2

    media_id = sys.argv[1]
    engine = sys.argv[2]
    in_srt = Path(sys.argv[3])
    out_json = Path(sys.argv[4])

    if not in_srt.exists():
        print(f"FATAL: SRT does not exist: {in_srt}", file=sys.stderr)
        return 2

    text = in_srt.read_text(errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    block_re = re.compile(
        r"(?ms)^\s*(?:\d+\s*\n)?"
        r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3}).*?\n"
        r"(.*?)(?=\n{2,}|\Z)"
    )

    segments = []
    for m in block_re.finditer(text):
        start_ts, end_ts, body = m.group(1), m.group(2), m.group(3)
        body = re.sub(r"\s+", " ", body).strip()
        if not body:
            continue
        segments.append(
            {
                "start": ts_to_seconds(start_ts),
                "end": ts_to_seconds(end_ts),
                "text": body,
            }
        )

    out = {
        "schema_version": 1,
        "engine": engine,
        "media_id": media_id,
        "segments": segments,
        "source_srt": str(in_srt),
    }

    out_json.write_text(json.dumps(out, indent=2))
    print(f"OK: wrote {out_json} (segments={len(segments)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
