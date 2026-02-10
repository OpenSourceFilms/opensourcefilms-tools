#!/usr/bin/env python3
import argparse
import json
import re
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 2

# Contract-enumerated ASR / INGEST reasons
REASON_NO_AUDIO_TRACK = "NO_AUDIO_TRACK"
REASON_SILENT_AUDIO = "SILENT_AUDIO"
REASON_NON_SPEECH_AUDIO = "NON_SPEECH_AUDIO"


def die(msg: str, code: int = 2) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    raise SystemExit(code)


def parse_ts_srt(ts: str) -> float:
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def parse_srt(path: Path):
    segments = []
    cur_start = None
    cur_end = None
    cur_lines = []

    def flush():
        nonlocal cur_start, cur_end, cur_lines
        if cur_start is None or cur_end is None:
            cur_lines = []
            return
        text = " ".join(t.strip() for t in cur_lines if t.strip()).strip()
        if text:
            segments.append({"start": cur_start, "end": cur_end, "text": text})
        cur_start = None
        cur_end = None
        cur_lines = []

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")

            if re.fullmatch(r"\d+", line.strip()):
                flush()
                continue

            m = re.match(
                r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*$",
                line,
            )
            if m:
                cur_start = parse_ts_srt(m.group(1))
                cur_end = parse_ts_srt(m.group(2))
                continue

            if line.strip() == "":
                flush()
                continue

            cur_lines.append(line)

    flush()
    return segments


def normalize_segments(segments):
    removed = []
    filtered = []

    # Treat these as explicit non-dialogue / placeholder tokens.
    # They must not produce "fake speech" segments.
    NON_DIALOGUE_MARKERS = {
        "[BLANK_AUDIO]": "BLANK_AUDIO",
        "[empty transcript]": "EMPTY_TRANSCRIPT_PLACEHOLDER",
    }

    for seg in segments:
        t = (seg.get("text") or "").strip()

        if t in NON_DIALOGUE_MARKERS:
            removed.append(NON_DIALOGUE_MARKERS[t])
            continue

        # Basic timestamp sanity (drop obviously invalid segments)
        st = seg.get("start")
        en = seg.get("end")
        try:
            stf = float(st)
            enf = float(en)
        except Exception:
            removed.append("BAD_TIMESTAMP")
            continue
        if stf < 0 or enf < 0 or enf < stf:
            removed.append("BAD_TIMESTAMP")
            continue

        seg["start"] = stf
        seg["end"] = enf
        seg["text"] = t
        filtered.append(seg)

    # Keep an internal marker for traceability
    reason_raw = ""
    if not filtered and segments and removed:
        # Preserve the old marker name when the classic blank-audio token triggered emptiness.
        if "BLANK_AUDIO" in removed:
            reason_raw = "blank_audio"

    return filtered, removed, reason_raw


def map_reason_to_contract(empty: bool, reason_raw: str) -> str:
    """
    Contract wants enumerated reason codes, not prose.
    """
    if not empty:
        return ""

    if reason_raw == "blank_audio":
        return REASON_SILENT_AUDIO

    return REASON_NON_SPEECH_AUDIO


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--srt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--media-id", required=True)
    ap.add_argument("--engine", default="whispercpp")
    ap.add_argument("--vad-used", choices=["0", "1"], required=True)
    args = ap.parse_args()

    srt_path = Path(args.srt)
    out_path = Path(args.out)

    if not srt_path.is_file():
        die(f"SRT does not exist: {srt_path}")

    mid = args.media_id.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", mid):
        die(f"media_id must be 64 lowercase hex sha256, got: {mid!r}")

    segments = parse_srt(srt_path)
    filtered, removed, reason_raw = normalize_segments(segments)

    empty = (len(filtered) == 0)

    # Contract compliance:
    # - Stage A must write quality.*
    # - Empty ASR must set quality.status="fail" and reason ASR_EMPTY
    quality_status = "ok"
    quality_reasons = []
    if empty:
        quality_status = "fail"
        quality_reasons = ["ASR_EMPTY"]

    reason = map_reason_to_contract(empty=empty, reason_raw=reason_raw)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "media_id": mid,
        "engine": args.engine,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "segments": filtered,
        "empty_transcript": empty,
        "vad_used": (args.vad_used == "1"),
        # Contract compliance: Stage A is origin of policy.*
        "policy": {
            "allow_indexing": True,
            "allow_serving": True,
        },
        "quality": {
            "status": quality_status,
            "reasons": quality_reasons,
            "stats": {
                "segments_raw": int(len(segments)),
                "segments_kept": int(len(filtered)),
                # Count of removals (not unique types)
                "non_dialogue_tokens_removed_count": int(len(removed)),
            },
        },
    }

    # Only include reason fields when empty, to avoid downstream ambiguity.
    if empty:
        payload["reason"] = reason
        if reason_raw:
            payload["reason_raw"] = reason_raw

    if removed:
        payload["non_dialogue_tokens_removed"] = removed

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"OK: wrote {out_path}")


if __name__ == "__main__":
    main()