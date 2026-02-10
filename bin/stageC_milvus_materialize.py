#!/usr/bin/env python3
"""
Stage C: Milvus Lite cache materialisation (offline only)

Builds a fresh Milvus Lite DB from:
- Stage B0 chunks JSONL
- Stage B1 embeddings JSON
- Stage E0 events JSONL (optional; derived from chunk vectors, no new embedding)
- Stage F1 entity nodes + mentions JSONL (optional; derived from chunk vectors, no new embedding)

This is the ONLY supported writer for moments.db.

DEFAULT BEHAVIOUR:
- Builds a new DB: <catalog>/milvus/m_build_<UTC>_aNN.db
- Validates it
- Publishes by swapping into moments.db (unless --build_only)

Publish policy (rebuild-on-retry):
- If publish is blocked (moments.db in use), Stage C waits and rebuilds a fresh
  candidate DB from current artifacts on each retry attempt.

Key invariants:
- ASR collection joins strictly on (media_id, chunk_id)
- Stores text as "text" prefixed with "passage: "
- Stores vector as float list len==384
- Enforces B1 eligibility + B0 policy.allow_indexing
- Writes an explicit build manifest with reason codes
- Never mutates moments.db in place

Important constraints (observed with milvus_lite 2.5.1 + pymilvus 2.6.x):
- MilvusClient local file URIs must end with ".db"
- The DB filename (basename) must be < 36 characters
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


CATALOG_DEFAULT = "/workspace/catalog"

ASR_COLLECTION = "moments_asr_v1"
EVENTS_COLLECTION = "moments_events_v1"
ENTITY_NODES_COLLECTION = "moments_entity_nodes_v1"

INDEX_NAME = "vector_index"
VECTOR_DIM = 384

DEFAULT_LOCK_DIR = "/workspace/pipeline/runtime_locks"
DEFAULT_LOCK_NAME = "stageC.lock"

LOGS_ALL_DEFAULT = "/logs.all"
LOGS_ALL_ENV = "PIPELINE_LOGS_ALL"


# Stage C reason codes (stable, copy into masters later)
REASON = {
    "STAGEC_LOCK_HELD": "STAGEC_LOCK_HELD",
    "STAGEC_LOCK_STALE": "STAGEC_LOCK_STALE",
    "OUT_DB_INVALID": "OUT_DB_INVALID",
    "EMBEDDINGS_DIR_EMPTY": "EMBEDDINGS_DIR_EMPTY",
    "FINAL_DB_IN_USE": "FINAL_DB_IN_USE",
    "FINAL_DB_IN_USE_UNKNOWN": "FINAL_DB_IN_USE_UNKNOWN",
    "PUBLISH_FORCED_WITHOUT_VERIFICATION": "PUBLISH_FORCED_WITHOUT_VERIFICATION",
    "B0_MISSING_CHUNKS_FILE": "B0_MISSING_CHUNKS_FILE",
    "B1_MISSING_EMBEDDINGS_FILE": "B1_MISSING_EMBEDDINGS_FILE",
    "B1_VECTOR_BAD_TYPE": "B1_VECTOR_BAD_TYPE",
    "B1_RECORD_INELIGIBLE": "B1_RECORD_INELIGIBLE",
    "B0_CHUNK_NOT_FOUND": "B0_CHUNK_NOT_FOUND",
    "B0_POLICY_NO_INDEX": "B0_POLICY_NO_INDEX",
    "B1_TEXT_BAD_PREFIX": "B1_TEXT_BAD_PREFIX",
    "B1_VECTOR_BAD_DIM": "B1_VECTOR_BAD_DIM",
    "B0_B1_TIME_MISMATCH": "B0_B1_TIME_MISMATCH",
    "INSERT_ERROR": "INSERT_ERROR",
    "FLUSH_FAILED": "FLUSH_FAILED",
    "LOAD_FAILED": "LOAD_FAILED",
    "STATS_FAILED": "STATS_FAILED",
    "VALIDATION_FAILED": "VALIDATION_FAILED",
    "MILVUS_TOOL_ERROR": "MILVUS_TOOL_ERROR",

    # Events specific (Stage E0 derived inputs)
    "E0_EVENTS_JSON_BAD": "E0_EVENTS_JSON_BAD",
    "E0_EVENTS_UNKNOWN_RECORD": "E0_EVENTS_UNKNOWN_RECORD",
    "E0_EVENTS_OUTCOME_MISSING": "E0_EVENTS_OUTCOME_MISSING",
    "E0_EVENT_MISSING_FIELDS": "E0_EVENT_MISSING_FIELDS",
    "E0_EVENT_MISSING_CHUNK_IDS": "E0_EVENT_MISSING_CHUNK_IDS",
    "E0_EVENT_NO_CHUNK_VECTORS": "E0_EVENT_NO_CHUNK_VECTORS",
    "E0_EVENT_TEXT_MISSING": "E0_EVENT_TEXT_MISSING",
    "E0_EVENT_VECTOR_BAD_DIM": "E0_EVENT_VECTOR_BAD_DIM",

    # Entity nodes specific (Stage F1 derived inputs)
    "F1_ENTITY_JSON_BAD": "F1_ENTITY_JSON_BAD",
    "F1_ENTITY_UNKNOWN_RECORD": "F1_ENTITY_UNKNOWN_RECORD",
    "F1_ENTITY_OUTCOME_MISSING": "F1_ENTITY_OUTCOME_MISSING",
    "F1_ENTITY_NODE_MISSING_FIELDS": "F1_ENTITY_NODE_MISSING_FIELDS",
    "F1_ENTITY_MENTION_MISSING_FIELDS": "F1_ENTITY_MENTION_MISSING_FIELDS",
    "F1_ENTITY_NODE_NO_MENTIONS": "F1_ENTITY_NODE_NO_MENTIONS",
    "F1_ENTITY_NODE_NO_CHUNK_IDS": "F1_ENTITY_NODE_NO_CHUNK_IDS",
    "F1_ENTITY_NODE_NO_CHUNK_VECTORS": "F1_ENTITY_NODE_NO_CHUNK_VECTORS",
    "F1_ENTITY_NODE_VECTOR_BAD_DIM": "F1_ENTITY_NODE_VECTOR_BAD_DIM",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False)
    except Exception:
        return json.dumps({"type": type(obj).__name__})


def append_logs_all(logs_all_path: str, line: str) -> None:
    try:
        with open(logs_all_path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception:
        # logs.all is best effort, never fail the stage for logging I/O
        pass


def log_event(logs_all_path: str, run_id: str, stage: str, level: str, msg: str, fields: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        "ts_utc": utc_now_iso(),
        "run_id": run_id,
        "stage": stage,
        "level": level,
        "msg": msg,
        "fields": fields or {},
        "producer": os.path.basename(__file__),
    }
    line = safe_json_dumps(payload)
    append_logs_all(logs_all_path, line)
    # Also emit a compact human line for interactive runs
    out = sys.stderr if level in ("ERROR", "FATAL") else sys.stdout
    print(f"[stageC] {level}: {msg}", file=out)


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_b0_chunks_map(chunks_jsonl_path: str) -> Dict[str, Dict[str, Any]]:
    """
    Returns map chunk_id -> record for record_type="chunk".
    """
    m: Dict[str, Dict[str, Any]] = {}
    with open(chunks_jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            rec = json.loads(s)
            if rec.get("record_type") != "chunk":
                continue
            cid = rec.get("chunk_id")
            if not cid:
                continue
            m[cid] = rec
    return m


def validate_out_db(out_db: str, final_db: str) -> Tuple[bool, str]:
    if os.path.abspath(out_db) == os.path.abspath(final_db):
        return False, "out_db must not be moments.db (refusing in-place mutation)"
    if not out_db.endswith(".db"):
        return False, "out_db must end with .db for MilvusClient local URI support"
    base = os.path.basename(out_db)
    if len(base) >= 36:
        return False, f"milvus_lite requires db filename < 36 chars, got {len(base)}: {base}"
    return True, "ok"


def sanitize_search_result(res: Any, max_hits: int = 5) -> Any:
    """
    Convert PyMilvus search results to JSON-serializable primitives.
    """
    try:
        if not isinstance(res, list):
            return {"type": type(res).__name__}

        out_outer: List[Any] = []
        for one_query in res:
            if not isinstance(one_query, list):
                out_outer.append({"type": type(one_query).__name__})
                continue

            hits_out: List[Dict[str, Any]] = []
            for h in one_query[:max_hits]:
                item: Dict[str, Any] = {}

                if isinstance(h, dict):
                    item["id"] = h.get("id")
                    item["distance"] = h.get("distance")
                    ent = h.get("entity") or {}
                    if isinstance(ent, dict):
                        for k in ["pk", "media_id", "start_s", "end_s"]:
                            if k in ent:
                                item[k] = ent.get(k)
                else:
                    for attr in ["id", "distance"]:
                        if hasattr(h, attr):
                            item[attr] = getattr(h, attr)

                    ent = None
                    if hasattr(h, "entity"):
                        ent = getattr(h, "entity")
                    if ent is None and hasattr(h, "fields"):
                        ent = getattr(h, "fields")

                    if ent is not None:
                        try:
                            for k in ["pk", "media_id", "start_s", "end_s"]:
                                if isinstance(ent, dict) and k in ent:
                                    item[k] = ent.get(k)
                                else:
                                    if hasattr(ent, "get"):
                                        v = ent.get(k)
                                        if v is not None:
                                            item[k] = v
                        except Exception:
                            pass

                hits_out.append(item)

            out_outer.append(hits_out)

        return out_outer
    except Exception as e:
        return {"sanitize_error": f"{type(e).__name__}: {e}"}


def _pid_is_alive(pid: int) -> bool:
    try:
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _parse_iso_utc(ts: str) -> Optional[float]:
    try:
        # expects Z suffix
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def acquire_lock(lock_dir: str, lock_name: str, run_id: str, allow_break_stale: bool, stale_s: float) -> Tuple[bool, str]:
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, lock_name)

    def _write_lock() -> None:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        payload = {
            "run_id": run_id,
            "created_utc": utc_now_iso(),
            "pid": os.getpid(),
            "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        }
        os.write(fd, (json.dumps(payload) + "\n").encode("utf-8"))
        os.close(fd)

    try:
        _write_lock()
        return True, lock_path
    except FileExistsError:
        # Check staleness if allowed
        if not allow_break_stale:
            return False, lock_path

        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                first = (f.readline() or "").strip()
            meta = json.loads(first) if first else {}
        except Exception:
            meta = {}

        pid = meta.get("pid")
        created = meta.get("created_utc")
        pid_alive = isinstance(pid, int) and _pid_is_alive(pid)
        created_ts = _parse_iso_utc(created) if isinstance(created, str) else None

        now_ts = datetime.now(timezone.utc).timestamp()
        age_s = (now_ts - created_ts) if created_ts is not None else None

        if (not pid_alive) and (age_s is not None) and (age_s >= float(stale_s)):
            try:
                os.remove(lock_path)
            except Exception:
                return False, lock_path
            try:
                _write_lock()
                return True, lock_path
            except Exception:
                return False, lock_path

        return False, lock_path
    except Exception as e:
        return False, f"{lock_path} ({type(e).__name__}: {e})"


def release_lock(lock_path: str) -> None:
    try:
        if lock_path and os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception:
        pass


def final_db_in_use_lsof(final_db: str) -> Tuple[str, str]:
    """
    Returns (status, detail)
    status in: "in_use", "not_in_use", "unknown"
    """
    try:
        p = subprocess.run(["lsof", final_db], capture_output=True, text=True, check=False)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()

        if p.returncode == 0 and out:
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if lines and lines[0].upper().startswith("COMMAND"):
                lines = lines[1:]
            if lines:
                return "in_use", lines[0][:200]
            return "in_use", out.splitlines()[0][:200]

        if p.returncode == 1:
            return "not_in_use", "no_open_handles"
        return "unknown", f"lsof_unknown rc={p.returncode} err={err[:200]}"
    except FileNotFoundError:
        return "unknown", "lsof_not_found"
    except Exception as e:
        return "unknown", f"lsof_error {type(e).__name__}: {e}"


def _mean_vectors(vs: List[List[float]]) -> Optional[List[float]]:
    if not vs:
        return None
    n = len(vs)
    dim = len(vs[0]) if vs else 0
    if dim != VECTOR_DIM:
        return None
    acc = [0.0] * dim
    for v in vs:
        if not isinstance(v, list) or len(v) != dim:
            return None
        for i in range(dim):
            acc[i] += float(v[i])
    inv = 1.0 / float(n)
    return [x * inv for x in acc]


def _load_events_jsonl(events_path: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    event_recs: List[Dict[str, Any]] = []
    outcome: Optional[Dict[str, Any]] = None
    try:
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if not isinstance(obj, dict):
                    return [], None, REASON["E0_EVENTS_JSON_BAD"]
                rt = obj.get("record_type")
                if rt == "event_record":
                    event_recs.append(obj)
                elif rt == "events_outcome":
                    outcome = obj
                else:
                    return [], None, REASON["E0_EVENTS_UNKNOWN_RECORD"]
        if outcome is None:
            return event_recs, None, REASON["E0_EVENTS_OUTCOME_MISSING"]
        return event_recs, outcome, None
    except Exception:
        return [], None, REASON["E0_EVENTS_JSON_BAD"]


def _load_entity_nodes_jsonl(path: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    nodes: List[Dict[str, Any]] = []
    outcome: Optional[Dict[str, Any]] = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if not isinstance(obj, dict):
                    return [], None, REASON["F1_ENTITY_JSON_BAD"]
                rt = obj.get("record_type")
                if rt == "entity_node":
                    nodes.append(obj)
                elif rt == "entity_link_outcome":
                    outcome = obj
                else:
                    return [], None, REASON["F1_ENTITY_UNKNOWN_RECORD"]
        if outcome is None:
            return nodes, None, REASON["F1_ENTITY_OUTCOME_MISSING"]
        return nodes, outcome, None
    except Exception:
        return [], None, REASON["F1_ENTITY_JSON_BAD"]


def _load_entity_mentions_jsonl(path: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    mens: List[Dict[str, Any]] = []
    outcome: Optional[Dict[str, Any]] = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if not isinstance(obj, dict):
                    return [], None, REASON["F1_ENTITY_JSON_BAD"]
                rt = obj.get("record_type")
                if rt == "entity_mention":
                    mens.append(obj)
                elif rt == "entity_link_outcome":
                    outcome = obj
                else:
                    return [], None, REASON["F1_ENTITY_UNKNOWN_RECORD"]
        if outcome is None:
            return mens, None, REASON["F1_ENTITY_OUTCOME_MISSING"]
        return mens, outcome, None
    except Exception:
        return [], None, REASON["F1_ENTITY_JSON_BAD"]


def parse_mids_arg(mids_arg: str) -> List[str]:
    mids_arg = (mids_arg or "").strip()
    if not mids_arg:
        return []
    parts = [p.strip() for p in mids_arg.split(",")]
    mids: List[str] = []
    for p in parts:
        if not p:
            continue
        if len(p) != 64 or any(c not in "0123456789abcdef" for c in p):
            raise ValueError(f"invalid mid in --mids: {p}")
        mids.append(p)
    return mids


def prune_run_candidates(milvus_dir: str, run_stamp: str, keep_basename: str) -> None:
    """
    Remove old candidate DBs for this run_stamp, except keep_basename.
    """
    patt = os.path.join(milvus_dir, f"m_build_{run_stamp}_a*.db")
    for p in glob.glob(patt):
        if os.path.basename(p) == keep_basename:
            continue
        try:
            os.remove(p)
        except Exception:
            pass


def close_milvus_client_best_effort(client: Any) -> None:
    """
    Close/release MilvusClient handles deterministically if possible.
    """
    try:
        if client is None:
            return
        if hasattr(client, "close") and callable(getattr(client, "close")):
            client.close()
            return
    except Exception:
        pass
    # Fallback: drop reference
    try:
        del client
    except Exception:
        pass


def build_db_once(
    out_db: str,
    emb_dir: str,
    chunks_dir: str,
    events_dir: str,
    entity_nodes_dir: str,
    entity_mentions_dir: str,
    manifest: Dict[str, Any],
    args: argparse.Namespace,
    mids_filter: List[str],
    logs_all_path: str,
    run_id: str,
) -> Tuple[bool, str]:
    """
    Build and validate a DB at out_db. Returns (ok, detail).
    On success, manifest is updated with counts/validation for the build.
    """
    # Reset per-build counts (attempt scoped)
    c = manifest["counts"]
    for k in list(c.keys()):
        c[k] = 0

    # Structured time mismatch stats
    manifest["time_mismatch"] = {
        "count": 0,
        "max_abs_start_s": 0.0,
        "max_abs_end_s": 0.0,
        "samples": [],  # capped small
        "tolerance_s": float(args.time_mismatch_tol_s),
    }

    if os.path.exists(out_db):
        try:
            os.remove(out_db)
        except Exception:
            pass

    try:
        from pymilvus import MilvusClient, DataType
    except Exception as e:
        manifest["reasons"][REASON["MILVUS_TOOL_ERROR"]] = int(manifest["reasons"].get(REASON["MILVUS_TOOL_ERROR"], 0)) + 1
        manifest["warnings"].append(f"import_pymilvus_failed: {type(e).__name__}: {e}")
        log_event(logs_all_path, run_id, "C", "ERROR", "import_pymilvus_failed", {"error": f"{type(e).__name__}: {e}"})
        return False, "import_pymilvus_failed"

    client = None
    try:
        client = MilvusClient(uri=out_db)

        # Drop any existing collections in this out_db if they exist
        try:
            cols = client.list_collections()
            for coll in [ASR_COLLECTION, EVENTS_COLLECTION, ENTITY_NODES_COLLECTION]:
                if coll in cols:
                    client.drop_collection(collection_name=coll)
        except Exception:
            pass

        # ASR schema
        schema_asr = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema_asr.add_field(field_name="pk", datatype=DataType.VARCHAR, is_primary=True, max_length=256)
        schema_asr.add_field(field_name="media_id", datatype=DataType.VARCHAR, max_length=128)
        schema_asr.add_field(field_name="start_s", datatype=DataType.FLOAT)
        schema_asr.add_field(field_name="end_s", datatype=DataType.FLOAT)
        schema_asr.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=8192)
        schema_asr.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
        client.create_collection(collection_name=ASR_COLLECTION, schema=schema_asr)

        # Events schema
        schema_evt = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema_evt.add_field(field_name="pk", datatype=DataType.VARCHAR, is_primary=True, max_length=256)
        schema_evt.add_field(field_name="media_id", datatype=DataType.VARCHAR, max_length=128)
        schema_evt.add_field(field_name="t0_ms", datatype=DataType.INT64)
        schema_evt.add_field(field_name="t1_ms", datatype=DataType.INT64)
        schema_evt.add_field(field_name="start_s", datatype=DataType.FLOAT)
        schema_evt.add_field(field_name="end_s", datatype=DataType.FLOAT)
        schema_evt.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=8192)
        schema_evt.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
        client.create_collection(collection_name=EVENTS_COLLECTION, schema=schema_evt)

        # Entity nodes schema
        schema_en = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema_en.add_field(field_name="pk", datatype=DataType.VARCHAR, is_primary=True, max_length=256)
        schema_en.add_field(field_name="media_id", datatype=DataType.VARCHAR, max_length=128)
        schema_en.add_field(field_name="start_s", datatype=DataType.FLOAT)
        schema_en.add_field(field_name="end_s", datatype=DataType.FLOAT)
        schema_en.add_field(field_name="entity_type", datatype=DataType.VARCHAR, max_length=64)
        schema_en.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=8192)
        schema_en.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
        client.create_collection(collection_name=ENTITY_NODES_COLLECTION, schema=schema_en)

        # Indexes
        index_params = MilvusClient.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_name=INDEX_NAME,
            index_type="FLAT",
            metric_type="COSINE",
            params={},
        )
        client.create_index(collection_name=ASR_COLLECTION, index_params=index_params)
        client.create_index(collection_name=EVENTS_COLLECTION, index_params=index_params)
        client.create_index(collection_name=ENTITY_NODES_COLLECTION, index_params=index_params)

        # Choose embeddings to process
        emb_paths: List[str] = []
        if mids_filter:
            for mid in mids_filter:
                p = os.path.join(emb_dir, f"{mid}.json")
                if os.path.isfile(p):
                    emb_paths.append(p)
                else:
                    c["b1_missing"] += 1
                    manifest["reasons"][REASON["B1_MISSING_EMBEDDINGS_FILE"]] = int(manifest["reasons"].get(REASON["B1_MISSING_EMBEDDINGS_FILE"], 0)) + 1
                    manifest["warnings"].append(f"missing_embeddings_for_mid: {mid}")
        else:
            emb_paths = sorted(glob.glob(os.path.join(emb_dir, "*.json")))
            if args.max_files and args.max_files > 0:
                emb_paths = emb_paths[: args.max_files]

        manifest["inputs"]["embedding_files_seen"] = len(emb_paths)

        if len(emb_paths) == 0:
            manifest["reasons"][REASON["EMBEDDINGS_DIR_EMPTY"]] = int(manifest["reasons"].get(REASON["EMBEDDINGS_DIR_EMPTY"], 0)) + 1
            return True, "no_embeddings"

        rows_batch_asr: List[Dict[str, Any]] = []
        rows_batch_evt: List[Dict[str, Any]] = []
        rows_batch_en: List[Dict[str, Any]] = []

        BATCH_INSERT_ASR = 500
        BATCH_INSERT_EVT = 200
        BATCH_INSERT_EN = 200

        def bump(r: str, n: int = 1) -> None:
            manifest["reasons"][r] = int(manifest["reasons"].get(r, 0)) + n

        def note_time_mismatch(mid: str, cid: str, b0_s: float, b0_e: float, b1_s: float, b1_e: float) -> None:
            tm = manifest.get("time_mismatch", {})
            tm["count"] = int(tm.get("count", 0)) + 1
            ds = abs(b0_s - b1_s)
            de = abs(b0_e - b1_e)
            tm["max_abs_start_s"] = float(max(float(tm.get("max_abs_start_s", 0.0)), ds))
            tm["max_abs_end_s"] = float(max(float(tm.get("max_abs_end_s", 0.0)), de))
            samples = tm.get("samples", [])
            if isinstance(samples, list) and len(samples) < int(args.time_mismatch_sample_max):
                samples.append(
                    {
                        "media_id": mid,
                        "chunk_id": cid,
                        "b0": [b0_s, b0_e],
                        "b1": [b1_s, b1_e],
                        "abs_delta": [ds, de],
                    }
                )
            tm["samples"] = samples
            manifest["time_mismatch"] = tm

        # Remember one known pk for deterministic validation
        first_inserted_pk: Optional[str] = None

        for ep in emb_paths:
            mid = os.path.splitext(os.path.basename(ep))[0]
            c["media_seen"] += 1

            chunks_path = os.path.join(chunks_dir, f"{mid}.jsonl")
            if not os.path.exists(chunks_path):
                c["b0_missing"] += 1
                bump(REASON["B0_MISSING_CHUNKS_FILE"])
                continue

            b0_map = load_b0_chunks_map(chunks_path)
            b1 = load_json(ep)

            vec_recs = b1.get("vectors", [])
            if not isinstance(vec_recs, list):
                bump(REASON["B1_VECTOR_BAD_TYPE"])
                continue

            c["b1_records_seen"] += len(vec_recs)

            # Deterministic mapping from chunk_id -> vector for records that pass ASR insertion checks.
            vec_map: Dict[str, List[float]] = {}
            time_map: Dict[str, Tuple[float, float]] = {}

            for rec in vec_recs:
                elig = rec.get("eligibility") or {}
                if not isinstance(elig, dict) or not bool(elig.get("eligible", False)):
                    c["rows_skipped"] += 1
                    bump(REASON["B1_RECORD_INELIGIBLE"])
                    continue

                cid = rec.get("chunk_id")
                if not cid or cid not in b0_map:
                    c["rows_skipped"] += 1
                    bump(REASON["B0_CHUNK_NOT_FOUND"])
                    continue

                b0 = b0_map[cid]
                pol = (b0.get("policy") or {})
                if not bool(pol.get("allow_indexing", False)):
                    c["rows_skipped"] += 1
                    bump(REASON["B0_POLICY_NO_INDEX"])
                    continue

                embed_text = rec.get("embed_text", "")
                if not isinstance(embed_text, str) or not embed_text.startswith("passage: "):
                    c["rows_skipped"] += 1
                    bump(REASON["B1_TEXT_BAD_PREFIX"])
                    continue

                vec = rec.get("vector")
                if not isinstance(vec, list):
                    c["rows_skipped"] += 1
                    bump(REASON["B1_VECTOR_BAD_TYPE"])
                    continue
                if len(vec) != VECTOR_DIM:
                    c["rows_skipped"] += 1
                    bump(REASON["B1_VECTOR_BAD_DIM"])
                    continue

                b0_s = float(b0.get("start_s", 0.0))
                b0_e = float(b0.get("end_s", 0.0))
                b1_s = float(rec.get("start_s", 0.0))
                b1_e = float(rec.get("end_s", 0.0))
                if abs(b0_s - b1_s) > float(args.time_mismatch_tol_s) or abs(b0_e - b1_e) > float(args.time_mismatch_tol_s):
                    bump(REASON["B0_B1_TIME_MISMATCH"])
                    note_time_mismatch(mid, cid, b0_s, b0_e, b1_s, b1_e)

                pk = f"{mid}:{cid}"

                rows_batch_asr.append(
                    {
                        "pk": pk,
                        "media_id": mid,
                        "start_s": b1_s,
                        "end_s": b1_e,
                        "text": embed_text,
                        "vector": vec,
                    }
                )

                if first_inserted_pk is None:
                    first_inserted_pk = pk

                vec_map[cid] = vec
                time_map[cid] = (b1_s, b1_e)

                if len(rows_batch_asr) >= BATCH_INSERT_ASR:
                    try:
                        client.insert(collection_name=ASR_COLLECTION, data=rows_batch_asr)
                        c["rows_inserted"] += len(rows_batch_asr)
                        rows_batch_asr = []
                    except Exception as e:
                        bump(REASON["INSERT_ERROR"])
                        manifest["warnings"].append(f"insert_error_asr: {type(e).__name__}: {e}")
                        c["rows_skipped"] += len(rows_batch_asr)
                        rows_batch_asr = []

            # EVENTS (optional)
            if args.include_events:
                events_path = os.path.join(events_dir, f"{mid}.jsonl")
                if os.path.isfile(events_path):
                    c["events_files_seen"] += 1
                    ev_recs, ev_outcome, ev_err = _load_events_jsonl(events_path)
                    if ev_err is not None:
                        bump(ev_err)
                    if ev_outcome is None:
                        bump(REASON["E0_EVENTS_OUTCOME_MISSING"])

                    c["events_records_seen"] += len(ev_recs)

                    for ev in ev_recs:
                        eid = ev.get("event_id")
                        t0_ms = ev.get("t0_ms")
                        t1_ms = ev.get("t1_ms")
                        etxt = ev.get("event_text")

                        if not isinstance(eid, str) or not eid:
                            bump(REASON["E0_EVENT_MISSING_FIELDS"])
                            c["events_rows_skipped"] += 1
                            continue
                        if not isinstance(t0_ms, int) or not isinstance(t1_ms, int) or t0_ms < 0 or t1_ms < 0 or t1_ms < t0_ms:
                            bump(REASON["E0_EVENT_MISSING_FIELDS"])
                            c["events_rows_skipped"] += 1
                            continue
                        if not isinstance(etxt, str):
                            bump(REASON["E0_EVENT_TEXT_MISSING"])
                            c["events_rows_skipped"] += 1
                            continue

                        evidence = ev.get("evidence") or {}
                        chunk_ids = evidence.get("chunk_ids") if isinstance(evidence, dict) else None
                        if not isinstance(chunk_ids, list) or not chunk_ids:
                            bump(REASON["E0_EVENT_MISSING_CHUNK_IDS"])
                            c["events_rows_skipped"] += 1
                            continue

                        vecs: List[List[float]] = []
                        for cid in chunk_ids:
                            if isinstance(cid, str) and cid in vec_map:
                                vecs.append(vec_map[cid])

                        if not vecs:
                            bump(REASON["E0_EVENT_NO_CHUNK_VECTORS"])
                            c["events_rows_skipped"] += 1
                            continue

                        vmean = _mean_vectors(vecs)
                        if vmean is None or len(vmean) != VECTOR_DIM:
                            bump(REASON["E0_EVENT_VECTOR_BAD_DIM"])
                            c["events_rows_skipped"] += 1
                            continue

                        text_field = etxt.strip()
                        if not text_field.startswith("passage: "):
                            text_field = "passage: " + text_field

                        start_s = float(t0_ms) / 1000.0
                        end_s = float(t1_ms) / 1000.0

                        rows_batch_evt.append(
                            {
                                "pk": eid,
                                "media_id": mid,
                                "t0_ms": int(t0_ms),
                                "t1_ms": int(t1_ms),
                                "start_s": float(start_s),
                                "end_s": float(end_s),
                                "text": text_field,
                                "vector": vmean,
                            }
                        )

                        if len(rows_batch_evt) >= BATCH_INSERT_EVT:
                            try:
                                client.insert(collection_name=EVENTS_COLLECTION, data=rows_batch_evt)
                                c["events_rows_inserted"] += len(rows_batch_evt)
                                rows_batch_evt = []
                            except Exception as e:
                                bump(REASON["INSERT_ERROR"])
                                manifest["warnings"].append(f"insert_error_events: {type(e).__name__}: {e}")
                                c["events_rows_skipped"] += len(rows_batch_evt)
                                rows_batch_evt = []
                # missing events file is not a reason if optional and absent

            # ENTITY NODES (optional)
            if args.include_entities:
                nodes_path = os.path.join(entity_nodes_dir, f"{mid}.jsonl")
                mentions_path = os.path.join(entity_mentions_dir, f"{mid}.jsonl")

                have_nodes = os.path.isfile(nodes_path)
                have_mentions = os.path.isfile(mentions_path)

                if have_nodes:
                    c["entity_nodes_files_seen"] += 1
                if have_mentions:
                    c["entity_mentions_files_seen"] += 1

                if have_nodes and have_mentions:
                    nodes, out_n, err_n = _load_entity_nodes_jsonl(nodes_path)
                    mens, out_m, err_m = _load_entity_mentions_jsonl(mentions_path)

                    if err_n is not None:
                        bump(err_n)
                    if err_m is not None:
                        bump(err_m)
                    if out_n is None or out_m is None:
                        bump(REASON["F1_ENTITY_OUTCOME_MISSING"])

                    c["entity_nodes_records_seen"] += len(nodes)
                    c["entity_mentions_records_seen"] += len(mens)

                    mention_chunk_ids: Dict[str, List[str]] = {}
                    for mrec in mens:
                        mention_id = mrec.get("mention_id")
                        ev = mrec.get("evidence") or {}
                        cids = ev.get("chunk_ids") if isinstance(ev, dict) else None
                        if not isinstance(mention_id, str) or not mention_id:
                            bump(REASON["F1_ENTITY_MENTION_MISSING_FIELDS"])
                            continue
                        if not isinstance(cids, list) or not cids:
                            bump(REASON["F1_ENTITY_NODE_NO_CHUNK_IDS"])
                            continue
                        mention_chunk_ids[mention_id] = [c for c in cids if isinstance(c, str) and c]

                    for nrec in nodes:
                        node_id = nrec.get("entity_node_id")
                        display = nrec.get("display_name")
                        etype = nrec.get("entity_type")
                        mention_ids = nrec.get("mention_ids")

                        if not isinstance(node_id, str) or not node_id:
                            bump(REASON["F1_ENTITY_NODE_MISSING_FIELDS"])
                            c["entity_nodes_rows_skipped"] += 1
                            continue
                        if not isinstance(display, str) or not display.strip():
                            bump(REASON["F1_ENTITY_NODE_MISSING_FIELDS"])
                            c["entity_nodes_rows_skipped"] += 1
                            continue
                        if not isinstance(etype, str) or not etype.strip():
                            bump(REASON["F1_ENTITY_NODE_MISSING_FIELDS"])
                            c["entity_nodes_rows_skipped"] += 1
                            continue
                        if not isinstance(mention_ids, list) or not mention_ids:
                            bump(REASON["F1_ENTITY_NODE_NO_MENTIONS"])
                            c["entity_nodes_rows_skipped"] += 1
                            continue

                        cids_union: List[str] = []
                        seen_c = set()
                        for mid2 in mention_ids:
                            if not isinstance(mid2, str):
                                continue
                            cids = mention_chunk_ids.get(mid2) or []
                            for c2 in cids:
                                if c2 not in seen_c:
                                    seen_c.add(c2)
                                    cids_union.append(c2)

                        if not cids_union:
                            bump(REASON["F1_ENTITY_NODE_NO_CHUNK_IDS"])
                            c["entity_nodes_rows_skipped"] += 1
                            continue

                        vecs: List[List[float]] = []
                        tmins: List[float] = []
                        tmaxs: List[float] = []
                        for cid in cids_union:
                            if cid in vec_map:
                                vecs.append(vec_map[cid])
                                t0, t1 = time_map.get(cid, (None, None))
                                if isinstance(t0, float):
                                    tmins.append(t0)
                                if isinstance(t1, float):
                                    tmaxs.append(t1)

                        if not vecs:
                            bump(REASON["F1_ENTITY_NODE_NO_CHUNK_VECTORS"])
                            c["entity_nodes_rows_skipped"] += 1
                            continue

                        vmean = _mean_vectors(vecs)
                        if vmean is None or len(vmean) != VECTOR_DIM:
                            bump(REASON["F1_ENTITY_NODE_VECTOR_BAD_DIM"])
                            c["entity_nodes_rows_skipped"] += 1
                            continue

                        start_s = float(min(tmins)) if tmins else 0.0
                        end_s = float(max(tmaxs)) if tmaxs else float(start_s)

                        text_field = display.strip()
                        if not text_field.startswith("passage: "):
                            text_field = "passage: " + text_field

                        rows_batch_en.append(
                            {
                                "pk": node_id,
                                "media_id": mid,
                                "start_s": float(start_s),
                                "end_s": float(end_s),
                                "entity_type": etype.strip(),
                                "text": text_field,
                                "vector": vmean,
                            }
                        )

                        if len(rows_batch_en) >= BATCH_INSERT_EN:
                            try:
                                client.insert(collection_name=ENTITY_NODES_COLLECTION, data=rows_batch_en)
                                c["entity_nodes_rows_inserted"] += len(rows_batch_en)
                                rows_batch_en = []
                            except Exception as e:
                                bump(REASON["INSERT_ERROR"])
                                manifest["warnings"].append(f"insert_error_entity_nodes: {type(e).__name__}: {e}")
                                c["entity_nodes_rows_skipped"] += len(rows_batch_en)
                                rows_batch_en = []

        # Flush pending inserts
        if rows_batch_asr:
            try:
                client.insert(collection_name=ASR_COLLECTION, data=rows_batch_asr)
                c["rows_inserted"] += len(rows_batch_asr)
                rows_batch_asr = []
            except Exception as e:
                bump(REASON["INSERT_ERROR"])
                manifest["warnings"].append(f"insert_error_asr: {type(e).__name__}: {e}")
                c["rows_skipped"] += len(rows_batch_asr)
                rows_batch_asr = []

        if rows_batch_evt:
            try:
                client.insert(collection_name=EVENTS_COLLECTION, data=rows_batch_evt)
                c["events_rows_inserted"] += len(rows_batch_evt)
                rows_batch_evt = []
            except Exception as e:
                bump(REASON["INSERT_ERROR"])
                manifest["warnings"].append(f"insert_error_events: {type(e).__name__}: {e}")
                c["events_rows_skipped"] += len(rows_batch_evt)
                rows_batch_evt = []

        if rows_batch_en:
            try:
                client.insert(collection_name=ENTITY_NODES_COLLECTION, data=rows_batch_en)
                c["entity_nodes_rows_inserted"] += len(rows_batch_en)
                rows_batch_en = []
            except Exception as e:
                bump(REASON["INSERT_ERROR"])
                manifest["warnings"].append(f"insert_error_entity_nodes: {type(e).__name__}: {e}")
                c["entity_nodes_rows_skipped"] += len(rows_batch_en)
                rows_batch_en = []

        # Flush + load
        for coll, tag in [
            (ASR_COLLECTION, "asr"),
            (EVENTS_COLLECTION, "events"),
            (ENTITY_NODES_COLLECTION, "entity_nodes"),
        ]:
            try:
                client.flush(collection_name=coll)
            except Exception as e:
                bump(REASON["FLUSH_FAILED"])
                manifest["warnings"].append(f"flush_failed_{tag}: {type(e).__name__}: {e}")

            try:
                client.load_collection(collection_name=coll)
            except Exception as e:
                bump(REASON["LOAD_FAILED"])
                manifest["warnings"].append(f"load_failed_{tag}: {type(e).__name__}: {e}")

        # Stats
        try:
            stats = client.get_collection_stats(collection_name=ASR_COLLECTION)
            c["milvus_row_count"] = int(stats.get("row_count", -1))
        except Exception as e:
            bump(REASON["STATS_FAILED"])
            manifest["warnings"].append(f"stats_failed_asr: {type(e).__name__}: {e}")
            c["milvus_row_count"] = -1

        try:
            stats_e = client.get_collection_stats(collection_name=EVENTS_COLLECTION)
            c["milvus_events_row_count"] = int(stats_e.get("row_count", -1))
        except Exception as e:
            bump(REASON["STATS_FAILED"])
            manifest["warnings"].append(f"stats_failed_events: {type(e).__name__}: {e}")
            c["milvus_events_row_count"] = -1

        try:
            stats_n = client.get_collection_stats(collection_name=ENTITY_NODES_COLLECTION)
            c["milvus_entity_nodes_row_count"] = int(stats_n.get("row_count", -1))
        except Exception as e:
            bump(REASON["STATS_FAILED"])
            manifest["warnings"].append(f"stats_failed_entity_nodes: {type(e).__name__}: {e}")
            c["milvus_entity_nodes_row_count"] = -1

        # Validation search (deterministic pk if available)
        validation: Dict[str, Any] = {}

        def _validate_collection(coll: str, output_fields: List[str], pk_value: Optional[str]) -> Dict[str, Any]:
            try:
                if not pk_value:
                    # fallback: limit 1 but do not rely on pk!= '' filter semantics
                    sample = client.query(
                        collection_name=coll,
                        filter=None,
                        output_fields=["pk", "vector"],
                        limit=1,
                    )
                else:
                    sample = client.query(
                        collection_name=coll,
                        filter=f'pk == "{pk_value}"',
                        output_fields=["pk", "vector"],
                        limit=1,
                    )

                if sample and isinstance(sample, list):
                    v = sample[0].get("vector")
                    if isinstance(v, list) and len(v) == VECTOR_DIM:
                        res = client.search(
                            collection_name=coll,
                            data=[v],
                            anns_field="vector",
                            limit=3,
                            output_fields=output_fields,
                        )
                        return {"search_ok": True, "hits_sanitized": sanitize_search_result(res, max_hits=5)}
                bump(REASON["VALIDATION_FAILED"])
                return {"search_ok": False, "reason": "sample_vector_missing_or_bad"}
            except Exception as e:
                bump(REASON["VALIDATION_FAILED"])
                return {"search_ok": False, "error": f"{type(e).__name__}: {e}"}

        if int(c.get("rows_inserted", 0)) > 0:
            validation["asr"] = _validate_collection(ASR_COLLECTION, ["pk", "media_id", "start_s", "end_s"], first_inserted_pk)
        else:
            validation["asr"] = {"search_ok": True, "hits_sanitized": []}

        if int(c.get("events_rows_inserted", 0)) > 0:
            validation["events"] = _validate_collection(EVENTS_COLLECTION, ["pk", "media_id", "start_s", "end_s"], None)
        else:
            validation["events"] = {"search_ok": True, "hits_sanitized": []}

        if int(c.get("entity_nodes_rows_inserted", 0)) > 0:
            validation["entity_nodes"] = _validate_collection(ENTITY_NODES_COLLECTION, ["pk", "media_id", "start_s", "end_s"], None)
        else:
            validation["entity_nodes"] = {"search_ok": True, "hits_sanitized": []}

        manifest["validation"] = validation

        if manifest.get("validation", {}).get("asr", {}).get("search_ok") is False and int(c.get("rows_inserted", 0)) > 0:
            return False, "validation_failed_asr"
        if manifest.get("validation", {}).get("events", {}).get("search_ok") is False and int(c.get("events_rows_inserted", 0)) > 0:
            return False, "validation_failed_events"
        if manifest.get("validation", {}).get("entity_nodes", {}).get("search_ok") is False and int(c.get("entity_nodes_rows_inserted", 0)) > 0:
            return False, "validation_failed_entity_nodes"

        return True, "ok"

    except Exception as e:
        manifest["reasons"][REASON["MILVUS_TOOL_ERROR"]] = int(manifest["reasons"].get(REASON["MILVUS_TOOL_ERROR"], 0)) + 1
        manifest["warnings"].append(f"build_db_exception: {type(e).__name__}: {e}")
        log_event(logs_all_path, run_id, "C", "ERROR", "build_db_exception", {"error": f"{type(e).__name__}: {e}"})
        return False, "build_db_exception"
    finally:
        close_milvus_client_best_effort(client)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=CATALOG_DEFAULT)
    ap.add_argument("--build_only", action="store_true", help="Build and validate DB, but do NOT swap into moments.db.")
    ap.add_argument("--max_files", type=int, default=0, help="For testing: limit number of media IDs processed (0=all)")
    ap.add_argument("--mids", default="", help="Comma-separated list of media_ids to process. When set, overrides --max_files.")
    ap.add_argument("--time_mismatch_tol_s", type=float, default=0.25, help="Warn threshold if B0 vs B1 times differ")
    ap.add_argument("--time_mismatch_sample_max", type=int, default=20, help="Max mismatch samples recorded in manifest")
    ap.add_argument("--publish_retry_max_attempts", type=int, default=12, help="Max publish attempts (rebuilds each attempt)")
    ap.add_argument("--publish_retry_sleep_s", type=float, default=10.0, help="Seconds to sleep between publish attempts")
    ap.add_argument("--publish_allow_without_verification", action="store_true",
                   help="Allow publish when DB-in-use verification is unknown (eg lsof missing). Records a reason code.")
    ap.add_argument("--include_events", action="store_true", help="Try to ingest Stage E0 events if present")
    ap.add_argument("--include_entities", action="store_true", help="Try to ingest Stage F1 entity nodes/mentions if present")
    ap.add_argument("--lock_dir", default=DEFAULT_LOCK_DIR)
    ap.add_argument("--lock_name", default=DEFAULT_LOCK_NAME)
    ap.add_argument("--break_stale_lock", action="store_true", help="Break stale Stage C lock if holder pid is dead and lock age exceeds --lock_stale_s")
    ap.add_argument("--lock_stale_s", type=float, default=6 * 3600, help="Seconds after which a dead lock is considered stale (default 6h)")
    args = ap.parse_args()

    # Default behaviour: include optional datasets if present, but never spam missing reasons
    if not args.include_events and not args.include_entities:
        # preserve backwards compatibility with your previous “optional but used when present” intent
        args.include_events = True
        args.include_entities = True

    mids_filter: List[str] = []
    if args.mids.strip():
        try:
            mids_filter = parse_mids_arg(args.mids)
        except Exception as e:
            print(f"[stageC] FATAL: bad --mids: {e}", file=sys.stderr)
            return 2

    producer = os.path.basename(__file__)
    stage = "C"
    media_id = "__catalog__"

    logs_all_path = os.environ.get(LOGS_ALL_ENV, LOGS_ALL_DEFAULT)

    catalog = os.path.abspath(args.catalog)
    emb_dir = os.path.join(catalog, "embeddings")
    chunks_dir = os.path.join(catalog, "chunks")
    events_dir = os.path.join(catalog, "events")
    entity_nodes_dir = os.path.join(catalog, "entity_nodes")
    entity_mentions_dir = os.path.join(catalog, "entity_mentions")
    milvus_dir = os.path.join(catalog, "milvus")
    audit_dir = os.path.join(catalog, "audit")

    os.makedirs(milvus_dir, exist_ok=True)
    os.makedirs(audit_dir, exist_ok=True)
    os.makedirs(events_dir, exist_ok=True)
    os.makedirs(entity_nodes_dir, exist_ok=True)
    os.makedirs(entity_mentions_dir, exist_ok=True)

    run_stamp = utc_stamp()
    run_id = f"stageC_{run_stamp}"
    manifest_path = os.path.join(audit_dir, f"stageC_build_{run_id}.json")
    final_db = os.path.join(milvus_dir, "moments.db")

    manifest: Dict[str, Any] = {
        "schema_version": "2",
        "media_id": media_id,
        "stage": stage,
        "created_utc": utc_now_iso(),
        "producer": producer,
        "ok": False,
        "outcome": "failed",
        "run_id": run_id,
        "record_type": "stageC_build_manifest",
        "inputs": {
            "embeddings_dir": emb_dir,
            "chunks_dir": chunks_dir,
            "events_dir": events_dir,
            "entity_nodes_dir": entity_nodes_dir,
            "entity_mentions_dir": entity_mentions_dir,
            "embedding_files_seen": 0,
            "mids_filter": mids_filter,
            "include_events": bool(args.include_events),
            "include_entities": bool(args.include_entities),
            "logs_all_path": logs_all_path,
        },
        "outputs": {
            "out_db": None,
            "final_db": final_db if not args.build_only else None,
            "collection_asr": ASR_COLLECTION,
            "collection_events": EVENTS_COLLECTION,
            "collection_entity_nodes": ENTITY_NODES_COLLECTION,
            "index_name": INDEX_NAME,
            "index_type": "FLAT",
            "metric_type": "COSINE",
            "vector_dim": VECTOR_DIM,
        },
        "counts": {
            "media_seen": 0,
            "b0_missing": 0,
            "b1_missing": 0,
            "b1_records_seen": 0,
            "rows_inserted": 0,
            "rows_skipped": 0,
            "events_files_seen": 0,
            "events_records_seen": 0,
            "events_rows_inserted": 0,
            "events_rows_skipped": 0,
            "entity_nodes_files_seen": 0,
            "entity_mentions_files_seen": 0,
            "entity_nodes_records_seen": 0,
            "entity_mentions_records_seen": 0,
            "entity_nodes_rows_inserted": 0,
            "entity_nodes_rows_skipped": 0,
            "milvus_row_count": -1,
            "milvus_events_row_count": -1,
            "milvus_entity_nodes_row_count": -1,
        },
        "reasons": {},
        "warnings": [],
        "versions": {},
        "validation": {},
        "attempts": {
            "publish_retry_max_attempts": int(args.publish_retry_max_attempts),
            "publish_retry_sleep_s": float(args.publish_retry_sleep_s),
            "items": [],
        },
    }

    def bump_reason(r: str, n: int = 1) -> None:
        manifest["reasons"][r] = int(manifest["reasons"].get(r, 0)) + n

    def write_manifest() -> None:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)

    # Version capture
    try:
        import pymilvus
        import milvus_lite
        manifest["versions"]["python"] = sys.version.split()[0]
        manifest["versions"]["pymilvus"] = getattr(pymilvus, "__version__", "unknown")
        manifest["versions"]["milvus_lite"] = getattr(milvus_lite, "__version__", "unknown")
    except Exception as e:
        manifest["warnings"].append(f"version_capture_failed: {type(e).__name__}: {e}")

    log_event(logs_all_path, run_id, "C", "INFO", "stageC_start", {"catalog": catalog, "build_only": bool(args.build_only)})

    got_lock, lock_detail = acquire_lock(
        args.lock_dir,
        args.lock_name,
        run_id,
        allow_break_stale=bool(args.break_stale_lock),
        stale_s=float(args.lock_stale_s),
    )
    lock_path = lock_detail if got_lock else None
    if not got_lock:
        # Distinguish stale lock from just-held lock when break_stale_lock is on
        if args.break_stale_lock:
            bump_reason(REASON["STAGEC_LOCK_STALE"])
        bump_reason(REASON["STAGEC_LOCK_HELD"])
        manifest["ok"] = True
        manifest["outcome"] = "skipped"
        manifest["warnings"].append(f"stagec_lock_held: {lock_detail}")
        write_manifest()
        log_event(logs_all_path, run_id, "C", "WARN", "stageC_skipped_lock_held", {"lock_path": lock_detail})
        return 2

    try:
        attempts = max(1, int(args.publish_retry_max_attempts))
        sleep_s = float(args.publish_retry_sleep_s)

        for attempt_idx in range(1, attempts + 1):
            attempt_rec: Dict[str, Any] = {
                "attempt_idx": attempt_idx,
                "started_utc": utc_now_iso(),
                "out_db": None,
                "build_ok": False,
                "build_detail": None,
                "publish_checked": False,
                "publish_check_status": None,
                "publish_check_detail": None,
                "published": False,
                "archived_prev_db": None,
                "ended_utc": None,
                "summary_counts": {},
            }

            out_db = os.path.join(milvus_dir, f"m_build_{run_stamp}_a{attempt_idx:02d}.db")
            attempt_rec["out_db"] = out_db
            manifest["outputs"]["out_db"] = out_db

            ok_db, msg = validate_out_db(out_db, final_db)
            if not ok_db:
                bump_reason(REASON["OUT_DB_INVALID"])
                manifest["warnings"].append(msg)
                manifest["ok"] = False
                manifest["outcome"] = "failed"
                attempt_rec["build_detail"] = "out_db_invalid"
                attempt_rec["ended_utc"] = utc_now_iso()
                manifest["attempts"]["items"].append(attempt_rec)
                write_manifest()
                log_event(logs_all_path, run_id, "C", "ERROR", "out_db_invalid", {"out_db": out_db, "detail": msg})
                return 2

            # Prune earlier candidates for this run before building the next
            prune_run_candidates(milvus_dir, run_stamp, keep_basename=os.path.basename(out_db))

            log_event(logs_all_path, run_id, "C", "INFO", "build_attempt_start", {"attempt_idx": attempt_idx, "out_db": out_db})

            ok_build, build_detail = build_db_once(
                out_db,
                emb_dir,
                chunks_dir,
                events_dir,
                entity_nodes_dir,
                entity_mentions_dir,
                manifest,
                args,
                mids_filter,
                logs_all_path,
                run_id,
            )

            attempt_rec["build_ok"] = bool(ok_build)
            attempt_rec["build_detail"] = build_detail
            attempt_rec["summary_counts"] = dict(manifest.get("counts", {}))

            # Persist state after each attempt, even if we are about to sleep and retry
            manifest["attempts"]["items"].append(attempt_rec)
            write_manifest()

            if not ok_build:
                bump_reason(REASON["MILVUS_TOOL_ERROR"])
                manifest["ok"] = False
                manifest["outcome"] = "failed"
                manifest["warnings"].append(f"build_failed: {build_detail}")
                write_manifest()
                log_event(logs_all_path, run_id, "C", "ERROR", "build_failed", {"detail": build_detail, "attempt_idx": attempt_idx})
                return 3

            if build_detail == "no_embeddings":
                manifest["ok"] = True
                manifest["outcome"] = "skipped"
                write_manifest()
                log_event(logs_all_path, run_id, "C", "WARN", "no_embeddings_skipped", {})
                return 2

            if int(manifest["counts"].get("rows_inserted", 0)) == 0:
                manifest["ok"] = True
                manifest["outcome"] = "empty"
            else:
                manifest["ok"] = True
                manifest["outcome"] = "success"

            if args.build_only:
                attempt_rec["ended_utc"] = utc_now_iso()
                attempt_rec["published"] = False
                write_manifest()
                log_event(logs_all_path, run_id, "C", "INFO", "build_only_complete", {"out_db": out_db})
                return 0

            # Publish guard: verify moments.db is not in use
            status, detail = final_db_in_use_lsof(final_db)
            attempt_rec["publish_checked"] = True
            attempt_rec["publish_check_status"] = status
            attempt_rec["publish_check_detail"] = detail

            # Update the last attempt record in manifest
            manifest["attempts"]["items"][-1] = attempt_rec
            write_manifest()

            if status == "in_use":
                bump_reason(REASON["FINAL_DB_IN_USE"])
                log_event(logs_all_path, run_id, "C", "WARN", "publish_blocked_db_in_use", {"detail": detail, "attempt_idx": attempt_idx})
                if attempt_idx < attempts:
                    time.sleep(max(0.0, sleep_s))
                    continue
                manifest["ok"] = True
                manifest["outcome"] = "skipped"
                manifest["warnings"].append(f"final_db_in_use_exhausted: {detail}")
                write_manifest()
                return 2

            if status == "unknown":
                bump_reason(REASON["FINAL_DB_IN_USE_UNKNOWN"])
                if not bool(args.publish_allow_without_verification):
                    log_event(logs_all_path, run_id, "C", "WARN", "publish_blocked_unknown_verification",
                              {"detail": detail, "attempt_idx": attempt_idx, "hint": "use --publish_allow_without_verification to override"})
                    if attempt_idx < attempts:
                        time.sleep(max(0.0, sleep_s))
                        continue
                    manifest["ok"] = True
                    manifest["outcome"] = "skipped"
                    manifest["warnings"].append(f"publish_verification_unknown_exhausted: {detail}")
                    write_manifest()
                    return 2
                bump_reason(REASON["PUBLISH_FORCED_WITHOUT_VERIFICATION"])
                log_event(logs_all_path, run_id, "C", "WARN", "publish_forced_without_verification", {"detail": detail})

            # Safe publish-by-swap
            ts = utc_stamp()
            archived = None
            if os.path.exists(final_db):
                archived = os.path.join(milvus_dir, f"moments.db.archived_{ts}")
                shutil.move(final_db, archived)

            shutil.move(out_db, final_db)

            attempt_rec["published"] = True
            attempt_rec["archived_prev_db"] = archived
            attempt_rec["ended_utc"] = utc_now_iso()
            manifest["attempts"]["items"][-1] = attempt_rec

            # Clean up any leftover candidates for this run
            prune_run_candidates(milvus_dir, run_stamp, keep_basename="__keep_none__")

            write_manifest()
            log_event(logs_all_path, run_id, "C", "INFO", "publish_success", {"final_db": final_db, "archived_prev_db": archived})

            return 0

        manifest["ok"] = True
        manifest["outcome"] = "skipped"
        write_manifest()
        return 2

    except Exception as e:
        bump_reason(REASON["MILVUS_TOOL_ERROR"])
        manifest["warnings"].append(f"stageC_exception: {type(e).__name__}: {e}")
        manifest["ok"] = False
        manifest["outcome"] = "failed"
        write_manifest()
        log_event(logs_all_path, run_id, "C", "ERROR", "stageC_exception", {"error": f"{type(e).__name__}: {e}"})
        return 3

    finally:
        if lock_path:
            release_lock(lock_path)
        log_event(logs_all_path, run_id, "C", "INFO", "stageC_end", {"manifest_path": manifest_path})


if __name__ == "__main__":
    sys.exit(main()) 