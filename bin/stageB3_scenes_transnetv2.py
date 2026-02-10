#!/usr/bin/env python3
"""
stageB3_scenes_transnetv2.py

Canonical scene detection using TransNetV2 (PyTorch) running in cpu_vision venv.

Writes canonical artifacts:
  /workspace/catalog/scenes/<media_id>.json

Inputs:
  /workspace/catalog/proxies/<media_id>.mp4

Notes:
- This stage is intended to replace PySceneDetect ContentDetector as the canonical source of scenes.
- Determinism: device is pinned to CPU.
- Atomic write: temp file then os.replace.
- Explicit outcome: always writes an artifact with outcome, even on failure.

Run in venv:
  source /workspace/venvs/cpu_vision/bin/activate
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import warnings
from importlib import metadata

# Hard guard, this must run in cpu_vision
try:
    import torch  # noqa: F401
    from transnetv2_pytorch import TransNetV2
except Exception as e:
    raise SystemExit(f"Missing deps in active venv. Activate cpu_vision. Import error: {type(e).__name__}: {e}")


CATALOG_SCENES_DIR = "/workspace/catalog/scenes"
CATALOG_PROXIES_DIR = "/workspace/catalog/proxies"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_now_iso_z() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def build_artifact(media_id: str, video_path: str) -> dict:
    # Reduce noise from upstream torch.load warning, weights are local to the installed wheel
    warnings.filterwarnings("ignore", category=FutureWarning, message="You are using `torch.load` with `weights_only=False`*")

    out = {
        "schema_version": 1,
        "record_type": "scenes",
        "detector": "TransNetV2",
        "media_id": media_id,
        "created_utc": utc_now_iso_z(),
        "input": {
            "path": video_path,
            "sha256": "",
        },
        "model": {
            "package": "transnetv2-pytorch",
            "package_version": "",
            "weights_path": "",
            "weights_sha256": "",
            "device": "cpu",
            "input_size_hwc": [27, 48, 3],
        },
        "runtime": {
            "python_version": sys.version.split()[0],
            "python_executable": sys.executable,
            "torch_version": "",
        },
        "config": {
            # Explicitly record init defaults used by the package
            "F": 16,
            "L": 3,
            "S": 2,
            "D": 1024,
            "use_many_hot_targets": True,
            "use_frame_similarity": True,
            "use_color_histograms": True,
            "use_mean_pooling": False,
            "dropout_rate": 0.5,
            "frame_similarity_on_last_layer": False,
            "device": "cpu",
        },
        "outcome": {
            "ok": False,
            "skipped": False,
            "blocked": False,
            "reason": "INIT",
            "detail": "",
        },
        "video": {
            "fps": None,
        },
        "scenes": [],
        "scene_timestamps": [],
        "stats": {
            "scenes_found": 0,
        },
    }

    if not os.path.exists(video_path):
        out["outcome"] = {
            "ok": False,
            "skipped": True,
            "blocked": False,
            "reason": "MISSING_INPUT",
            "detail": f"proxy not found: {video_path}",
        }
        return out

    # Populate input hash
    out["input"]["sha256"] = sha256_file(video_path)

    # Populate runtime and model identity
    out["runtime"]["torch_version"] = __import__("torch").__version__
    try:
        out["model"]["package_version"] = metadata.version("transnetv2-pytorch")
    except Exception:
        out["model"]["package_version"] = ""

    # Weight path inside installed package
    import transnetv2_pytorch as pkg  # type: ignore
    weights_path = os.path.join(os.path.dirname(pkg.__file__), "transnetv2-pytorch-weights.pth")
    out["model"]["weights_path"] = weights_path
    if os.path.exists(weights_path):
        out["model"]["weights_sha256"] = sha256_file(weights_path)

    try:
        m = TransNetV2(device="cpu")
        fps = float(m.get_video_fps(video_path))
        scenes = m.detect_scenes(video_path)

        try:
            timestamps = m.get_scene_timestamps(video_path)
        except Exception:
            timestamps = []

        out["video"]["fps"] = fps
        out["scenes"] = scenes if isinstance(scenes, list) else []
        out["scene_timestamps"] = timestamps if isinstance(timestamps, list) else []
        out["stats"]["scenes_found"] = len(out["scenes"])

        out["outcome"] = {
            "ok": True,
            "skipped": False,
            "blocked": False,
            "reason": "OK",
            "detail": "",
        }
        return out

    except Exception as e:
        out["outcome"] = {
            "ok": False,
            "skipped": False,
            "blocked": False,
            "reason": "DETECT_FAILED",
            "detail": f"{type(e).__name__}: {str(e)}",
        }
        return out


def atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage B3 scenes via TransNetV2 (canonical writer).")
    ap.add_argument("--media_id", required=True, help="media_id (sha256) matching proxies/<media_id>.mp4")
    ap.add_argument("--proxies_dir", default=CATALOG_PROXIES_DIR, help="Proxies dir, default /workspace/catalog/proxies")
    ap.add_argument("--scenes_dir", default=CATALOG_SCENES_DIR, help="Scenes dir, default /workspace/catalog/scenes")
    args = ap.parse_args()

    media_id = args.media_id.strip()
    if not media_id:
        print(json.dumps({"ok": False, "error": "missing media_id"}))
        return 2

    os.makedirs(args.scenes_dir, exist_ok=True)

    video_path = os.path.join(args.proxies_dir, f"{media_id}.mp4")
    artifact = build_artifact(media_id, video_path)

    out_path = os.path.join(args.scenes_dir, f"{media_id}.json")
    atomic_write_json(out_path, artifact)

    print(out_path)
    return 0 if artifact.get("outcome", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())