#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

TS_RE = re.compile(r"^(?P<s>\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(?P<e>\d{2}:\d{2}:\d{2}\.\d{3})")

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def parse_ts(t: str) -> float:
    # HH:MM:SS.mmm -> seconds float
    hh, mm, rest = t.split(":")
    ss, ms = rest.split(".")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + (int(ms) / 1000.0)

def main():
    if len(sys.argv) != 5:
        print("usage: vtt_to_json.py <media_id> <engine> <in.vtt> <out.json>", file=sys.stderr)
        return 2

    media_id, engine, in_vtt, out_json = sys.argv[1], sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])

    text = in_vtt.read_text(errors="replace").splitlines()

    segments = []
    i = 0
    while i < len(text):
        line = text[i].strip()
        i += 1

        if not line or line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue

        m = TS_RE.match(line)
        if not m:
            continue

        start = parse_ts(m.group("s"))
        end = parse_ts(m.group("e"))

        # Collect cue text until blank line
        cue_lines = []
        while i < len(text) and text[i].strip() != "":
            cue_lines.append(text[i].rstrip("\n"))
            i += 1

        # Normalise cue text: strip simple tags, collapse whitespace
        cue = "\n".join(cue_lines).strip()
        cue = re.sub(r"<[^>]+>", "", cue)  # basic tag strip
        cue = re.sub(r"\s+", " ", cue).strip()

        if cue:
            segments.append({
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "text": cue
            })

    out = {
        "schema_version": 1,
        "media_id": media_id,
        "engine": engine,
        "source_vtt": str(in_vtt),
        "segments": segments
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"OK: wrote {out_json} (segments={len(segments)})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
