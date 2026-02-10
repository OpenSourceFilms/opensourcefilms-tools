#!/usr/bin/env python3
"""
Run applicable contract checkers for media.

Supports two entry modes:
A) --mid <MEDIA_ID> [--media-path <file>]
B) --in <file> [--in <file> ...]

Principles:
- Pure enforcement. No inference from absence.
- Missing prerequisites produce explicit SKIPPED outcomes with enumerated reasons.
- Eligibility constraints (from explicit chunking_outcome) may make downstream artifacts non-required.
- Writes one audit JSON report (atomic) under <catalog_root>/audit/ per media_id.
- Does not modify canonical artifacts.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


REASON_OK = "OK"
REASON_MISSING_INPUT = "MISSING_REQUIRED_INPUT"
REASON_CHECKER_FAILED = "CHECKER_FAILED"
REASON_NOT_REQUESTED = "NOT_REQUESTED"
REASON_INELIGIBLE = "INELIGIBLE_FOR_EMBEDDING"


def utc_now_stamp() -> str:
    return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def atomic_write_json(path: Path, obj: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(str(tmp), str(path))


def run_cmd(cmd: List[str], timeout_s: int = 180) -> Dict:
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
        )
        dt_ms = int((time.time() - t0) * 1000)
        return {
            "exit_code": p.returncode,
            "duration_ms": dt_ms,
            "stdout": p.stdout,
            "stderr": p.stderr,
        }
    except subprocess.TimeoutExpired as e:
        dt_ms = int((time.time() - t0) * 1000)
        return {
            "exit_code": 124,
            "duration_ms": dt_ms,
            "stdout": e.stdout or "",
            "stderr": (e.stderr or "") + "\nTIMEOUT",
        }
    except Exception as e:
        dt_ms = int((time.time() - t0) * 1000)
        return {
            "exit_code": 2,
            "duration_ms": dt_ms,
            "stdout": "",
            "stderr": f"EXCEPTION: {repr(e)}",
        }


def first_existing(paths: List[Path]) -> Optional[Path]:
    for p in paths:
        if p.exists():
            return p
    return None


def build_paths(catalog_root: Path, mid: str) -> Dict[str, Path]:
    return {
        "asr_json": catalog_root / "asr" / f"{mid}.json",
        "chunks_jsonl": catalog_root / "chunks" / f"{mid}.jsonl",
        "embeddings_json": catalog_root / "embeddings" / f"{mid}.json",
        "events_jsonl": catalog_root / "events" / f"{mid}.jsonl",
        "entities_jsonl": catalog_root / "entities" / f"{mid}.jsonl",
        "entity_nodes_jsonl": catalog_root / "entity_nodes" / f"{mid}.jsonl",
        "entity_mentions_jsonl": catalog_root / "entity_mentions" / f"{mid}.jsonl",
    }


def check_mid_format(mid: str) -> None:
    if len(mid) != 64 or any(c not in "0123456789abcdef" for c in mid):
        raise ValueError("mid must be 64 lowercase hex chars")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_chunks_tail(chunks_path: Path) -> Tuple[Optional[Dict], Optional[str]]:
    try:
        if not chunks_path.exists():
            return None, "missing_chunks"
        lines = [ln.strip() for ln in chunks_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return None, "empty_chunks_file"
        tail = json.loads(lines[-1])
        if not isinstance(tail, dict):
            return None, "tail_not_object"
        return tail, None
    except Exception as e:
        return None, f"tail_read_error:{type(e).__name__}"


def is_ineligible_for_embedding(chunks_tail: Optional[Dict]) -> bool:
    """
    True only if explicit chunking_outcome eligibility says eligible_for_embedding=false.
    """
    if not isinstance(chunks_tail, dict):
        return False
    if chunks_tail.get("record_type") != "chunking_outcome":
        return False
    elig = chunks_tail.get("eligibility")
    if not isinstance(elig, dict):
        return False
    val = elig.get("eligible_for_embedding")
    if isinstance(val, bool):
        return val is False
    if val in (0, 1):
        return bool(val) is False
    return False


def run_for_mid(
    mid: str,
    catalog_root: Path,
    media_path: Optional[Path],
    timeout_s: int,
) -> int:
    try:
        check_mid_format(mid)
    except Exception as e:
        print(f"FATAL: {e}")
        return 2

    paths = build_paths(catalog_root, mid)
    run_id = f"tool_run_contracts_{utc_now_stamp()}"
    created_utc = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    results: List[Dict] = []

    def add_result(name: str, status: str, reasons: Dict[str, int], detail: Dict) -> None:
        results.append(
            {
                "checker": name,
                "status": status,
                "reasons": reasons,
                "detail": detail,
            }
        )

    python_exe = sys.executable
    bin_dir = Path(__file__).resolve().parent

    # Stage A contract check
    if media_path is not None:
        if not media_path.exists():
            add_result(
                "stageA_contract_check",
                "SKIPPED",
                {REASON_MISSING_INPUT: 1},
                {"required": {"media_path": str(media_path)}, "note": "media_path does not exist"},
            )
        else:
            cmd = [
                python_exe,
                str(bin_dir / "stageA_contract_check.py"),
                "--in",
                str(media_path),
                "--mid",
                mid,
                "--catalog-root",
                str(catalog_root),
            ]
            out = run_cmd(cmd, timeout_s=timeout_s)
            if out["exit_code"] == 0:
                add_result("stageA_contract_check", "OK", {REASON_OK: 1}, {"cmd": cmd, **out})
            else:
                add_result("stageA_contract_check", "FAILED", {REASON_CHECKER_FAILED: 1}, {"cmd": cmd, **out})
    else:
        add_result("stageA_contract_check", "SKIPPED", {REASON_NOT_REQUESTED: 1}, {"note": "no media path provided"})

    # Stage B0 contract check requires ASR json + chunks jsonl
    asr_candidates = [
        paths["asr_json"],
        catalog_root / "asr" / f"{mid}.asr.json",
        catalog_root / "asr" / f"{mid}.jsonl",
    ]
    asr_path = first_existing(asr_candidates)
    chunks_path = paths["chunks_jsonl"] if paths["chunks_jsonl"].exists() else None

    chunks_tail: Optional[Dict] = None
    ineligible = False

    if not asr_path or not chunks_path:
        missing = {}
        if not asr_path:
            missing["asr"] = [str(p) for p in asr_candidates]
        if not chunks_path:
            missing["chunks"] = [str(paths["chunks_jsonl"])]
        add_result("stageB0_contract_check", "SKIPPED", {REASON_MISSING_INPUT: 1}, {"required": missing})
    else:
        cmd = [
            python_exe,
            str(bin_dir / "stageB0_contract_check.py"),
            "--asr",
            str(asr_path),
            "--chunks",
            str(chunks_path),
        ]
        out = run_cmd(cmd, timeout_s=timeout_s)
        if out["exit_code"] == 0:
            add_result("stageB0_contract_check", "OK", {REASON_OK: 1}, {"cmd": cmd, **out})
        else:
            add_result("stageB0_contract_check", "FAILED", {REASON_CHECKER_FAILED: 1}, {"cmd": cmd, **out})

        chunks_tail, _err = read_chunks_tail(chunks_path)
        ineligible = is_ineligible_for_embedding(chunks_tail)

    # If ineligible, downstream artifacts are non-required for the purposes of the contract gate.
    if ineligible:
        add_result("stageB1_contract_check", "SKIPPED", {REASON_INELIGIBLE: 1}, {"note": "ineligible_for_embedding; downstream stages not required"})
        add_result("stageE0_contract_check", "SKIPPED", {REASON_INELIGIBLE: 1}, {"note": "ineligible_for_embedding; downstream stages not required"})
        add_result("stageF0_contract_check", "SKIPPED", {REASON_INELIGIBLE: 1}, {"note": "ineligible_for_embedding; downstream stages not required"})
        add_result("stageF1_contract_check", "SKIPPED", {REASON_INELIGIBLE: 1}, {"note": "ineligible_for_embedding; downstream stages not required"})
    else:
        # Stage B1
        emb_path = paths["embeddings_json"]
        if not emb_path.exists():
            add_result("stageB1_contract_check", "SKIPPED", {REASON_MISSING_INPUT: 1}, {"required": {"embeddings": str(emb_path)}})
        else:
            cmd = [python_exe, str(bin_dir / "stageB1_contract_check.py"), "--mid", mid]
            out = run_cmd(cmd, timeout_s=timeout_s)
            if out["exit_code"] == 0:
                add_result("stageB1_contract_check", "OK", {REASON_OK: 1}, {"cmd": cmd, **out})
            else:
                add_result("stageB1_contract_check", "FAILED", {REASON_CHECKER_FAILED: 1}, {"cmd": cmd, **out})

        # Stage E0
        events_path = paths["events_jsonl"]
        if not events_path.exists():
            add_result("stageE0_contract_check", "SKIPPED", {REASON_MISSING_INPUT: 1}, {"required": {"events": str(events_path)}})
        else:
            cmd = [python_exe, str(bin_dir / "stageE0_contract_check.py"), "--mid", mid]
            out = run_cmd(cmd, timeout_s=timeout_s)
            if out["exit_code"] == 0:
                add_result("stageE0_contract_check", "OK", {REASON_OK: 1}, {"cmd": cmd, **out})
            else:
                add_result("stageE0_contract_check", "FAILED", {REASON_CHECKER_FAILED: 1}, {"cmd": cmd, **out})

        # Stage F0
        entities_path = paths["entities_jsonl"]
        if not entities_path.exists():
            add_result("stageF0_contract_check", "SKIPPED", {REASON_MISSING_INPUT: 1}, {"required": {"entities": str(entities_path)}})
        else:
            cmd = [python_exe, str(bin_dir / "stageF0_contract_check.py"), "--path", str(entities_path)]
            out = run_cmd(cmd, timeout_s=timeout_s)
            if out["exit_code"] == 0:
                add_result("stageF0_contract_check", "OK", {REASON_OK: 1}, {"cmd": cmd, **out})
            else:
                add_result("stageF0_contract_check", "FAILED", {REASON_CHECKER_FAILED: 1}, {"cmd": cmd, **out})

        # Stage F1
        nodes_path = paths["entity_nodes_jsonl"]
        mentions_path = paths["entity_mentions_jsonl"]
        if not nodes_path.exists() or not mentions_path.exists():
            missing = {}
            if not nodes_path.exists():
                missing["nodes"] = str(nodes_path)
            if not mentions_path.exists():
                missing["mentions"] = str(mentions_path)
            add_result("stageF1_contract_check", "SKIPPED", {REASON_MISSING_INPUT: 1}, {"required": missing})
        else:
            cmd = [
                python_exe,
                str(bin_dir / "stageF1_contract_check.py"),
                "--nodes",
                str(nodes_path),
                "--mentions",
                str(mentions_path),
            ]
            out = run_cmd(cmd, timeout_s=timeout_s)
            if out["exit_code"] == 0:
                add_result("stageF1_contract_check", "OK", {REASON_OK: 1}, {"cmd": cmd, **out})
            else:
                add_result("stageF1_contract_check", "FAILED", {REASON_CHECKER_FAILED: 1}, {"cmd": cmd, **out})

    ok = all(r["status"] in ("OK", "SKIPPED") for r in results) and any(r["status"] == "OK" for r in results)
    fatal = any(r["status"] == "FAILED" for r in results)

    outcome = {
        "schema_version": 1,
        "record_type": "tool_contracts_outcome",
        "tool": "run_contracts",
        "run_id": run_id,
        "created_utc": created_utc,
        "media_id": mid,
        "media_path": str(media_path) if media_path is not None else "",
        "catalog_root": str(catalog_root),
        "python": sys.version,
        "python_executable": sys.executable,
        "ok": bool(ok) and not fatal,
        "results": results,
    }

    out_path = catalog_root / "audit" / f"{run_id}_{mid}.json"
    atomic_write_json(out_path, outcome)

    print(f"wrote: {out_path}")
    for r in results:
        print(f"{r['checker']}: {r['status']}")

    return 0 if outcome["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", default="", help="64-hex sha256 media_id")
    ap.add_argument(
        "--in",
        dest="inputs",
        action="append",
        default=[],
        help="Absolute input media path. Repeatable. Computes mid=sha256(file bytes) and runs Stage A contract check too.",
    )
    ap.add_argument("--media-path", default="", help="Absolute input media path for --mid mode (enables Stage A checker).")
    ap.add_argument("--catalog-root", default="/workspace/catalog", help="Catalog root (default /workspace/catalog)")
    ap.add_argument("--timeout-s", type=int, default=180, help="Per-checker timeout in seconds")
    args = ap.parse_args()

    catalog_root = Path(args.catalog_root).resolve()

    if args.inputs:
        rc = 0
        for inp in args.inputs:
            p = Path(inp).resolve()
            if not p.exists():
                print(f"FAIL: --in path does not exist: {p}", file=sys.stderr)
                rc = 2
                continue
            mid = sha256_file(p)
            r = run_for_mid(mid=mid, catalog_root=catalog_root, media_path=p, timeout_s=args.timeout_s)
            rc = max(rc, r)
        return rc

    if not args.mid.strip():
        print("FATAL: provide --mid or one or more --in paths", file=sys.stderr)
        return 2

    mid = args.mid.strip()
    media_path = Path(args.media_path).resolve() if args.media_path.strip() else None
    return run_for_mid(mid=mid, catalog_root=catalog_root, media_path=media_path, timeout_s=args.timeout_s)


if __name__ == "__main__":
    sys.exit(main())