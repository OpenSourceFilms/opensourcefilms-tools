#!/usr/bin/env python3
"""
Stage B2 (Motion): Dense optical flow summaries (CPU)

Writes one artifact per media_id:
  /workspace/catalog/motion/<media_id>.json

Design goals:
- CPU-only, deterministic
- No dependency on ComfyUI
- Explicit outcomes (never infer from absence)
- Atomic writes, no 0-byte artifacts
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np


SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if len(data.strip()) == 0:
        raise RuntimeError("refusing to write empty artifact")
    tmp.write_text(data, encoding="utf-8")
    os.replace(str(tmp), str(path))


def get_media_id(in_path: str) -> str:
    # Prefer shell helper if available, otherwise hash bytes here.
    # Keep it explicit and deterministic, no timestamps.
    # This matches your pipeline convention: sha256(full input bytes).
    try:
        # If user has get_media_id in PATH (lib_paths.sh exports it sometimes)
        import subprocess

        p = subprocess.run(["bash", "-lc", f'command -v get_media_id >/dev/null 2>&1 && get_media_id "{in_path}"'],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        out = (p.stdout or "").strip()
        if len(out) == 64 and all(c in "0123456789abcdef" for c in out):
            return out
    except Exception:
        pass

    return sha256_file(in_path)


def open_video(in_path: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(in_path)
    return cap


def read_gray_frame(cap: cv2.VideoCapture, resize_w: int) -> Tuple[bool, Optional[np.ndarray]]:
    ok, frame = cap.read()
    if not ok or frame is None:
        return False, None

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    if resize_w > 0:
        h, w = gray.shape[:2]
        if w > resize_w:
            scale = float(resize_w) / float(w)
            new_h = max(2, int(round(h * scale)))
            gray = cv2.resize(gray, (resize_w, new_h), interpolation=cv2.INTER_AREA)

    return True, gray


def flow_stats_from_pair(prev: np.ndarray, cur: np.ndarray, fb_params: Dict[str, Any]) -> Dict[str, float]:
    flow = cv2.calcOpticalFlowFarneback(
        prev,
        cur,
        None,
        pyr_scale=float(fb_params["pyr_scale"]),
        levels=int(fb_params["levels"]),
        winsize=int(fb_params["winsize"]),
        iterations=int(fb_params["iterations"]),
        poly_n=int(fb_params["poly_n"]),
        poly_sigma=float(fb_params["poly_sigma"]),
        flags=int(fb_params["flags"]),
    )

    dx = flow[..., 0]
    dy = flow[..., 1]
    mag = np.sqrt(dx * dx + dy * dy)

    # Use float() to make JSON clean
    return {
        "mean_dx": float(dx.mean()),
        "mean_dy": float(dy.mean()),
        "mean_mag": float(mag.mean()),
        "median_mag": float(np.median(mag)),
        "p95_mag": float(np.quantile(mag, 0.95)),
        "max_mag": float(mag.max()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Absolute input media path")
    ap.add_argument("--mid", default="", help="Optional explicit media_id (64-hex). If empty, compute from bytes.")
    ap.add_argument("--out_dir", default="/workspace/catalog/motion", help="Output directory for motion artifacts")
    ap.add_argument("--resize_w", type=int, default=320, help="Resize width for analysis (0 disables resize)")
    ap.add_argument("--stride", type=int, default=1, help="Frame stride (1 = every frame, 2 = every other)")
    ap.add_argument("--max_pairs", type=int, default=300, help="Max frame pairs to process (limits runtime)")
    ap.add_argument("--write_per_pair", action="store_true", default=False, help="Include per-pair stats array (bigger files)")

    args = ap.parse_args()

    in_path = args.inp
    if not os.path.isabs(in_path):
        raise SystemExit("FATAL: --in must be absolute")
    if not os.path.isfile(in_path):
        raise SystemExit(f"FATAL: input missing: {in_path}")

    mid = (args.mid or "").strip()
    if mid:
        if len(mid) != 64 or any(c not in "0123456789abcdef" for c in mid):
            raise SystemExit(f"FATAL: --mid must be 64 lowercase hex sha256, got: {mid!r}")
    else:
        mid = get_media_id(in_path)

    out_dir = Path(args.out_dir)
    out_path = out_dir / f"{mid}.json"

    fb_params = {
        "pyr_scale": 0.5,
        "levels": 3,
        "winsize": 15,
        "iterations": 3,
        "poly_n": 5,
        "poly_sigma": 1.2,
        "flags": 0,
    }
    cfg_sha = sha256_bytes(canonical_json_bytes(
        {
            "resize_w": int(args.resize_w),
            "stride": int(args.stride),
            "max_pairs": int(args.max_pairs),
            "farneback": fb_params,
        }
    ))

    # Default artifact skeleton (explicit outcome first, then fill)
    artifact: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "motion_flow",
        "media_id": mid,
        "created_utc": utc_now_iso(),
        "input": {
            "path": in_path,
            "sha256": sha256_file(in_path),
        },
        "config": {
            "resize_w": int(args.resize_w),
            "stride": int(args.stride),
            "max_pairs": int(args.max_pairs),
            "farneback": fb_params,
        },
        "config_sha256": cfg_sha,
        "runtime": {
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "opencv_version": getattr(cv2, "__version__", "unknown"),
            "numpy_version": getattr(np, "__version__", "unknown"),
            "device": "cpu",
        },
        "outcome": {
            "ok": False,
            "skipped": False,
            "blocked": False,
            "reason": "",
            "detail": "",
        },
        "stats": {},
    }

    stride = max(1, int(args.stride))
    max_pairs = max(0, int(args.max_pairs))

    cap = open_video(in_path)
    if not cap.isOpened():
        artifact["outcome"] = {
            "ok": False,
            "skipped": False,
            "blocked": False,
            "reason": "VIDEO_OPEN_FAILED",
            "detail": "cv2.VideoCapture could not open input",
        }
        atomic_write_json(out_path, artifact)
        print("WROTE:", str(out_path))
        raise SystemExit(2)

    # Read first frame
    ok, prev = read_gray_frame(cap, int(args.resize_w))
    if not ok or prev is None:
        cap.release()
        artifact["outcome"] = {
            "ok": True,
            "skipped": True,
            "blocked": False,
            "reason": "NO_FRAMES",
            "detail": "video opened but yielded zero frames",
        }
        atomic_write_json(out_path, artifact)
        print("WROTE:", str(out_path))
        return

    # Apply stride by skipping frames after each read
    def skip_n(n: int) -> None:
        for _ in range(n):
            _ok = cap.grab()
            if not _ok:
                break

    pair_stats: List[Dict[str, float]] = []
    mags: List[float] = []
    mean_dx_list: List[float] = []
    mean_dy_list: List[float] = []

    pairs_done = 0
    frames_read = 1

    # Main loop
    while True:
        if max_pairs and pairs_done >= max_pairs:
            break

        # stride-1 grabs between reads
        if stride > 1:
            skip_n(stride - 1)

        ok2, cur = read_gray_frame(cap, int(args.resize_w))
        if not ok2 or cur is None:
            break

        frames_read += stride

        st = flow_stats_from_pair(prev, cur, fb_params)
        pairs_done += 1

        mags.append(st["mean_mag"])
        mean_dx_list.append(st["mean_dx"])
        mean_dy_list.append(st["mean_dy"])

        if args.write_per_pair:
            pair_stats.append(st)

        prev = cur

    cap.release()

    if pairs_done == 0:
        artifact["outcome"] = {
            "ok": True,
            "skipped": True,
            "blocked": False,
            "reason": "TOO_SHORT",
            "detail": "only one readable frame, no pairs to compute flow",
        }
        artifact["stats"] = {
            "pairs": 0,
            "frames_read": int(frames_read),
        }
        atomic_write_json(out_path, artifact)
        print("WROTE:", str(out_path))
        return

    mags_np = np.array(mags, dtype=np.float32)
    dx_np = np.array(mean_dx_list, dtype=np.float32)
    dy_np = np.array(mean_dy_list, dtype=np.float32)

    artifact["outcome"] = {
        "ok": True,
        "skipped": False,
        "blocked": False,
        "reason": "OK",
        "detail": "",
    }
    artifact["stats"] = {
        "pairs": int(pairs_done),
        "frames_read": int(frames_read),
        "flow_mean_mag": float(mags_np.mean()),
        "flow_median_mag": float(np.median(mags_np)),
        "flow_p95_mag": float(np.quantile(mags_np, 0.95)),
        "flow_max_mag": float(mags_np.max()),
        "flow_mean_dx": float(dx_np.mean()),
        "flow_mean_dy": float(dy_np.mean()),
    }

    if args.write_per_pair:
        artifact["per_pair"] = pair_stats

    atomic_write_json(out_path, artifact)
    print("WROTE:", str(out_path))
    print("media_id:", mid, "pairs:", pairs_done, "mean_mag:", artifact["stats"]["flow_mean_mag"])


if __name__ == "__main__":
    main()
    