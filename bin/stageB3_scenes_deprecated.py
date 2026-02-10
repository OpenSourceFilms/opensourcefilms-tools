#!/usr/bin/env python3
"""
Stage B3 (Scenes): Shot/scene boundary detection (CPU) using PySceneDetect.

Writes one artifact per media_id:
  /workspace/catalog/scenes/<media_id>.json

Design goals:
- CPU-only, deterministic (given same input + params)
- Explicit outcomes (never infer from absence)
- Atomic writes, no 0-byte artifacts
- Records tool versions + config for auditability
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, List

from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector
import scenedetect


SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if len(data.strip()) == 0:
        raise RuntimeError("refusing to write empty artifact")
    tmp.write_text(data, encoding="utf-8")
    os.replace(str(tmp), str(path))


def get_media_id(in_path: str) -> str:
    # Deterministic: sha256(full input bytes)
    return sha256_file(in_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Absolute input media path")
    ap.add_argument("--mid", default="", help="Optional explicit media_id (64-hex). If empty, compute from bytes.")
    ap.add_argument("--out_dir", default="/workspace/catalog/scenes", help="Output directory for scene artifacts")
    ap.add_argument("--threshold", type=float, default=27.0, help="ContentDetector threshold (lower = more cuts)")
    ap.add_argument("--min_scene_len", type=int, default=15, help="Minimum scene length in frames")
    ap.add_argument("--max_scenes", type=int, default=0, help="If >0, stop after this many scenes found")

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

    out_path = Path(args.out_dir) / f"{mid}.json"

    artifact: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "scenes",
        "media_id": mid,
        "created_utc": utc_now_iso(),
        "input": {
            "path": in_path,
            "sha256": sha256_file(in_path),
        },
        "config": {
            "detector": "ContentDetector",
            "threshold": float(args.threshold),
            "min_scene_len_frames": int(args.min_scene_len),
            "max_scenes": int(args.max_scenes),
        },
        "runtime": {
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "scenedetect_version": getattr(scenedetect, "__version__", "unknown"),
            "device": "cpu",
        },
        "outcome": {
            "ok": False,
            "skipped": False,
            "blocked": False,
            "reason": "",
            "detail": "",
        },
        "scenes": [],
        "stats": {},
    }

    try:
        video = open_video(in_path)
    except Exception as e:
        artifact["outcome"] = {
            "ok": False,
            "skipped": False,
            "blocked": False,
            "reason": "VIDEO_OPEN_FAILED",
            "detail": repr(e),
        }
        atomic_write_json(out_path, artifact)
        print("WROTE:", str(out_path))
        raise SystemExit(2)

    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=args.threshold, min_scene_len=args.min_scene_len))

    try:
        scene_manager.detect_scenes(video=video, show_progress=False)
        scene_list = scene_manager.get_scene_list()
    except Exception as e:
        artifact["outcome"] = {
            "ok": False,
            "skipped": False,
            "blocked": False,
            "reason": "SCENE_DETECT_FAILED",
            "detail": repr(e),
        }
        atomic_write_json(out_path, artifact)
        print("WROTE:", str(out_path))
        raise SystemExit(2)

    # Convert to a simple JSON-friendly structure
    out_scenes: List[Dict[str, Any]] = []
    for i, (start, end) in enumerate(scene_list):
        if args.max_scenes and i >= args.max_scenes:
            break
        out_scenes.append(
            {
                "index": int(i),
                "start": {
                    "frame": int(start.get_frames()),
                    "timecode": str(start.get_timecode()),
                    "seconds": float(start.get_seconds()),
                },
                "end": {
                    "frame": int(end.get_frames()),
                    "timecode": str(end.get_timecode()),
                    "seconds": float(end.get_seconds()),
                },
            }
        )

    artifact["scenes"] = out_scenes
    artifact["stats"] = {
        "scenes_found": int(len(out_scenes)),
        "scenes_detected_total": int(len(scene_list)),
    }

    artifact["outcome"] = {
        "ok": True,
        "skipped": False,
        "blocked": False,
        "reason": "OK",
        "detail": "",
    }

    atomic_write_json(out_path, artifact)
    print("WROTE:", str(out_path))
    print("media_id:", mid, "scenes_found:", len(out_scenes))


if __name__ == "__main__":
    main()