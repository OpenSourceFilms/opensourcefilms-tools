#!/usr/bin/env python3
import argparse
import json
import os
import sys

def die(msg):
    print("FATAL:", msg, file=sys.stderr)
    sys.exit(2)

def load_json(path, ctx):
    if not os.path.isfile(path):
        die(f"missing file for {ctx}: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        die(f"failed to read JSON for {ctx}: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--mid", required=True)
    ap.add_argument("--kf-fps", required=True, type=float)
    args = ap.parse_args()

    IN = args.inp
    MID = args.mid
    KF_FPS = args.kf_fps

    if not os.path.isabs(IN):
        die("--in must be absolute")
    if not os.path.isfile(IN):
        die(f"input file missing: {IN}")

    manifest_path = f"/workspace/catalog/media_manifest/{MID}.json"
    enrich_path = f"/workspace/catalog/enrich/{MID}.json"
    status_path = f"/workspace/catalog/status/{MID}.json"
    kf_dir = f"/workspace/catalog/keyframes/{MID}"

    m = load_json(manifest_path, "manifest")
    if m.get("media_id") != MID:
        die("manifest media_id mismatch")
    if m.get("source_path") != IN:
        die("manifest source_path mismatch")

    e = load_json(enrich_path, "enrich")
    if e.get("media_id") != MID:
        die("enrich media_id mismatch")
    if e.get("stage") != "B":
        die("enrich stage != B")
    if e.get("status") != "ok":
        die("enrich status != ok")

    fps = e.get("method", {}).get("keyframes", {}).get("fps")
    if fps is None:
        die("missing method.keyframes.fps")
    if float(fps) != KF_FPS:
        die(f"fps mismatch: expected {KF_FPS}, got {fps}")

    if not os.path.isdir(kf_dir):
        die("missing keyframes dir")

    frames = e.get("frames")
    if not isinstance(frames, list) or not frames:
        die("frames list invalid")

    for fr in frames:
        p = fr.get("path")
        if not p or not os.path.isfile(p):
            die(f"missing frame file: {p}")

    s = load_json(status_path, "status")
    sb = s.get("stages", {}).get("stageB")
    if not sb:
        die("status missing stageB")

    if sb.get("enrich_json") != enrich_path:
        die("status enrich_json mismatch")
    if sb.get("keyframes_dir") != kf_dir:
        die("status keyframes_dir mismatch")

    print("OK: contract_check passed")

if __name__ == "__main__":
    main()
