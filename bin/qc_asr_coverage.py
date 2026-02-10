#!/usr/bin/env python3
# /workspace/pipeline/bin/qc_asr_coverage.py
#
# Usage:
#   qc_asr_coverage.py <manifest.json> <asr.json> <out_qc.json>
#
# Exit codes:
#   0  = pass
#   20 = fail-soft (policy failure)
#   2  = hard failure / misuse

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


def now_utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def coerce_float(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        v = float(x)
    elif isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    else:
        return None

    if math.isnan(v) or math.isinf(v):
        return None
    return v


def manifest_duration_seconds(manifest: dict):
    """
    Be tolerant: different manifest generators may store duration in different places.
    Try common patterns before giving up.
    """
    candidates = []

    # Most direct
    candidates.append(manifest.get("duration_s"))
    candidates.append(manifest.get("duration_sec"))
    candidates.append(manifest.get("duration_seconds"))

    # Nested common shapes
    media = manifest.get("media") or {}
    candidates.append(media.get("duration_s"))
    candidates.append(media.get("duration_sec"))
    candidates.append(media.get("duration_seconds"))

    probe = manifest.get("probe") or {}
    candidates.append(probe.get("duration_s"))
    candidates.append(probe.get("duration"))
    candidates.append(probe.get("format_duration_s"))
    candidates.append(probe.get("format_duration"))

    fmt = probe.get("format") or {}
    candidates.append(fmt.get("duration"))
    candidates.append(fmt.get("duration_s"))

    # Some ffprobe JSON puts duration in strings
    for x in candidates:
        v = coerce_float(x)
        if v is not None and v > 0:
            return v

    return None


def get_ts(seg, key_a, key_b):
    if key_a in seg:
        return seg.get(key_a)
    return seg.get(key_b)


def main():
    if len(sys.argv) != 4:
        print("usage: qc_asr_coverage.py <manifest.json> <asr.json> <out_qc.json>", file=sys.stderr)
        return 2

    manifest_path = Path(sys.argv[1])
    asr_path = Path(sys.argv[2])
    out_path = Path(sys.argv[3])

    if not manifest_path.exists():
        print(f"FATAL: manifest does not exist: {manifest_path}", file=sys.stderr)
        return 2
    if not asr_path.exists():
        print(f"FATAL: asr json does not exist: {asr_path}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text())
    asr = json.loads(asr_path.read_text())

    media_id = manifest.get("media_id") or asr.get("media_id") or "unknown"
    engine = asr.get("engine") or "unknown"

    duration_s = manifest_duration_seconds(manifest)

    segs = asr.get("segments") or []
    n_segments = len(segs)

    valid = []
    for seg in segs:
        st_raw = get_ts(seg, "start", "start_s")
        en_raw = get_ts(seg, "end", "end_s")
        st = coerce_float(st_raw)
        en = coerce_float(en_raw)

        if st is None or en is None:
            continue
        if st < 0 or en < 0:
            continue
        if en < st:
            continue
        valid.append((st, en))

    qc = {
        "schema_version": 1,
        "ts_utc": now_utc_compact(),
        "media_id": media_id,
        "engine": engine,
        "duration_s": duration_s,
        "n_segments": n_segments,
        "n_valid_segments": len(valid),
        "min_span_coverage": 0.7,
        "pass": False,
        "reason": "uninitialised",
    }

    if len(valid) == 0:
        qc["reason"] = "segments_missing_valid_timestamps"
        out_path.write_text(json.dumps(qc, indent=2))
        return 20

    if duration_s is None or duration_s <= 0:
        qc["reason"] = "manifest_missing_or_invalid_duration_s"
        out_path.write_text(json.dumps(qc, indent=2))
        return 20

    first_start = min(st for st, _ in valid)
    last_end = max(en for _, en in valid)
    span_s = max(0.0, last_end - first_start)
    span_coverage = span_s / duration_s

    valid_sorted = sorted(valid, key=lambda x: x[0])
    merged = []
    cur_s, cur_e = valid_sorted[0]
    for s, e in valid_sorted[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))

    speech_s = sum(max(0.0, e - s) for s, e in merged)
    speech_coverage = speech_s / duration_s

    qc.update(
        {
            "first_start_s": first_start,
            "last_end_s": last_end,
            "span_s": span_s,
            "span_coverage": span_coverage,
            "speech_s": speech_s,
            "speech_coverage": speech_coverage,
        }
    )

    if span_coverage >= qc["min_span_coverage"]:
        qc["pass"] = True
        qc["reason"] = "ok"
        out_path.write_text(json.dumps(qc, indent=2))
        return 0

    qc["pass"] = False
    qc["reason"] = f"span_coverage_below_threshold:{span_coverage:.6f}< {qc['min_span_coverage']}"
    out_path.write_text(json.dumps(qc, indent=2))
    return 20


if __name__ == "__main__":
    sys.exit(main())
