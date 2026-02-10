#!/usr/bin/env python3
"""
run_batch.py

Deterministic batch runner for the Media Intelligence Pipeline.

Goals:
- Avoid interactive shell loop anomalies.
- Drive existing stage entrypoints with explicit input lists and coverage logs.
- Never treat status JSON or logs as authoritative.
- Write a run manifest under /workspace/catalog/runs/<RUN_ID>/ with per-item logs.
- Optional post-run: regenerate audit index and optionally run contract checks.

This runner is orchestration only. Stage scripts remain the source of artifact generation.

Stages:
- A  : stageA_one.sh (manifest + wav + srt/vtt + asr json + qc)
- B0 : stageB0_chunk.py (chunks jsonl)
- B1 : stageB1 embeddings (onnxruntime, e5-small-v2)
- B  : stageB_one.sh (keyframes + enrich)

Convenience presets for --stages:
- full      -> A,B0,B1,B
- semantic  -> A,B0,B1
- editorial -> A,B0,B1,B
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


CATALOG_ROOT_DEFAULT = "/workspace/catalog"
PIPELINE_BIN_DEFAULT = "/workspace/pipeline/bin"


def utc_ts() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fail(msg: str, code: int = 2) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(code)


def read_inputs(inputs_path: Path) -> List[str]:
    data = inputs_path.read_bytes().splitlines()
    paths: List[str] = []
    for line in data:
        if not line.strip():
            continue
        p = line.decode("utf-8", errors="strict").strip()
        if p:
            paths.append(p)
    return paths


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _render_env_subset(env: Dict[str, str]) -> Dict[str, str]:
    keys = ["RUN_ID", "IN", "MEDIA_ID", "KF_FPS", "CATALOG_ROOT"]
    out: Dict[str, str] = {}
    for k in keys:
        if k in env and env[k] is not None:
            out[k] = str(env[k])
    return out


def run_cmd(
    cmd: List[str],
    env: Dict[str, str],
    log_path: Path,
    timeout_s: Optional[int] = None,
) -> Dict[str, Any]:
    t0 = time.time()
    start_utc = utc_iso()

    log_path.parent.mkdir(parents=True, exist_ok=True)

    header = {
        "event": "stage_run_start",
        "start_utc": start_utc,
        "cwd": os.getcwd(),
        "cmd": cmd,
        "env_subset": _render_env_subset(env),
        "timeout_s": timeout_s,
        "host": os.uname().nodename if hasattr(os, "uname") else "",
    }

    with log_path.open("w", encoding="utf-8") as f:
        f.write("=== run_batch: stage log ===\n")
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        f.write("=== stage stdout/stderr ===\n")

    rc = 999
    out = ""
    err = None
    try:
        p = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        rc = p.returncode
        out = p.stdout or ""
    except subprocess.TimeoutExpired as e:
        rc = 124
        out = (e.stdout or "") if hasattr(e, "stdout") else ""
        err = {"type": "timeout", "detail": str(e)}
    except Exception as e:
        rc = 2
        out = out or ""
        err = {"type": "exception", "detail": repr(e)}

    elapsed_s = round(time.time() - t0, 3)
    end_utc = utc_iso()

    footer = {
        "event": "stage_run_end",
        "end_utc": end_utc,
        "rc": rc,
        "elapsed_s": elapsed_s,
    }
    if err:
        footer["error"] = err

    with log_path.open("a", encoding="utf-8") as f:
        if out:
            f.write(out)
            if not out.endswith("\n"):
                f.write("\n")
        f.write("=== run_batch: stage result ===\n")
        f.write(json.dumps(footer, ensure_ascii=False) + "\n")

    return {
        "cmd": cmd,
        "rc": rc,
        "elapsed_s": elapsed_s,
        "log_path": str(log_path),
    }


def extract_media_id_from_stageA_log(text: str) -> Optional[str]:
    for ln in text.splitlines():
        if ln.startswith("MEDIA_ID="):
            mid = ln.split("=", 1)[1].strip()
            if len(mid) == 64:
                return mid
        if "OK: MEDIA_ID=" in ln:
            mid = ln.split("OK: MEDIA_ID=", 1)[1].strip()
            if len(mid) == 64:
                return mid
    return None


def parse_stages(stages_raw: str) -> List[str]:
    s = (stages_raw or "").strip()
    if not s:
        return ["A"]

    if s.lower() in ("full", "editorial"):
        return ["A", "B0", "B1", "B"]
    if s.lower() == "semantic":
        return ["A", "B0", "B1"]

    parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", required=True, help="Path to UTF-8 text file, one absolute media path per line")
    ap.add_argument(
        "--stages",
        default="A",
        help="Comma-separated stages: A,B0,B1,B, or preset full|semantic|editorial. Default: A",
    )
    ap.add_argument("--run-id", default="", help="Override RUN_ID. Default: batch_<UTC>")
    ap.add_argument("--catalog-root", default=CATALOG_ROOT_DEFAULT)
    ap.add_argument("--pipeline-bin", default=PIPELINE_BIN_DEFAULT)
    ap.add_argument("--device", default="cpu", help="Device for embedding stage label (cpu only for ONNX in this runner).")
    ap.add_argument("--kf-fps", default="1", help="Keyframe FPS for Stage B. Default 1")
    ap.add_argument("--audit-after", action="store_true", help="Regenerate audit index after run")
    ap.add_argument("--audit-with-checks", action="store_true", help="If auditing after, run stageA and stageB1 contract checks")
    args = ap.parse_args()

    inputs_path = Path(args.inputs)
    if not inputs_path.exists():
        fail(f"inputs file not found: {inputs_path}")

    stages = parse_stages(args.stages)
    allowed = {"A", "B0", "B1", "B"}
    for st in stages:
        if st not in allowed:
            fail(f"invalid stage '{st}', allowed: {sorted(allowed)} or presets full|semantic|editorial")

    run_id = args.run_id.strip() or f"batch_{utc_ts()}"
    catalog_root = Path(args.catalog_root)
    pipeline_bin = Path(args.pipeline_bin)
    run_dir = catalog_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "run_id": run_id,
        "date_utc": utc_iso(),
        "inputs_file": str(inputs_path),
        "stages": stages,
        "stages_raw": args.stages,
        "pipeline_bin": str(pipeline_bin),
        "catalog_root": str(catalog_root),
        "device_label": args.device,
        "kf_fps": args.kf_fps,
        "embedding_backend": "onnxruntime",
        "embedding_model_id": "intfloat/e5-small-v2",
        "embedding_max_seq_length": 256,
        "embedding_normalize": True,
        "embedding_pooling": "mean_attention",
        "embedding_prefix_passage": "passage: ",
        "embedding_prefix_query": "query: ",
        "embedding_onnx_model_path": "/workspace/models/onnx/e5-small-v2/model.onnx",
        "embedding_tokenizer_path": "/workspace/models/onnx/e5-small-v2/tokenizer.json",
        "embedding_ort_intra_threads": 4,
        "embedding_ort_inter_threads": 1,
    }
    write_text(run_dir / "meta.json", json.dumps(meta, indent=2))

    paths = read_inputs(inputs_path)

    bad = [p for p in paths if not p.startswith("/")]
    if bad:
        write_text(run_dir / "bad_inputs.json", json.dumps(bad, indent=2))
        fail(f"inputs must be absolute paths; found {len(bad)} bad inputs (see {run_dir / 'bad_inputs.json'})")

    write_text(run_dir / "inputs.txt", "\n".join(paths) + "\n")

    results: List[Dict[str, Any]] = []
    mids: List[str] = []

    # Stage A
    if "A" in stages:
        for i, src in enumerate(paths, 1):
            item: Dict[str, Any] = {"i": i, "src": src, "stageA": None}
            if not Path(src).is_file():
                item["stageA"] = {"rc": 2, "error": "file_not_found"}
                results.append(item)
                continue

            env = os.environ.copy()
            env["IN"] = src
            env["RUN_ID"] = run_id

            log_path = run_dir / f"stageA_{i}.log"
            r = run_cmd([str(pipeline_bin / "stageA_one.sh")], env=env, log_path=log_path)
            item["stageA"] = r

            text = log_path.read_text(encoding="utf-8", errors="replace")
            mid = extract_media_id_from_stageA_log(text)
            item["media_id"] = mid
            if mid:
                mids.append(mid)

            results.append(item)

    if any(s in stages for s in ["B0", "B1", "B"]) and not mids:
        fail("requested B0/B1/B but no media_ids available. Run with stage A first, or extend runner to accept a mids list.")

    # Stage B0 (chunks)
    if "B0" in stages:
        for i, mid in enumerate(mids, 1):
            env = os.environ.copy()
            env["RUN_ID"] = run_id

            asr_json = catalog_root / "asr" / f"{mid}.json"
            out_jsonl = catalog_root / "chunks" / f"{mid}.jsonl"
            log_path = run_dir / f"stageB0_{i}_{mid[:12]}.log"

            if not asr_json.exists():
                results.append({"i": i, "media_id": mid, "stageB0": {"rc": 2, "error": f"missing_asr_json:{asr_json}"}})
                continue

            cmd = [
                sys.executable,
                str(pipeline_bin / "stageB0_chunk.py"),
                "--in",
                str(asr_json),
                "--out",
                str(out_jsonl),
            ]
            r = run_cmd(cmd, env=env, log_path=log_path)
            results.append({"i": i, "media_id": mid, "stageB0": r})

    # Stage B1 (embeddings) — canonical ONNX backend in cpu venv
    if "B1" in stages:
        embed_py = "/workspace/venvs/cpu/bin/python3"
        if not Path(embed_py).exists():
            fail(f"missing canonical cpu python: {embed_py}")

        for i, mid in enumerate(mids, 1):
            env = os.environ.copy()
            env["RUN_ID"] = run_id

            log_path = run_dir / f"stageB1_{i}_{mid[:12]}.log"
            cmd = [
                embed_py,
                str(pipeline_bin / "stageB1_embed_e5_onnx.py"),
                "--mid",
                mid,
                "--venv",
                "/workspace/venvs/cpu",
                "--batch_size",
                "32",
                "--max_seq_length",
                "256",
                "--normalize",
                "--model_id",
                "intfloat/e5-small-v2",
                "--onnx_model_path",
                "/workspace/models/onnx/e5-small-v2/model.onnx",
                "--tokenizer_path",
                "/workspace/models/onnx/e5-small-v2/tokenizer.json",
                "--ort_intra_threads",
                "4",
                "--ort_inter_threads",
                "1",
            ]
            r = run_cmd(cmd, env=env, log_path=log_path)
            results.append({"i": i, "media_id": mid, "stageB1": r})

    # Stage B (keyframes/enrich)
    if "B" in stages:
        for i, mid in enumerate(mids, 1):
            env = os.environ.copy()
            env["MEDIA_ID"] = mid
            env["RUN_ID"] = run_id
            env["KF_FPS"] = str(args.kf_fps)

            log_path = run_dir / f"stageB_{i}_{mid[:12]}.log"
            r = run_cmd([str(pipeline_bin / "stageB_one.sh")], env=env, log_path=log_path)
            results.append({"i": i, "media_id": mid, "stageB": r})

    write_text(run_dir / "results.jsonl", "\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n")

    if args.audit_after:
        audit_log = run_dir / "audit_index.log"
        audit_cmd = [sys.executable, str(pipeline_bin / "audit_index.py")]
        if args.audit_with_checks:
            audit_cmd += ["--run-stageA-check", "--run-stageB1-check"]
        r = run_cmd(audit_cmd, env=os.environ.copy(), log_path=audit_log)
        write_text(run_dir / "audit_result.json", json.dumps(r, indent=2))

    print(f"OK: run_id={run_id}")
    print(f"OK: run_dir={run_dir}")
    print(f"OK: results={run_dir / 'results.jsonl'}")
    if args.audit_after:
        print(f"OK: audit_log={run_dir / 'audit_index.log'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
