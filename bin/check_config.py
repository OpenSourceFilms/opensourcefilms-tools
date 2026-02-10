import os
import sys
import shutil
from pathlib import Path

import yaml

CONFIG_PATH = Path("/workspace/pipeline/conf/pipeline.yaml")

REQUIRED_TOP_LEVEL_DIRS = [
    "ingest",
    "state",
    "catalog",
    "index",
    "services",
    "pipeline",
    "tmp",
]

REQUIRED_SUBDIRS = [
    "/workspace/state/queue_cpu",
    "/workspace/state/processing_cpu",
    "/workspace/catalog/media_manifest",
    "/workspace/catalog/chunks",
    "/workspace/catalog/asr",
    "/workspace/catalog/frames",
    "/workspace/index/milvus",
    "/workspace/index/embed_cache",
]

REQUIRED_BINARIES = ["ffprobe", "ffmpeg"]


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    if not CONFIG_PATH.exists():
        die(f"Missing config: {CONFIG_PATH}")

    cfg = yaml.safe_load(CONFIG_PATH.read_text())

    # Minimal structural checks
    if not isinstance(cfg, dict):
        die("pipeline.yaml did not parse into a dict")

    paths = cfg.get("paths", {})
    for k in ["ingest_root", "catalog_root", "state_root", "index_root", "tmp_root"]:
        if k not in paths:
            die(f"Missing paths.{k} in pipeline.yaml")

    # Check top-level dirs
    for d in REQUIRED_TOP_LEVEL_DIRS:
        p = Path("/workspace") / d
        if not p.exists():
            die(f"Missing required dir: {p}")

    # Check required subdirs
    for p in REQUIRED_SUBDIRS:
        if not Path(p).exists():
            die(f"Missing required subdir: {p}")

    # Check binaries
    missing_bins = [b for b in REQUIRED_BINARIES if shutil.which(b) is None]
    if missing_bins:
        die(f"Missing required binaries in PATH: {missing_bins}")

    print("OK: config parsed and required dirs/binaries present")
    print(f"Config: {CONFIG_PATH}")
    print("Resolved roots:")
    print(f"  ingest_root  = {paths['ingest_root']}")
    print(f"  catalog_root = {paths['catalog_root']}")
    print(f"  state_root   = {paths['state_root']}")
    print(f"  index_root   = {paths['index_root']}")
    print(f"  tmp_root     = {paths['tmp_root']}")


if __name__ == "__main__":
    main()
