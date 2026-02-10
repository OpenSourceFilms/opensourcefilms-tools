#!/usr/bin/env python3
import os
import sys
import time
import subprocess
from pathlib import Path

LIST = Path("/workspace/catalog/input_list_stagePAB.txt")
PIPE_DIR = Path("/workspace/pipeline/bin")
VENV_PY = Path("/workspace/venvs/cpu/bin/python")
SERVICE = "media_webroot"

def ts_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def sh(cmd, check=True, capture=False, env=None):
    if capture:
        return subprocess.run(cmd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    return subprocess.run(cmd, check=check, env=env)

def main():
    run_ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    batch_dir = Path(f"/workspace/catalog/logs/batch_{run_ts}")
    batch_dir.mkdir(parents=True, exist_ok=True)

    master_log = batch_dir / "batch_master.log"
    ok_list = batch_dir / "ok.txt"
    fail_list = batch_dir / "fail.txt"

    def log(msg: str):
        line = f"[{ts_utc()}] {msg}\n"
        master_log.open("a", encoding="utf-8").write(line)
        sys.stdout.write(line)
        sys.stdout.flush()

    log("BATCH START")
    log(f"LIST={LIST}")
    log(f"BATCH_DIR={batch_dir}")
    log(f"SERVICE={SERVICE}")
    log(f"PYTHON={VENV_PY}")

    if not LIST.exists():
        log(f"FATAL: list missing: {LIST}")
        return 2
    if not VENV_PY.exists():
        log(f"FATAL: venv python missing: {VENV_PY}")
        return 2

    # Stop service once, to avoid Milvus Lite DB conflicts
    log(f"Stopping {SERVICE}...")
    sh(["supervisorctl", "stop", SERVICE], check=False)
    r = sh(["supervisorctl", "status", SERVICE], check=False, capture=True)
    log("Supervisor status: " + (r.stdout or "").strip())

    # Read list in a byte-safe way
    raw_lines = LIST.read_bytes().splitlines()
    paths = []
    for b in raw_lines:
        # Strip CR, decode as utf-8 (paths are normal ASCII/UTF-8)
        b = b.replace(b"\r", b"").strip()
        if not b:
            continue
        s = b.decode("utf-8", errors="strict")
        paths.append(s)

    total = len(paths)
    log(f"Total inputs: {total}")

    for i, in_path in enumerate(paths, start=1):
        p = Path(in_path)
        if not p.is_file():
            log(f"WARN [{i}/{total}]: missing file, skipping: {in_path}")
            fail_list.open("a", encoding="utf-8").write(in_path + "\n")
            continue

        safe_name = in_path.replace("/", "_").replace(" ", "_")
        per_log = batch_dir / f"item_{i:04d}_{safe_name}.log"

        log(f"DO [{i}/{total}]: {in_path}")

        env = os.environ.copy()
        env["IN"] = in_path

        # Run pipeline_run.sh (Stage A then Stage B) and capture all output
        with per_log.open("w", encoding="utf-8") as f:
            proc = subprocess.run(
                ["/usr/bin/env", "bash", str(PIPE_DIR / "pipeline_run.sh")],
                cwd=str(PIPE_DIR),
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
            )
        rc = proc.returncode

        if rc == 0:
            log(f"OK [{i}/{total}]: rc=0")
            ok_list.open("a", encoding="utf-8").write(in_path + "\n")
        else:
            log(f"FAIL [{i}/{total}]: rc={rc} (see {per_log})")
            fail_list.open("a", encoding="utf-8").write(in_path + "\n")

    # Stage P over all manifests (proxies)
    log("Stage P: run_stageP_all.sh")
    p_log = batch_dir / "stageP_all.log"
    with p_log.open("w", encoding="utf-8") as f:
        proc = subprocess.run(
            ["/usr/bin/env", "bash", str(PIPE_DIR / "run_stageP_all.sh")],
            cwd=str(PIPE_DIR),
            env=os.environ.copy(),
            stdout=f,
            stderr=subprocess.STDOUT,
            text=True,
        )
    log(f"Stage P done: rc={proc.returncode} log={p_log}")

    # Restart service
    log(f"Starting {SERVICE}...")
    sh(["supervisorctl", "start", SERVICE], check=False)
    r = sh(["supervisorctl", "status", SERVICE], check=False, capture=True)
    log("Supervisor status: " + (r.stdout or "").strip())

    ok_count = ok_list.read_text(encoding="utf-8").count("\n") if ok_list.exists() else 0
    fail_count = fail_list.read_text(encoding="utf-8").count("\n") if fail_list.exists() else 0
    log(f"BATCH DONE: ok={ok_count} fail={fail_count}")
    log(f"MASTER_LOG={master_log}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
