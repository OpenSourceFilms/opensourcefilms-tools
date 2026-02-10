#!/usr/bin/env python3
import argparse, csv, json, os, re, subprocess, sys
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlencode

WEBROOT_INDEX = Path("/workspace/webroot/index.json")
EXPORT_DIR = Path("/workspace/catalog/resolve_exports")

def ffprobe_fps(path: str) -> int:
    # Returns integer fps for proxy by rounding avg_frame_rate.
    # Falls back to 30 if ffprobe fails.
    try:
        out = subprocess.check_output([
            "ffprobe","-v","error","-select_streams","v:0",
            "-show_entries","stream=avg_frame_rate",
            "-of","default=nk=1:nw=1", path
        ], text=True).strip()
        if not out:
            return 30
        num, den = out.split("/", 1)
        num = float(num); den = float(den)
        if den == 0:
            return 30
        fps = num / den
        return max(1, int(round(fps)))
    except Exception:
        return 30

def sec_to_tc(t: float, fps: int) -> str:
    # Option 1 policy: clip-relative, start at 00:00:00:00, non-drop, round(t*fps).
    total_frames = int(round(float(t) * fps))
    hh = total_frames // (fps * 3600)
    mm = (total_frames // (fps * 60)) % 60
    ss = (total_frames // fps) % 60
    ff = total_frames % fps
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

def sanitize(s: str, max_len: int = 80) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:max_len]

def load_index_map():
    obj = json.loads(WEBROOT_INDEX.read_text(encoding="utf-8"))
    m = {}
    for r in obj.get("records", []):
        mid = r.get("media_id")
        if mid:
            m[mid] = r
    return m

def fetch_hits(q: str, k: int, api_base: str):
    url = f"{api_base}/api/semantic?{urlencode({'q': q, 'k': k})}"
    with urlopen(url) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--api", default="http://127.0.0.1:3000")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    idx = load_index_map()
    data = fetch_hits(args.q, args.k, args.api)
    hits = data.get("results", [])

    # Build rows
    rows = []
    fps_cache = {}

    for h in hits:
        mid = h.get("media_id")
        start_s = float(h.get("start_s", 0.0) or 0.0)
        text = h.get("text", "") or ""
        rec = idx.get(mid, {}) if mid else {}

        # Resolve-friendly strings
        source_name = rec.get("source_name", "") or ""
        proxy_rel = rec.get("proxy_path", "") or ""
        proxy_abs = ("/workspace/" + proxy_rel) if proxy_rel and not proxy_rel.startswith("/workspace/") else proxy_rel

        fps = fps_cache.get(proxy_abs)
        if fps is None:
            fps = ffprobe_fps(proxy_abs) if proxy_abs else 30
            fps_cache[proxy_abs] = fps

        tc_in = sec_to_tc(start_s, fps)

        name = sanitize(f"{args.q} | {source_name}", 80)
        comment = sanitize(f"{text}  [start_s={start_s:.3f}, fps={fps}, media_id={mid}]", 200)

        rows.append({
            "Timecode In": tc_in,
            "Name": name,
            "Comment": comment,
        })

    # Output path
    out = args.out.strip()
    if not out:
        safe_q = re.sub(r"[^A-Za-z0-9_-]+", "_", args.q.strip())[:60]
        out = str(EXPORT_DIR / f"markers_{safe_q}_k{args.k}.csv")

    # Write CSV
    fieldnames = ["Timecode In", "Name", "Comment"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(out)

if __name__ == "__main__":
    main()
