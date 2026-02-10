#!/usr/bin/env python3
import json
import os
import sys
import tempfile

from pathlib import Path
import shutil

RUNS_DIR = Path(os.environ.get("RUNS_DIR", "/workspace/catalog/runs"))

def maybe_write_run_history(status_path: Path, media_id: str) -> None:
    run_id = os.environ.get("RUN_ID", "").strip()
    if not run_id:
        return
    out_dir = RUNS_DIR / media_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}.json"
    shutil.copy2(status_path, out_path)
from datetime import datetime, timezone

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)

            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass

def main():
    if len(sys.argv) < 5:
        print("usage: status_write.py <status_json> <media_id> <stage> <ok:0|1> [key=value ...]", file=sys.stderr)
        return 2

    status_path = Path(sys.argv[1])
    media_id = sys.argv[2]
    stage = sys.argv[3]
    ok = sys.argv[4] == "1"
    kv = sys.argv[5:]

    # Load existing, or initialise
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text())
        except Exception:
            data = {}
    else:
        data = {}

    data.setdefault("schema_version", 1)
    data.setdefault("media_id", media_id)
    data.setdefault("stages", {})

    stage_obj = data["stages"].get(stage, {})
    stage_obj["ok"] = ok
    stage_obj["at_utc"] = utc_now()
    if not ok:
        stage_obj.setdefault("error", None)
    else:
        stage_obj.pop("error", None)

    # Apply key=value patches, supports dotted keys under the stage object only.
    # Example: outputs.srt=/path/to/file
    for item in kv:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        cur = stage_obj
        parts = k.split(".")
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                cur[p] = {}
            cur = cur[p]
        # Minimal type coercion
        if v.lower() == "true":
            vv = True
        elif v.lower() == "false":
            vv = False
        else:
            try:
                vv = int(v)
            except Exception:
                vv = v
        cur[parts[-1]] = vv

    data["stages"][stage] = stage_obj
    atomic_write_json(status_path, data)
    print(f"OK: wrote {status_path}")

if __name__ == "__main__":
    raise SystemExit(main())
