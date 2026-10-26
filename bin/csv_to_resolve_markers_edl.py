#!/usr/bin/env python3
import argparse, csv, re
from pathlib import Path

def parse_tc(tc: str):
    m = re.match(r"^(\d+):([0-5]\d):([0-5]\d):([0-5]\d)$", (tc or "").strip())
    if not m:
        raise ValueError(f"bad timecode: {tc}")
    return tuple(int(x) for x in m.groups())

def validate_non_drop_timecode(tc, fps):
    """Reject timecodes that would silently wrap at the given integer FPS."""
    if not 1 <= fps <= 60:
        raise ValueError(f"non-drop EDL requires integer fps in 1..60, got {fps}")
    if not 0 <= tc[3] < fps:
        raise ValueError(f"frame field {tc[3]:02d} outside 00..{fps - 1:02d} at {fps} fps")


def add_frames(tc, add_ff, fps):
    validate_non_drop_timecode(tc, fps)
    hh, mm, ss, ff = tc
    total = (((hh*60 + mm)*60 + ss) * fps) + ff
    total += add_ff
    if total < 0:
        total = 0
    hh = total // (fps*3600)
    mm = (total // (fps*60)) % 60
    ss = (total // fps) % 60
    ff = total % fps
    return (hh, mm, ss, ff)

def fmt_tc(tc):
    hh, mm, ss, ff = tc
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

def edl_reel(s: str, max_len: int = 24) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s)
    return (s or "REEL")[:max_len]

def wrap_edl_comment(s: str, prefix: str, width: int = 70):
    # Conservative width for EDL comment lines.
    s = (s or "").strip()
    if not s:
        return []
    out = []
    while s:
        chunk = s[:width]
        s = s[width:]
        out.append(f"{prefix}{chunk}")
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in_csv", required=True)
    ap.add_argument("--out_edl", required=True)
    ap.add_argument("--marker_len_frames", type=int, default=1)
    ap.add_argument("--fps_default", type=int, default=30)
    ap.add_argument("--title", default="MEDIA_INTEL_MARKERS")
    args = ap.parse_args()
    if not 1 <= args.fps_default <= 60:
        ap.error("--fps_default must be an integer from 1 to 60 (non-drop-frame EDL)")
    if args.marker_len_frames <= 0:
        ap.error("--marker_len_frames must be positive")

    rows = []
    with open(args.in_csv, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)

    out = []
    out.append(f"TITLE: {args.title}")
    out.append("FCM: NON-DROP FRAME")
    out.append("")

    event = 1
    for row in rows:
        tc_in = (row.get("Timecode In") or "").strip()
        name = (row.get("Name") or "").strip()
        comment = (row.get("Comment") or "").strip()

        fps = args.fps_default
        m = re.search(r"\bfps=([0-9]+(?:\.[0-9]+)?)\b", comment)
        if m:
            # CMX non-drop HH:MM:SS:FF uses integer frames-per-second.
            # A decimal here previously truncated, e.g. 23.976 became 23.
            if "." in m.group(1):
                raise ValueError(
                    f"row {event}: fractional fps={m.group(1)} is unsupported "
                    "by this integer non-drop-frame exporter"
                )
            fps = int(m.group(1))

        in_tc = parse_tc(tc_in)
        try:
            validate_non_drop_timecode(in_tc, fps)
        except ValueError as exc:
            raise ValueError(f"row {event}: {exc}") from exc
        out_tc = add_frames(in_tc, args.marker_len_frames, fps)

        reel = edl_reel(name.split("|",1)[-1] if "|" in name else name)

        out.append(f"{event:03d}  {reel:<8} V     C        {fmt_tc(in_tc)} {fmt_tc(out_tc)} {fmt_tc(in_tc)} {fmt_tc(out_tc)}")

        # Split long metadata into multiple lines to avoid truncation and parsing issues.
        for line in wrap_edl_comment(name, "* FROM CLIP NAME: ", width=70):
            out.append(line)
        for line in wrap_edl_comment(comment, "* COMMENT: ", width=70):
            out.append(line)

        out.append("")
        event += 1

    Path(args.out_edl).write_text("\n".join(out), encoding="utf-8")
    print(args.out_edl)

if __name__ == "__main__":
    main()
