#!/usr/bin/env python3
"""
media_watch_daemon.py

Always-on media watcher + pipeline runner, with forensic-grade logging.

AUTHORITATIVE OPERATIONAL INVARIANT:
All operational state, logs, and audit records are written exclusively under:

  /workspace/pipeline/logs.all

No operational writes may occur outside this root.

This tool emits operational truth only. Canonical semantic truth lives under
/workspace/catalog and is never inferred by this daemon.

Forensic goals:
- Decision log is minimal and readable (no tool spam).
- Every processed file writes one structured audit JSON under logs.all/media_watch/runs/.
- Each stage invocation writes full combined stdout/stderr under logs.all/media_watch/stage_logs/<run_id>/,
  referenced by sha256 and byte length in the audit JSON.
- Audit JSON embeds input identity, produced artifact hashes, and contracts summary.

Principles:
- Polling only, no extra deps
- Deterministic media_id = sha256(file bytes)
- Explicit operational state under logs.all/media_watch, canonical artifacts under /workspace/catalog
- No inference from absence: missing artifacts are treated as "needs stage"
- Deprecation detection via explicit artifact_fingerprint (reprocess_policy.json)
- Eligibility is governed by chunking_outcome.eligibility (explicit)
- Identity linkage: downstream artifacts must reference current upstream artifact sha256s
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


MEDIA_EXTS_DEFAULT = [".mp4", ".mov", ".mkv", ".wav", ".mp3"]

# ============================================================================
# Canonical operational log root (NON-NEGOTIABLE)
# ============================================================================
LOGROOT = Path("/workspace/pipeline/logs.all").resolve()
SERVICE_ROOT = (LOGROOT / "media_watch").resolve()

DAEMON_LOG_DEFAULT = SERVICE_ROOT / "daemon.log"
STATE_PATH_DEFAULT = SERVICE_ROOT / "state.json"
RUNTIME_PATH_DEFAULT = SERVICE_ROOT / "runtime.json"

SCANS_DIR_DEFAULT = SERVICE_ROOT / "scans"
RUNS_DIR_DEFAULT = SERVICE_ROOT / "runs"
STAGE_LOGS_DIR_DEFAULT = SERVICE_ROOT / "stage_logs"

DEFAULT_CATALOG_ROOT = Path("/workspace/catalog").resolve()

# Enumerated reason codes (daemon/process-level)
R_OK = "OK"
R_FILE_UNSTABLE = "FILE_UNSTABLE"
R_FILE_CHANGED_DURING_HASH = "FILE_CHANGED_DURING_HASH"
R_NEW_FILE = "NEW_FILE"
R_ALREADY_COMPLIANT = "ALREADY_COMPLIANT"
R_INELIGIBLE_FOR_EMBEDDING = "INELIGIBLE_FOR_EMBEDDING"
R_MAX_PER_SCAN_BACKPRESSURE = "MAX_PER_SCAN_BACKPRESSURE"
R_STAGE_FAILED = "STAGE_FAILED"
R_CONTRACTS_FAILED = "CONTRACTS_FAILED"
R_POLICY_UNREADABLE = "POLICY_UNREADABLE"
R_POLICY_SCHEMA_MISMATCH = "POLICY_SCHEMA_MISMATCH"
R_CHUNKING_OUTCOME_MISSING = "CHUNKING_OUTCOME_MISSING"
R_UPSTREAM_ID_MISMATCH_CHUNKS_SHA = "UPSTREAM_IDENTITY_MISMATCH_CHUNKS_SHA"

# Operational note codes
R_STAGE_STDOUT_EMPTY = "STAGE_STDOUT_EMPTY"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def log_line(log_path: Path, msg: str) -> None:
    """
    Forensic-grade log append:
    - Always creates parent dir
    - Writes one line with UTC prefix
    - Flush + fsync so mtime advances and contents are durable
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{utc_now_iso()} {msg}\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def log_kv(log_path: Path, event: str, **fields: Any) -> None:
    """
    Consistent minimal event logger: EVENT k=v k=v ...
    Values are kept single-line.
    """
    parts: List[str] = [event]
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v).replace("\n", "\\n").replace("\r", "\\r")
        parts.append(f"{k}={s}")
    log_line(log_path, " ".join(parts))


def atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def load_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def ensure_cpu_venv(log_path: Path) -> None:
    py = sys.executable
    if not py.startswith("/workspace/venvs/cpu/"):
        log_kv(log_path, "FATAL", reason="not_in_cpu_venv", sys_executable=py)
        raise SystemExit(2)


def _assert_under_logroot(p: Path, name: str, daemon_log: Path) -> None:
    """
    Enforce NON-NEGOTIABLE invariant: operational writes must remain under LOGROOT.
    If user passes an override path outside LOGROOT, fail fast.
    """
    rp = p.resolve()
    try:
        rp.relative_to(LOGROOT)
    except Exception:
        log_kv(daemon_log, "FATAL", reason="path_outside_logroot", name=name, path=str(rp), logroot=str(LOGROOT))
        raise SystemExit(2)


def is_media_file(path: Path, exts: List[str]) -> bool:
    return path.is_file() and path.suffix.lower() in exts


def scan_paths(watch_dirs: List[Path], exts: List[str], max_depth: int) -> List[Path]:
    out: List[Path] = []
    for root in watch_dirs:
        if not root.exists():
            continue
        root = root.resolve()
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, str(root))
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > max_depth:
                dirnames[:] = []
                continue
            for fn in filenames:
                p = Path(dirpath) / fn
                if is_media_file(p, exts):
                    out.append(p)
    return out


def file_stat_key(p: Path) -> Tuple[int, int]:
    st = p.stat()
    return (int(st.st_size), int(st.st_mtime))


def file_is_stable(p: Path, last: Optional[Tuple[int, int]], settle_seconds: int) -> Tuple[bool, Tuple[int, int]]:
    cur = file_stat_key(p)
    now = int(time.time())
    if last is None:
        return False, cur
    if cur != last:
        return False, cur
    if now - cur[1] < settle_seconds:
        return False, cur
    return True, cur


def stable_mid_with_guard(
    p: Path,
    settle_seconds: int,
    last_seen: Optional[Tuple[int, int]],
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """
    For files we are about to process, strengthen the stability claim:
    - Require stable (size, mtime) across daemon tracking AND mtime older than settle_seconds.
    - Hash bytes for mid.
    - Re-stat after hashing; if changed, treat as unstable and do not process.
    """
    ok, cur = file_is_stable(p, last_seen, settle_seconds)
    details: Dict[str, Any] = {
        "last_seen": list(last_seen) if last_seen else None,
        "cur": list(cur),
        "settle_seconds": int(settle_seconds),
    }
    if not ok:
        return False, None, details

    st0 = p.stat()
    mid = sha256_file(p)
    st1 = p.stat()

    details.update(
        {
            "stat_before": {"size": int(st0.st_size), "mtime": int(st0.st_mtime)},
            "stat_after": {"size": int(st1.st_size), "mtime": int(st1.st_mtime)},
        }
    )

    if int(st0.st_size) != int(st1.st_size) or int(st0.st_mtime) != int(st1.st_mtime):
        return False, None, details

    # Also require mtime still older than settle_seconds at point of dispatch
    now = int(time.time())
    if now - int(st1.st_mtime) < int(settle_seconds):
        return False, None, details

    return True, mid, details


def load_policy(policy_path: Path, log_path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    d = load_json(policy_path)
    if not isinstance(d, dict):
        log_kv(log_path, "WARN", reason="policy_unreadable", path=str(policy_path), action="using_empty_policy")
        return {"schema_version": 1, "required_fingerprints": {}}, R_POLICY_UNREADABLE
    if d.get("schema_version") != 1:
        log_kv(
            log_path,
            "WARN",
            reason="policy_schema_mismatch",
            path=str(policy_path),
            action="using_empty_policy",
            got=str(d.get("schema_version")),
        )
        return {"schema_version": 1, "required_fingerprints": {}}, R_POLICY_SCHEMA_MISMATCH
    rf = d.get("required_fingerprints")
    if not isinstance(rf, dict):
        d["required_fingerprints"] = {}
    return d, None


def _read_json_tail(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        obj = json.loads(lines[-1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _read_json_head(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def read_chunking_outcome(catalog_root: Path, mid: str) -> Optional[Dict[str, Any]]:
    p = catalog_root / "chunks" / f"{mid}.jsonl"
    out = _read_json_tail(p)
    if not isinstance(out, dict):
        return None
    if out.get("record_type") != "chunking_outcome":
        return None
    return out


def chunking_eligible_for_embedding(catalog_root: Path, mid: str) -> Optional[bool]:
    out = read_chunking_outcome(catalog_root, mid)
    if not isinstance(out, dict):
        return None
    elig = out.get("eligibility")
    if not isinstance(elig, dict):
        return None
    val = elig.get("eligible_for_embedding")
    if isinstance(val, bool):
        return val
    if val in (0, 1):
        return bool(val)
    return None


def read_embeddings_fingerprint(catalog_root: Path, mid: str) -> Optional[Dict[str, Any]]:
    p = catalog_root / "embeddings" / f"{mid}.json"
    if not p.exists():
        return None
    d = load_json(p)
    if not isinstance(d, dict):
        return None
    fp = d.get("artifact_fingerprint")
    return fp if isinstance(fp, dict) else None


def read_embeddings_chunks_sha(catalog_root: Path, mid: str) -> Optional[str]:
    p = catalog_root / "embeddings" / f"{mid}.json"
    d = load_json(p)
    if not isinstance(d, dict):
        return None
    v = d.get("chunks_artifact_sha256")
    if isinstance(v, str) and len(v) == 64:
        return v
    return None


def current_chunks_sha(catalog_root: Path, mid: str) -> Optional[str]:
    p = catalog_root / "chunks" / f"{mid}.jsonl"
    if not p.exists():
        return None
    return sha256_file(p)


def embeddings_stale_vs_chunks(catalog_root: Path, mid: str) -> Tuple[bool, Dict[str, Any]]:
    cur = current_chunks_sha(catalog_root, mid)
    emb = read_embeddings_chunks_sha(catalog_root, mid)
    details = {
        "chunks_sha256_current": cur or "",
        "chunks_artifact_sha256_in_embeddings": emb or "",
        "mismatch": False,
        "unknown": False,
    }
    if not cur or not emb:
        details["unknown"] = True
        return False, details
    if cur != emb:
        details["mismatch"] = True
        return True, details
    return False, details


def _read_json_tail_fingerprint(path: Path) -> Optional[Dict[str, Any]]:
    out = _read_json_tail(path)
    if not isinstance(out, dict):
        return None
    fp = out.get("artifact_fingerprint")
    return fp if isinstance(fp, dict) else None


def read_events_outcome_fingerprint(catalog_root: Path, mid: str) -> Optional[Dict[str, Any]]:
    p = catalog_root / "events" / f"{mid}.jsonl"
    return _read_json_tail_fingerprint(p)


def read_entities_outcome_fingerprint(catalog_root: Path, mid: str) -> Optional[Dict[str, Any]]:
    p = catalog_root / "entities" / f"{mid}.jsonl"
    return _read_json_tail_fingerprint(p)


def fingerprint_matches(required: Dict[str, Any], got: Dict[str, Any]) -> Tuple[bool, str]:
    for k, v in required.items():
        if got.get(k) != v:
            return False, f"fingerprint_mismatch key={k} got={got.get(k)} want={v}"
    return True, "ok"


def decide_earliest_stage_to_rerun(policy: Dict[str, Any], catalog_root: Path, mid: str) -> Tuple[str, str, Dict[str, Any]]:
    """
    Returns (start_stage, reason_code, extra_details). start_stage may be NONE.
    extra_details is included in watcher audit when present.
    """
    extra: Dict[str, Any] = {}

    asr = catalog_root / "asr" / f"{mid}.json"
    chunks = catalog_root / "chunks" / f"{mid}.jsonl"

    if not asr.exists():
        return "A", "missing_asr", extra
    if not chunks.exists():
        return "B0", "missing_chunks", extra

    elig = chunking_eligible_for_embedding(catalog_root, mid)
    if elig is False:
        return "NONE", R_INELIGIBLE_FOR_EMBEDDING, extra

    emb = catalog_root / "embeddings" / f"{mid}.json"
    events = catalog_root / "events" / f"{mid}.jsonl"
    entities = catalog_root / "entities" / f"{mid}.jsonl"
    nodes = catalog_root / "entity_nodes" / f"{mid}.jsonl"
    mentions = catalog_root / "entity_mentions" / f"{mid}.jsonl"

    if emb.exists():
        stale, details = embeddings_stale_vs_chunks(catalog_root, mid)
        if stale:
            extra["identity_mismatch"] = {"type": "chunks_sha", **details}
            return "B1", R_UPSTREAM_ID_MISMATCH_CHUNKS_SHA, extra

    if not emb.exists():
        return "B1", "missing_embeddings", extra
    if not events.exists():
        return "E0", "missing_events", extra
    if not entities.exists():
        return "F0", "missing_entities", extra
    if not nodes.exists() or not mentions.exists():
        return "F1", "missing_f1_outputs", extra

    rf = policy.get("required_fingerprints") or {}
    if not isinstance(rf, dict):
        rf = {}

    if "B1" in rf and isinstance(rf["B1"], dict):
        got = read_embeddings_fingerprint(catalog_root, mid)
        if got is None:
            return "B1", "missing_or_no_fingerprint_embeddings", extra
        ok, why = fingerprint_matches(rf["B1"], got)
        if not ok:
            return "B1", why, extra

    if "E0" in rf and isinstance(rf["E0"], dict):
        got = read_events_outcome_fingerprint(catalog_root, mid)
        if got is None:
            return "E0", "missing_or_no_fingerprint_events", extra
        ok, why = fingerprint_matches(rf["E0"], got)
        if not ok:
            return "E0", why, extra

    if "F0" in rf and isinstance(rf["F0"], dict):
        got = read_entities_outcome_fingerprint(catalog_root, mid)
        if got is None:
            return "F0", "missing_or_no_fingerprint_entities", extra
        ok, why = fingerprint_matches(rf["F0"], got)
        if not ok:
            return "F0", why, extra

    return "NONE", R_ALREADY_COMPLIANT, extra


def _ensure_nonempty_stage_log(
    out_path: Path,
    stage: str,
    run_id: str,
    mid: str,
    cmd: List[str],
    rc: int,
    note: str,
) -> bytes:
    """
    Enforces the invariant: stage log files are never 0 bytes.
    This is operational audit, not canonical semantics.
    """
    line = {
        "record_type": "tool_stage_log_header",
        "schema_version": 1,
        "created_utc": utc_now_iso(),
        "run_id": run_id,
        "media_id": mid,
        "stage": stage,
        "exit_code": int(rc),
        "note": note,
        "cmd": cmd,
    }
    b = (json.dumps(line, sort_keys=True) + "\n").encode("utf-8")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b)
    return b


def run_cmd_capture(
    stage: str,
    run_id: str,
    mid: str,
    cmd: List[str],
    out_path: Path,
    env: Optional[Dict[str, str]] = None,
    timeout_s: int = 0,
) -> Dict[str, Any]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            env=env,
            timeout=(timeout_s if timeout_s and timeout_s > 0 else None),
        )
        b = p.stdout or b""
        if len(b) == 0:
            b = _ensure_nonempty_stage_log(out_path, stage, run_id, mid, cmd, p.returncode, R_STAGE_STDOUT_EMPTY)
        else:
            out_path.write_bytes(b)
        dt_ms = int((time.time() - t0) * 1000)
        return {
            "exit_code": p.returncode,
            "duration_ms": dt_ms,
            "log_path": str(out_path),
            "log_sha256": sha256_bytes(b),
            "log_bytes": int(len(b)),
        }
    except subprocess.TimeoutExpired as e:
        b = (e.stdout or b"") + b"\nTIMEOUT"
        if len(b) == 0:
            b = _ensure_nonempty_stage_log(out_path, stage, run_id, mid, cmd, 124, "TIMEOUT_NO_OUTPUT")
        else:
            out_path.write_bytes(b)
        dt_ms = int((time.time() - t0) * 1000)
        return {
            "exit_code": 124,
            "duration_ms": dt_ms,
            "log_path": str(out_path),
            "log_sha256": sha256_bytes(b),
            "log_bytes": int(len(b)),
        }
    except Exception as e:
        b = f"EXCEPTION: {type(e).__name__}: {e}\n".encode("utf-8")
        if len(b) == 0:
            b = _ensure_nonempty_stage_log(out_path, stage, run_id, mid, cmd, 2, "EXCEPTION_NO_OUTPUT")
        else:
            out_path.write_bytes(b)
        dt_ms = int((time.time() - t0) * 1000)
        return {
            "exit_code": 2,
            "duration_ms": dt_ms,
            "log_path": str(out_path),
            "log_sha256": sha256_bytes(b),
            "log_bytes": int(len(b)),
        }


def find_latest_contracts_audit(catalog_root: Path, mid: str) -> Optional[Path]:
    audit_dir = catalog_root / "audit"
    if not audit_dir.exists():
        return None
    cands = sorted(
        audit_dir.glob(f"tool_run_contracts_*_{mid}.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return cands[0] if cands else None


def load_contracts_summary(path: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": None, "run_id": "", "statuses": {}, "path": str(path)}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        out["ok"] = d.get("ok")
        out["run_id"] = d.get("run_id") or ""
        results = d.get("results") or []
        if isinstance(results, list):
            for r in results:
                if isinstance(r, dict):
                    chk = r.get("checker") or ""
                    st = r.get("status") or ""
                    if chk:
                        out["statuses"][chk] = st
    except Exception:
        pass
    return out


def artifact_entry(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "sha256": ""}
    return {"path": str(path), "exists": True, "sha256": sha256_file(path)}


def harvest_stage_outcomes(catalog_root: Path, mid: str) -> Dict[str, Any]:
    """
    Best-effort harvesting of explicit stage outcome records from canonical artifacts.
    This is used to make per-run manifests instantly auditable.
    """
    out: Dict[str, Any] = {}

    co = read_chunking_outcome(catalog_root, mid)
    if isinstance(co, dict):
        out["B0_chunking_outcome"] = co

    events_p = catalog_root / "events" / f"{mid}.jsonl"
    ev_tail = _read_json_tail(events_p)
    if isinstance(ev_tail, dict) and str(ev_tail.get("record_type", "")).endswith("outcome"):
        out["E0_events_outcome"] = ev_tail

    ent_p = catalog_root / "entities" / f"{mid}.jsonl"
    en_tail = _read_json_tail(ent_p)
    if isinstance(en_tail, dict) and str(en_tail.get("record_type", "")).endswith("outcome"):
        out["F0_entities_outcome"] = en_tail

    nodes_p = catalog_root / "entity_nodes" / f"{mid}.jsonl"
    head = _read_json_head(nodes_p)
    if isinstance(head, dict) and head.get("record_type") == "entity_link_outcome":
        out["F1_entity_link_outcome"] = head

    return out


def write_run_manifest(
    manifest_dir: Path,
    run_id: str,
    mid: str,
    audit_path: Path,
    audit_obj: Dict[str, Any],
    catalog_root: Path,
) -> Path:
    """
    Writes a single per-run manifest that is always non-empty and sufficient for quick audit.
    """
    manifest_dir.mkdir(parents=True, exist_ok=True)
    audit_sha = sha256_file(audit_path) if audit_path.exists() else ""

    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "record_type": "tool_media_watch_manifest",
        "created_utc": utc_now_iso(),
        "run_id": run_id,
        "media_id": mid,
        "audit_path": str(audit_path),
        "audit_sha256": audit_sha,
        "ok": bool(audit_obj.get("ok")),
        "decision": str(audit_obj.get("decision", "")),
        "decision_reason": str(audit_obj.get("decision_reason", "")),
        "start_stage": str(audit_obj.get("start_stage", "")),
        "python_executable": str(audit_obj.get("python_executable", "")),
        "input": audit_obj.get("input") or {},
        "artifacts": audit_obj.get("artifacts") or {},
        "contracts_summary": audit_obj.get("contracts_summary") or {},
        "contracts_audit_path": str(audit_obj.get("contracts_audit_path", "")),
        "contracts_audit_sha256": str(audit_obj.get("contracts_audit_sha256", "")),
        "stages": audit_obj.get("stages") or [],
        "stage_outcomes": harvest_stage_outcomes(catalog_root, mid),
    }

    manifest_path = manifest_dir / f"{run_id}_{mid}.manifest.json"
    atomic_write_json(manifest_path, manifest)
    return manifest_path


def runtime_state_init(watch_dirs: List[Path], exts: List[str], max_depth: int, policy_path: Path, catalog_root: Path) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "tool_media_watch_runtime",
        "created_utc": utc_now_iso(),
        "pid": os.getpid(),
        "heartbeat_utc": utc_now_iso(),
        "watch_dirs": [str(p) for p in watch_dirs],
        "exts": exts,
        "max_depth": int(max_depth),
        "policy_path": str(policy_path),
        "catalog_root": str(catalog_root),
        "last_scan_utc": "",
        "in_flight": None,
        "recent": [],
    }


def runtime_state_load_or_init(path: Path, watch_dirs: List[Path], exts: List[str], max_depth: int, policy_path: Path, catalog_root: Path) -> Dict[str, Any]:
    if path.exists():
        d = load_json(path)
        if isinstance(d, dict) and d.get("record_type") == "tool_media_watch_runtime":
            # Refresh always-truthy fields that can change
            d["pid"] = os.getpid()
            d["heartbeat_utc"] = utc_now_iso()
            d["catalog_root"] = str(catalog_root)
            return d
    return runtime_state_init(watch_dirs, exts, max_depth, policy_path, catalog_root)


def runtime_set_in_flight(rt: Dict[str, Any], run_id: str, mid: str, infile: Path, start_stage: str, reason: str) -> None:
    rt["in_flight"] = {
        "run_id": run_id,
        "media_id": mid,
        "input_path": str(infile),
        "start_stage": start_stage,
        "decision_reason": reason,
        "pid": os.getpid(),
        "started_utc": utc_now_iso(),
        "heartbeat_utc": utc_now_iso(),
    }


def runtime_touch_in_flight(rt: Dict[str, Any]) -> None:
    inf = rt.get("in_flight")
    if isinstance(inf, dict):
        inf["heartbeat_utc"] = utc_now_iso()
        rt["in_flight"] = inf


def runtime_clear_in_flight(rt: Dict[str, Any]) -> None:
    rt["in_flight"] = None


def runtime_add_recent(rt: Dict[str, Any], entry: Dict[str, Any], keep: int = 50) -> None:
    rec = rt.get("recent")
    if not isinstance(rec, list):
        rec = []
    rec.insert(0, entry)
    rt["recent"] = rec[:keep]


def run_pipeline_from_stage(
    infile: Path,
    mid: str,
    start_stage: str,
    daemon_log: Path,
    cpu_python: str,
    publish_stageC: bool,
    stageC_build_only: bool,
    run_id: str,
    stage_logs_dir: Path,
    catalog_root: Path,
    runtime_path: Path,
    rt: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    env = os.environ.copy()
    env["IN"] = str(infile)

    stages: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {}
    artifacts: Dict[str, Any] = {}

    def bump(code: str) -> None:
        reasons[code] = reasons.get(code, 0) + 1

    def snapshot_artifacts() -> None:
        artifacts["asr"] = artifact_entry(catalog_root / "asr" / f"{mid}.json")
        artifacts["chunks"] = artifact_entry(catalog_root / "chunks" / f"{mid}.jsonl")
        artifacts["embeddings"] = artifact_entry(catalog_root / "embeddings" / f"{mid}.json")
        artifacts["events"] = artifact_entry(catalog_root / "events" / f"{mid}.jsonl")
        artifacts["entities"] = artifact_entry(catalog_root / "entities" / f"{mid}.jsonl")
        artifacts["entity_nodes"] = artifact_entry(catalog_root / "entity_nodes" / f"{mid}.jsonl")
        artifacts["entity_mentions"] = artifact_entry(catalog_root / "entity_mentions" / f"{mid}.jsonl")

    def _run(stage_name: str, cmd: List[str], use_env: bool = False) -> bool:
        stage_dir = stage_logs_dir / run_id
        stage_dir.mkdir(parents=True, exist_ok=True)
        out_file = stage_dir / f"stage_{stage_name}.log"

        meta = run_cmd_capture(stage_name, run_id, mid, cmd, out_file, env=(env if use_env else None))
        stages.append({"stage": stage_name, "cmd": cmd, **meta})
        log_kv(
            daemon_log,
            "STAGE",
            run_id=run_id,
            mid=mid,
            stage=stage_name,
            rc=meta["exit_code"],
            dur_ms=meta["duration_ms"],
            log_sha256=meta["log_sha256"],
            log_bytes=meta["log_bytes"],
        )
        runtime_touch_in_flight(rt)
        atomic_write_json(runtime_path, rt)
        return meta["exit_code"] == 0

    log_kv(daemon_log, "PROCESS_START", run_id=run_id, mid=mid, in_path=str(infile), start_stage=start_stage)

    if start_stage == "A":
        if not _run("A", ["bash", "/workspace/pipeline/bin/stageA_one.sh"], use_env=True):
            bump(R_STAGE_FAILED)
            snapshot_artifacts()
            return False, {"decision": "FAIL", "reasons": reasons, "stages": stages, "artifacts": artifacts}

    snapshot_artifacts()

    if start_stage in ("A", "B0"):
        asr_json = catalog_root / "asr" / f"{mid}.json"
        chunks_out = catalog_root / "chunks" / f"{mid}.jsonl"
        if not _run("B0", [cpu_python, "/workspace/pipeline/bin/stageB0_chunk.py", "--in", str(asr_json), "--out", str(chunks_out)]):
            bump(R_STAGE_FAILED)
            snapshot_artifacts()
            return False, {"decision": "FAIL", "reasons": reasons, "stages": stages, "artifacts": artifacts}

        snapshot_artifacts()

        elig = chunking_eligible_for_embedding(catalog_root, mid)
        chunk_out = read_chunking_outcome(catalog_root, mid)

        if elig is False:
            bump(R_INELIGIBLE_FOR_EMBEDDING)
            log_kv(daemon_log, "DECISION", run_id=run_id, mid=mid, decision="PROCESS_MINIMAL", reason=R_INELIGIBLE_FOR_EMBEDDING)

            if not _run("CONTRACTS", [cpu_python, "/workspace/pipeline/bin/run_contracts.py", "--in", str(infile)]):
                bump(R_CONTRACTS_FAILED)
                snapshot_artifacts()
                contracts_audit = find_latest_contracts_audit(catalog_root, mid)
                return False, {
                    "decision": "FAIL",
                    "reasons": reasons,
                    "stages": stages,
                    "artifacts": artifacts,
                    "eligibility_snapshot": (chunk_out or {}).get("eligibility"),
                    "chunking_outcome": chunk_out,
                    "contracts_audit_path": str(contracts_audit or ""),
                }

            contracts_audit = find_latest_contracts_audit(catalog_root, mid)
            contracts_sha = sha256_file(contracts_audit) if contracts_audit and contracts_audit.exists() else ""
            contracts_summary = (
                load_contracts_summary(contracts_audit)
                if contracts_audit and contracts_audit.exists()
                else {"ok": None, "run_id": "", "statuses": {}, "path": ""}
            )

            bump(R_OK)
            return True, {
                "decision": "PROCESS_MINIMAL",
                "reasons": reasons,
                "stages": stages,
                "artifacts": artifacts,
                "eligibility_snapshot": (chunk_out or {}).get("eligibility"),
                "chunking_outcome": chunk_out,
                "contracts_audit_path": str(contracts_audit or ""),
                "contracts_audit_sha256": contracts_sha,
                "contracts_summary": contracts_summary,
            }

        if elig is None:
            bump(R_CHUNKING_OUTCOME_MISSING)
            log_kv(daemon_log, "WARN", run_id=run_id, mid=mid, reason="cannot_read_chunking_eligibility", action="continuing_full_pipeline")

    if start_stage in ("A", "B0", "B1"):
        if not _run("B1", [cpu_python, "/workspace/pipeline/bin/stageB1_embed_e5_onnx.py", "--mid", mid, "--venv", "/workspace/venvs/cpu", "--normalize"]):
            bump(R_STAGE_FAILED)
            snapshot_artifacts()
            return False, {"decision": "FAIL", "reasons": reasons, "stages": stages, "artifacts": artifacts}

    snapshot_artifacts()

    if start_stage in ("A", "B0", "B1", "E0"):
        if not _run("E0", [cpu_python, "/workspace/pipeline/bin/stageE0_events.py", "--mid", mid]):
            bump(R_STAGE_FAILED)
            snapshot_artifacts()
            return False, {"decision": "FAIL", "reasons": reasons, "stages": stages, "artifacts": artifacts}

    snapshot_artifacts()

    if start_stage in ("A", "B0", "B1", "E0", "F0"):
        if not _run("F0", [cpu_python, "/workspace/pipeline/bin/stageF0_entities.py", "--media_id", mid, "--prefer_events"]):
            bump(R_STAGE_FAILED)
            snapshot_artifacts()
            return False, {"decision": "FAIL", "reasons": reasons, "stages": stages, "artifacts": artifacts}

    snapshot_artifacts()

    if start_stage in ("A", "B0", "B1", "E0", "F0", "F1"):
        if not _run("F1", [cpu_python, "/workspace/pipeline/bin/stageF1_entity_link.py", "--media_id", mid, "--canonical_out"]):
            bump(R_STAGE_FAILED)
            snapshot_artifacts()
            return False, {"decision": "FAIL", "reasons": reasons, "stages": stages, "artifacts": artifacts}

    snapshot_artifacts()

    if not _run("CONTRACTS", [cpu_python, "/workspace/pipeline/bin/run_contracts.py", "--in", str(infile)]):
        bump(R_CONTRACTS_FAILED)
        snapshot_artifacts()
        contracts_audit = find_latest_contracts_audit(catalog_root, mid)
        return False, {"decision": "FAIL", "reasons": reasons, "stages": stages, "artifacts": artifacts, "contracts_audit_path": str(contracts_audit or "")}

    snapshot_artifacts()

    if publish_stageC:
        cmd = [cpu_python, "/workspace/pipeline/bin/stageC_milvus_materialize.py", "--mids", mid]
        if stageC_build_only:
            cmd.insert(2, "--build_only")
        ok_c = _run("C", cmd)
        if not ok_c:
            log_kv(daemon_log, "WARN", run_id=run_id, mid=mid, reason="stageC_failed", action="best_effort")

    contracts_audit = find_latest_contracts_audit(catalog_root, mid)
    contracts_sha = sha256_file(contracts_audit) if contracts_audit and contracts_audit.exists() else ""
    contracts_summary = (
        load_contracts_summary(contracts_audit)
        if contracts_audit and contracts_audit.exists()
        else {"ok": None, "run_id": "", "statuses": {}, "path": ""}
    )

    bump(R_OK)
    return True, {
        "decision": "PROCESS_FULL",
        "reasons": reasons,
        "stages": stages,
        "artifacts": artifacts,
        "contracts_audit_path": str(contracts_audit or ""),
        "contracts_audit_sha256": contracts_sha,
        "contracts_summary": contracts_summary,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="append", default=[], help="Directory to watch. Repeatable.")
    ap.add_argument("--exts", default=",".join(MEDIA_EXTS_DEFAULT), help="Comma-separated extensions to include")
    ap.add_argument("--max-depth", type=int, default=6, help="Max directory walk depth")
    ap.add_argument("--scan-interval-s", type=int, default=30, help="Seconds between scans")
    ap.add_argument("--settle-seconds", type=int, default=90, help="Require file mtime older than this and stable across checks")
    ap.add_argument("--state", default=str(STATE_PATH_DEFAULT), help="State JSON path (config + per-file history)")
    ap.add_argument("--runtime", default=str(RUNTIME_PATH_DEFAULT), help="Runtime JSON path (in_flight + recent)")
    ap.add_argument("--runs-dir", default=str(RUNS_DIR_DEFAULT), help="Per-run directory (audit + manifest)")
    ap.add_argument("--log", default=str(DAEMON_LOG_DEFAULT), help="Daemon decision log path")
    ap.add_argument("--stage-logs-dir", default=str(STAGE_LOGS_DIR_DEFAULT), help="Stage logs root dir")
    ap.add_argument("--scans-dir", default=str(SCANS_DIR_DEFAULT), help="Per-scan records dir")
    ap.add_argument("--max-per-scan", type=int, default=2, help="Max files to process per scan")
    ap.add_argument("--policy", default="/workspace/pipeline/config/reprocess_policy.json", help="Policy JSON path")
    ap.add_argument("--catalog-root", default=str(DEFAULT_CATALOG_ROOT), help="Canonical catalog root (default /workspace/catalog)")
    ap.add_argument("--publish-stageC", action="store_true", help="After processing a file, run Stage C for that MID")
    ap.add_argument("--stageC-build-only", action="store_true", help="If publishing Stage C, do build_only (no swap)")
    args = ap.parse_args()

    SERVICE_ROOT.mkdir(parents=True, exist_ok=True)

    daemon_log = Path(args.log).resolve()
    state_path = Path(args.state).resolve()
    runtime_path = Path(args.runtime).resolve()
    runs_dir = Path(args.runs_dir).resolve()
    stage_logs_dir = Path(args.stage_logs_dir).resolve()
    scans_dir = Path(args.scans_dir).resolve()
    policy_path = Path(args.policy).resolve()
    catalog_root = Path(args.catalog_root).resolve()

    # Enforce operational write surface invariant
    _assert_under_logroot(daemon_log, "daemon_log", daemon_log)
    _assert_under_logroot(state_path, "state_path", daemon_log)
    _assert_under_logroot(runtime_path, "runtime_path", daemon_log)
    _assert_under_logroot(runs_dir, "runs_dir", daemon_log)
    _assert_under_logroot(stage_logs_dir, "stage_logs_dir", daemon_log)
    _assert_under_logroot(scans_dir, "scans_dir", daemon_log)

    ensure_cpu_venv(daemon_log)

    watch_args = args.watch or ["/workspace/BTS"]
    watch_dirs_raw = [Path(x).resolve() for x in watch_args]
    watch_dirs: List[Path] = []
    seen = set()
    for p in watch_dirs_raw:
        sp = str(p)
        if sp in seen:
            continue
        seen.add(sp)
        watch_dirs.append(p)

    exts = [e.strip().lower() for e in args.exts.split(",") if e.strip()]
    max_depth = int(args.max_depth)
    cpu_python = sys.executable

    policy, policy_err = load_policy(policy_path, daemon_log)

    # Existing state file (config + per-file history)
    state: Dict[str, Any] = {
        "schema_version": 1,
        "created_utc": utc_now_iso(),
        "watch_dirs": [str(p) for p in watch_dirs],
        "exts": exts,
        "max_depth": max_depth,
        "policy_path": str(policy_path),
        "catalog_root": str(catalog_root),
        "files": {},
    }

    if state_path.exists():
        d = load_json(state_path)
        if isinstance(d, dict):
            state = d
            state["catalog_root"] = str(catalog_root)
            if "files" not in state or not isinstance(state.get("files"), dict):
                state["files"] = {}
    if "files" not in state or not isinstance(state.get("files"), dict):
        state["files"] = {}

    runs_dir.mkdir(parents=True, exist_ok=True)
    stage_logs_dir.mkdir(parents=True, exist_ok=True)
    scans_dir.mkdir(parents=True, exist_ok=True)

    # Runtime state (in-flight + recent outcomes), always written even on idle scans
    rt = runtime_state_load_or_init(runtime_path, watch_dirs, exts, max_depth, policy_path, catalog_root)

    log_kv(
        daemon_log,
        "DAEMON_START",
        watch=",".join(str(p) for p in watch_dirs),
        exts=",".join(exts),
        policy=str(policy_path),
        policy_err=(policy_err or ""),
        scan_interval_s=args.scan_interval_s,
        settle_seconds=args.settle_seconds,
        max_per_scan=args.max_per_scan,
        max_depth=max_depth,
        state=str(state_path),
        runtime=str(runtime_path),
        runs_dir=str(runs_dir),
        stage_logs_dir=str(stage_logs_dir),
        scans_dir=str(scans_dir),
        python_executable=sys.executable,
        cwd=str(Path.cwd()),
        logroot=str(LOGROOT),
        catalog_root=str(catalog_root),
    )

    def write_scan_record(
        scan_id: str,
        n_paths: int,
        n_stable: int,
        n_processed: int,
        dur_ms: int,
        note: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        obj: Dict[str, Any] = {
            "schema_version": 1,
            "record_type": "tool_media_watch_scan",
            "scan_id": scan_id,
            "created_utc": utc_now_iso(),
            "pid": os.getpid(),
            "watch_dirs": [str(p) for p in watch_dirs],
            "n_paths": int(n_paths),
            "n_stable": int(n_stable),
            "n_processed": int(n_processed),
            "max_per_scan": int(args.max_per_scan),
            "scan_interval_s": int(args.scan_interval_s),
            "settle_seconds": int(args.settle_seconds),
            "duration_ms": int(dur_ms),
            "note": note,
            "catalog_root": str(catalog_root),
        }
        if isinstance(extra, dict) and extra:
            obj["extra"] = extra
        p = scans_dir / f"{scan_id}.json"
        atomic_write_json(p, obj)

    run_counter = 0

    while True:
        scan_id = f"scan_{utc_now_stamp()}"
        t_scan0 = time.time()
        n_paths = 0
        stable_count = 0
        processed_this_scan = 0
        skipped_unstable = 0
        skipped_compliant = 0
        skipped_backpressure = 0
        skipped_ineligible = 0
        warned_chunk_outcome_missing = 0

        try:
            log_kv(daemon_log, "SCAN_START", scan_id=scan_id)

            rt["heartbeat_utc"] = utc_now_iso()
            rt["last_scan_utc"] = utc_now_iso()
            runtime_touch_in_flight(rt)
            atomic_write_json(runtime_path, rt)

            paths = scan_paths(watch_dirs, exts, max_depth)
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            n_paths = len(paths)

            for p in paths:
                sp = str(p)
                rec = (state.get("files") or {}).get(sp) or {}

                last = None
                if "last_size" in rec and "last_mtime" in rec:
                    last = (int(rec["last_size"]), int(rec["last_mtime"]))

                # Maintain last-seen tracking even if unstable
                stable, cur = file_is_stable(p, last, int(args.settle_seconds))
                rec["last_size"] = cur[0]
                rec["last_mtime"] = cur[1]
                rec.setdefault("seen_utc", utc_now_iso())

                if not stable:
                    rec["status"] = rec.get("status") or "seen"
                    rec["decision"] = "SKIP"
                    rec["decision_reason"] = R_FILE_UNSTABLE
                    state["files"][sp] = rec
                    skipped_unstable += 1
                    log_kv(daemon_log, "DECISION", scan_id=scan_id, decision="SKIP", reason=R_FILE_UNSTABLE, path=sp)
                    continue

                stable_count += 1
                if "stable_utc" not in rec:
                    rec["stable_utc"] = utc_now_iso()

                # For dispatch candidates, strengthen stability and compute mid with guard
                ok_mid, mid, mid_details = stable_mid_with_guard(p, int(args.settle_seconds), last)
                if not ok_mid or not mid:
                    rec["decision"] = "SKIP"
                    rec["decision_reason"] = R_FILE_CHANGED_DURING_HASH
                    rec["mid"] = rec.get("mid") or ""
                    rec["mid_guard"] = mid_details
                    state["files"][sp] = rec
                    skipped_unstable += 1
                    log_kv(daemon_log, "DECISION", scan_id=scan_id, decision="SKIP", reason=R_FILE_CHANGED_DURING_HASH, path=sp)
                    continue

                rec["mid"] = mid
                rec["mid_guard"] = mid_details

                start_stage = "A"
                reason = R_NEW_FILE
                extra: Dict[str, Any] = {}

                if rec.get("status") == "ok":
                    start_stage, reason, extra = decide_earliest_stage_to_rerun(policy, catalog_root, mid)
                    if start_stage == "NONE":
                        rec["decision"] = "SKIP"
                        rec["decision_reason"] = reason
                        state["files"][sp] = rec
                        skipped_compliant += 1
                        log_kv(daemon_log, "DECISION", scan_id=scan_id, decision="SKIP", reason=reason, mid=mid, path=sp)

                        runtime_add_recent(
                            rt,
                            {"created_utc": utc_now_iso(), "kind": "skip", "media_id": mid, "input_path": sp, "reason": reason},
                        )
                        atomic_write_json(runtime_path, rt)
                        continue

                    if reason == R_INELIGIBLE_FOR_EMBEDDING:
                        skipped_ineligible += 1

                if processed_this_scan >= int(args.max_per_scan):
                    rec["decision"] = "SKIP"
                    rec["decision_reason"] = R_MAX_PER_SCAN_BACKPRESSURE
                    state["files"][sp] = rec
                    skipped_backpressure += 1
                    log_kv(daemon_log, "DECISION", scan_id=scan_id, decision="SKIP", reason=R_MAX_PER_SCAN_BACKPRESSURE, mid=mid, path=sp)

                    runtime_add_recent(
                        rt,
                        {"created_utc": utc_now_iso(), "kind": "skip", "media_id": mid, "input_path": sp, "reason": R_MAX_PER_SCAN_BACKPRESSURE},
                    )
                    atomic_write_json(runtime_path, rt)
                    continue

                run_counter += 1
                run_id = f"media_watch_{utc_now_stamp()}_{os.getpid()}_{run_counter}"
                rec["run_id"] = run_id
                rec["decision"] = "PROCESS"
                rec["decision_reason"] = reason
                rec["start_stage"] = start_stage
                rec["processing_utc"] = utc_now_iso()

                st = p.stat()
                input_identity = {
                    "input_path": str(p),
                    "input_bytes_sha256": mid,
                    "input_size_bytes": int(st.st_size),
                    "input_mtime_epoch": int(st.st_mtime),
                    "settle_seconds": int(args.settle_seconds),
                    "scan_interval_s": int(args.scan_interval_s),
                    "max_per_scan": int(args.max_per_scan),
                    "max_depth": int(args.max_depth),
                    "mid_guard": mid_details,
                }

                log_kv(
                    daemon_log,
                    "PROCESS_DISPATCH",
                    scan_id=scan_id,
                    run_id=run_id,
                    mid=mid,
                    in_path=str(p),
                    start_stage=start_stage,
                    reason=reason,
                )

                runtime_set_in_flight(rt, run_id, mid, p, start_stage, reason)
                atomic_write_json(runtime_path, rt)

                ok, payload = run_pipeline_from_stage(
                    infile=p,
                    mid=mid,
                    start_stage=start_stage,
                    daemon_log=daemon_log,
                    cpu_python=cpu_python,
                    publish_stageC=bool(args.publish_stageC),
                    stageC_build_only=bool(args.stageC_build_only),
                    run_id=run_id,
                    stage_logs_dir=stage_logs_dir,
                    catalog_root=catalog_root,
                    runtime_path=runtime_path,
                    rt=rt,
                )

                # Count chunking eligibility warnings as a scan metric (truthful)
                if isinstance(payload, dict):
                    rs = payload.get("reasons")
                    if isinstance(rs, dict) and rs.get(R_CHUNKING_OUTCOME_MISSING):
                        warned_chunk_outcome_missing += int(rs.get(R_CHUNKING_OUTCOME_MISSING) or 0)

                rec["processed_utc"] = utc_now_iso()
                rec["status"] = "ok" if ok else "failed"
                state["files"][sp] = rec
                processed_this_scan += 1

                audit_obj: Dict[str, Any] = {
                    "schema_version": 1,
                    "record_type": "tool_media_watch_outcome",
                    "run_id": run_id,
                    "created_utc": utc_now_iso(),
                    "ok": bool(ok),
                    "media_id": mid,
                    "policy_path": str(policy_path),
                    "policy_sha256": sha256_file(policy_path) if policy_path.exists() else "",
                    "decision_reason": reason,
                    "start_stage": start_stage,
                    "publish_stageC": bool(args.publish_stageC),
                    "stageC_build_only": bool(args.stageC_build_only),
                    "python_executable": sys.executable,
                    "input": input_identity,
                    "logroot": str(LOGROOT),
                    "catalog_root": str(catalog_root),
                }
                if extra:
                    audit_obj["identity_mismatches"] = extra
                if isinstance(payload, dict):
                    audit_obj.update(payload)

                audit_path = runs_dir / f"{run_id}_{mid}.outcome.json"
                atomic_write_json(audit_path, audit_obj)
                log_kv(daemon_log, "PROCESS_OUTCOME", scan_id=scan_id, run_id=run_id, mid=mid, ok=bool(ok), audit=str(audit_path))

                manifest_path = write_run_manifest(runs_dir, run_id, mid, audit_path, audit_obj, catalog_root=catalog_root)
                log_kv(daemon_log, "RUN_MANIFEST", scan_id=scan_id, run_id=run_id, mid=mid, manifest=str(manifest_path))

                runtime_add_recent(
                    rt,
                    {
                        "created_utc": utc_now_iso(),
                        "kind": "process",
                        "ok": bool(ok),
                        "run_id": run_id,
                        "media_id": mid,
                        "input_path": str(p),
                        "audit_path": str(audit_path),
                        "manifest_path": str(manifest_path),
                        "decision_reason": reason,
                        "start_stage": start_stage,
                    },
                )
                runtime_clear_in_flight(rt)

                atomic_write_json(runtime_path, rt)
                atomic_write_json(state_path, state)

            dur_ms = int((time.time() - t_scan0) * 1000)

            write_scan_record(
                scan_id,
                n_paths=n_paths,
                n_stable=stable_count,
                n_processed=processed_this_scan,
                dur_ms=dur_ms,
                note="ok",
            )

            atomic_write_json(state_path, state)
            rt["heartbeat_utc"] = utc_now_iso()
            atomic_write_json(runtime_path, rt)

            log_kv(
                daemon_log,
                "SCAN_SUMMARY",
                scan_id=scan_id,
                n_paths=n_paths,
                n_stable=stable_count,
                n_processed=processed_this_scan,
                skip_unstable=skipped_unstable,
                skip_compliant=skipped_compliant,
                skip_backpressure=skipped_backpressure,
                skip_ineligible=skipped_ineligible,
                warn_chunk_outcome_missing=warned_chunk_outcome_missing,
                dur_ms=dur_ms,
            )
            log_kv(daemon_log, "SCAN_END", scan_id=scan_id, dur_ms=dur_ms)

            time.sleep(int(args.scan_interval_s))

        except KeyboardInterrupt:
            log_kv(daemon_log, "DAEMON_STOP", reason="keyboard_interrupt")
            runtime_clear_in_flight(rt)
            rt["heartbeat_utc"] = utc_now_iso()
            atomic_write_json(runtime_path, rt)
            atomic_write_json(state_path, state)
            return 0
        except Exception as e:
            dur_ms = int((time.time() - t_scan0) * 1000)
            tb = traceback.format_exc().replace("\n", "\\n")
            log_kv(
                daemon_log,
                "WARN",
                scan_id=scan_id,
                reason="daemon_loop_exception",
                exc_type=type(e).__name__,
                exc=str(e),
                traceback=tb,
            )
            runtime_clear_in_flight(rt)
            rt["heartbeat_utc"] = utc_now_iso()
            atomic_write_json(runtime_path, rt)
            atomic_write_json(state_path, state)
            try:
                write_scan_record(scan_id, n_paths=0, n_stable=0, n_processed=0, dur_ms=dur_ms, note="exception")
            except Exception:
                pass
            time.sleep(int(args.scan_interval_s))


if __name__ == "__main__":
    raise SystemExit(main())