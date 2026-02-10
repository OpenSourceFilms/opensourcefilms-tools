#!/usr/bin/env python3
"""
query_audit.py

Canonical, machine-facing query tool over the artifact-derived audit index.

Principles:
- Reads /workspace/catalog/audit/index_latest.jsonl (or a specified index path)
- Never consults status JSON
- Never scans the catalog directly
- Returns JSON only (no human prose), suitable for LLM tool calls and future Milvus orchestration

It can answer:
- list media_ids by readiness tier
- explain one media_id (paths, existence, checks, tier)
- list missing artifacts by type
- compute a summary (delegates to audit_index.py --summary semantics in-line)
- diff summaries (two index files) to detect changes without human eyeballing
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple


def fail(msg: str, code: int = 2) -> None:
    payload = {"ok": False, "error": msg}
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(code)


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            yield json.loads(s)


def bool_get(d: Dict[str, Any], path: List[str], default: bool = False) -> bool:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return bool(cur)


def str_get(d: Dict[str, Any], path: List[str], default: str = "") -> str:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return str(cur) if cur is not None else default


def check_ok(rec: Dict[str, Any], key: str) -> bool:
    """
    True only if check ran and ok.
    """
    chk = rec.get("checks", {}).get(key, {})
    ran = bool(chk.get("ran", False))
    ok = bool(chk.get("ok", False)) if ran else False
    return ran and ok


def tier_of(rec: Dict[str, Any]) -> str:
    """
    Readiness logic for downstream consumers.

    Returns one of: "none", "stageA_ready", "semantic_ready", "editorial_ready".

    Policy:
    - stageA_ready is artifact-existence based (manifest + ASR outputs).
    - semantic_ready additionally requires chunks + embeddings existence AND StageB1 contract check OK.
      This prevents false positives where embeddings exist but are invalid.
    - editorial_ready requires semantic_ready + enrich + keyframes_dir existence.
    """
    ex_manifest = bool_get(rec, ["exists", "manifest", "exists"])
    ex_asr_json = bool_get(rec, ["exists", "asr_json", "exists"])
    ex_wav = bool_get(rec, ["exists", "wav", "exists"])
    ex_srt = bool_get(rec, ["exists", "srt", "exists"])
    ex_vtt = bool_get(rec, ["exists", "vtt", "exists"])
    ex_chunks = bool_get(rec, ["exists", "chunks_jsonl", "exists"])
    ex_embeddings = bool_get(rec, ["exists", "embeddings_json", "exists"])
    ex_enrich = bool_get(rec, ["exists", "enrich_json", "exists"])
    ex_keyframes = bool_get(rec, ["exists", "keyframes_dir", "exists"])

    stageA_ready = ex_manifest and ex_asr_json and ex_wav and ex_srt and ex_vtt
    if not stageA_ready:
        return "none"

    stageB1_ok = check_ok(rec, "stageB1_contract_check")
    semantic_ready = stageA_ready and ex_chunks and ex_embeddings and stageB1_ok
    if not semantic_ready:
        return "stageA_ready"

    editorial_ready = semantic_ready and ex_enrich and ex_keyframes
    if editorial_ready:
        return "editorial_ready"
    return "semantic_ready"


def load_index(index_path: Path) -> List[Dict[str, Any]]:
    if not index_path.exists():
        fail(f"index file does not exist: {index_path}")
    recs = list(iter_jsonl(index_path))
    # Basic sanity: ensure unique media_id
    seen: Set[str] = set()
    for r in recs:
        mid = str(r.get("media_id", "")).strip()
        if not mid:
            fail("index contains a record missing media_id")
        if mid in seen:
            fail(f"index contains duplicate media_id: {mid}")
        seen.add(mid)
    return recs


def build_summary_from_records(index_path: Path, recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    records = len(recs)
    present = {
        "manifest": 0,
        "asr_json": 0,
        "wav": 0,
        "srt": 0,
        "vtt": 0,
        "chunks": 0,
        "embeddings": 0,
        "enrich": 0,
        "keyframes_dir": 0,
    }
    checks = {
        "stageA": {"ok": 0, "fail": 0, "not_ran": 0},
        "stageB1": {"ok": 0, "fail": 0, "not_ran": 0},
    }
    tiers_count = {"stageA_ready": 0, "semantic_ready": 0, "editorial_ready": 0}
    tiers_mids: Dict[str, List[str]] = {"stageA_ready": [], "semantic_ready": [], "editorial_ready": []}

    for rec in recs:
        mid = str(rec.get("media_id", "")).strip()

        ex_manifest = bool_get(rec, ["exists", "manifest", "exists"])
        ex_asr_json = bool_get(rec, ["exists", "asr_json", "exists"])
        ex_wav = bool_get(rec, ["exists", "wav", "exists"])
        ex_srt = bool_get(rec, ["exists", "srt", "exists"])
        ex_vtt = bool_get(rec, ["exists", "vtt", "exists"])
        ex_chunks = bool_get(rec, ["exists", "chunks_jsonl", "exists"])
        ex_embeddings = bool_get(rec, ["exists", "embeddings_json", "exists"])
        ex_enrich = bool_get(rec, ["exists", "enrich_json", "exists"])
        ex_keyframes = bool_get(rec, ["exists", "keyframes_dir", "exists"])

        if ex_manifest:
            present["manifest"] += 1
        if ex_asr_json:
            present["asr_json"] += 1
        if ex_wav:
            present["wav"] += 1
        if ex_srt:
            present["srt"] += 1
        if ex_vtt:
            present["vtt"] += 1
        if ex_chunks:
            present["chunks"] += 1
        if ex_embeddings:
            present["embeddings"] += 1
        if ex_enrich:
            present["enrich"] += 1
        if ex_keyframes:
            present["keyframes_dir"] += 1

        # check counters
        for key, label in [("stageA_contract_check", "stageA"), ("stageB1_contract_check", "stageB1")]:
            chk = rec.get("checks", {}).get(key, {})
            ran = bool(chk.get("ran", False))
            ok = bool(chk.get("ok", False)) if ran else None
            if not ran:
                checks[label]["not_ran"] += 1
            elif ok:
                checks[label]["ok"] += 1
            else:
                checks[label]["fail"] += 1

        # tiers (must match tier_of)
        stageA_ready = ex_manifest and ex_asr_json and ex_wav and ex_srt and ex_vtt
        stageB1_ok = check_ok(rec, "stageB1_contract_check")
        semantic_ready = stageA_ready and ex_chunks and ex_embeddings and stageB1_ok
        editorial_ready = semantic_ready and ex_enrich and ex_keyframes

        if stageA_ready:
            tiers_count["stageA_ready"] += 1
            if mid:
                tiers_mids["stageA_ready"].append(mid)
        if semantic_ready:
            tiers_count["semantic_ready"] += 1
            if mid:
                tiers_mids["semantic_ready"].append(mid)
        if editorial_ready:
            tiers_count["editorial_ready"] += 1
            if mid:
                tiers_mids["editorial_ready"].append(mid)

    missing = {k: records - present[k] for k in present.keys()}

    return {
        "ok": True,
        "schema_version": 1,
        "index_path": str(index_path),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "records": records,
        "present": present,
        "missing": missing,
        "checks": checks,
        "tiers": {
            "counts": tiers_count,
            "media_ids": tiers_mids,
        },
    }


def cmd_summary(index_path: Path) -> None:
    recs = load_index(index_path)
    print(json.dumps(build_summary_from_records(index_path, recs), ensure_ascii=False, indent=2))


def cmd_list(index_path: Path, tier: str, source_contains: str) -> None:
    recs = load_index(index_path)
    out: List[str] = []

    allowed = {"none", "stageA_ready", "semantic_ready", "editorial_ready"}
    if tier not in allowed:
        fail(f"invalid tier '{tier}', must be one of {sorted(allowed)}")

    for rec in recs:
        mid = str(rec.get("media_id", "")).strip()
        sp = str(rec.get("source_path", "") or "")

        if source_contains and source_contains not in sp:
            continue

        t = tier_of(rec)
        if tier == "none":
            if t == "none":
                out.append(mid)
        else:
            if t == tier:
                out.append(mid)

    payload = {
        "ok": True,
        "schema_version": 1,
        "index_path": str(index_path),
        "tier": tier,
        "source_contains": source_contains or None,
        "count": len(out),
        "media_ids": out,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_explain(index_path: Path, mid: str) -> None:
    if not mid or len(mid) != 64:
        fail("explain requires --mid 64-hex media_id")
    recs = load_index(index_path)
    by_mid = {str(r.get("media_id")): r for r in recs}
    if mid not in by_mid:
        fail(f"media_id not found in index: {mid}", code=3)

    rec = by_mid[mid]
    payload = {
        "ok": True,
        "schema_version": 1,
        "index_path": str(index_path),
        "media_id": mid,
        "tier": tier_of(rec),
        "source_path": rec.get("source_path"),
        "paths": rec.get("paths", {}),
        "exists": rec.get("exists", {}),
        "checks": rec.get("checks", {}),
        "provenance": rec.get("provenance", {}),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_missing(index_path: Path, kind: str, source_contains: str) -> None:
    """
    List media_ids missing a specific artifact family.
    kind must match the record's exists keys.
    """
    recs = load_index(index_path)

    kinds = {
        "manifest": ["exists", "manifest", "exists"],
        "asr_json": ["exists", "asr_json", "exists"],
        "wav": ["exists", "wav", "exists"],
        "srt": ["exists", "srt", "exists"],
        "vtt": ["exists", "vtt", "exists"],
        "chunks": ["exists", "chunks_jsonl", "exists"],
        "embeddings": ["exists", "embeddings_json", "exists"],
        "enrich": ["exists", "enrich_json", "exists"],
        "keyframes_dir": ["exists", "keyframes_dir", "exists"],
    }
    if kind not in kinds:
        fail(f"invalid kind '{kind}', must be one of {sorted(kinds.keys())}")

    out: List[str] = []
    for rec in recs:
        mid = str(rec.get("media_id", "")).strip()
        sp = str(rec.get("source_path", "") or "")
        if source_contains and source_contains not in sp:
            continue
        if not bool_get(rec, kinds[kind], False):
            out.append(mid)

    payload = {
        "ok": True,
        "schema_version": 1,
        "index_path": str(index_path),
        "kind": kind,
        "source_contains": source_contains or None,
        "count": len(out),
        "media_ids": out,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_diff(index_a: Path, index_b: Path) -> None:
    """
    Diff two index files at a high level:
    - added/removed media_ids
    - tier changes
    - artifact existence changes (coarse)
    - check status changes (coarse)

    Output is JSON only.
    """
    a = load_index(index_a)
    b = load_index(index_b)

    a_map = {str(r["media_id"]): r for r in a}
    b_map = {str(r["media_id"]): r for r in b}

    a_mids = set(a_map.keys())
    b_mids = set(b_map.keys())

    added = sorted(list(b_mids - a_mids))
    removed = sorted(list(a_mids - b_mids))
    common = sorted(list(a_mids & b_mids))

    tier_changed: List[Dict[str, Any]] = []
    exists_changed: List[Dict[str, Any]] = []
    checks_changed: List[Dict[str, Any]] = []

    exists_keys = [
        ("manifest", ["exists", "manifest", "exists"]),
        ("asr_json", ["exists", "asr_json", "exists"]),
        ("wav", ["exists", "wav", "exists"]),
        ("srt", ["exists", "srt", "exists"]),
        ("vtt", ["exists", "vtt", "exists"]),
        ("chunks", ["exists", "chunks_jsonl", "exists"]),
        ("embeddings", ["exists", "embeddings_json", "exists"]),
        ("enrich", ["exists", "enrich_json", "exists"]),
        ("keyframes_dir", ["exists", "keyframes_dir", "exists"]),
    ]

    for mid in common:
        ra = a_map[mid]
        rb = b_map[mid]

        ta = tier_of(ra)
        tb = tier_of(rb)
        if ta != tb:
            tier_changed.append({"media_id": mid, "from": ta, "to": tb})

        changes = {}
        for name, path in exists_keys:
            va = bool_get(ra, path, False)
            vb = bool_get(rb, path, False)
            if va != vb:
                changes[name] = {"from": va, "to": vb}
        if changes:
            exists_changed.append({"media_id": mid, "changed": changes})

        # check changes (only ran/ok booleans)
        def chk_state(r: Dict[str, Any], key: str) -> Tuple[bool, bool]:
            c = r.get("checks", {}).get(key, {})
            ran = bool(c.get("ran", False))
            ok = bool(c.get("ok", False)) if ran else False
            return ran, ok

        for ck in ["stageA_contract_check", "stageB1_contract_check"]:
            ran_a, ok_a = chk_state(ra, ck)
            ran_b, ok_b = chk_state(rb, ck)
            if (ran_a, ok_a) != (ran_b, ok_b):
                checks_changed.append(
                    {"media_id": mid, "check": ck, "from": {"ran": ran_a, "ok": ok_a}, "to": {"ran": ran_b, "ok": ok_b}}
                )

    payload = {
        "ok": True,
        "schema_version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index_a": str(index_a),
        "index_b": str(index_b),
        "added_media_ids": added,
        "removed_media_ids": removed,
        "tier_changed": tier_changed,
        "exists_changed": exists_changed,
        "checks_changed": checks_changed,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="/workspace/catalog/audit/index_latest.jsonl", help="Audit index JSONL path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("summary", help="Emit JSON summary over the index")
    sp.add_argument("--index-path", default="", help="Override --index for this command")

    sp = sub.add_parser("list", help="List media_ids by tier")
    sp.add_argument("--tier", required=True, help="none|stageA_ready|semantic_ready|editorial_ready")
    sp.add_argument("--source-contains", default="", help="Filter by source_path substring")
    sp.add_argument("--index-path", default="", help="Override --index for this command")

    sp = sub.add_parser("explain", help="Explain a single media_id (paths/existence/checks/tier)")
    sp.add_argument("--mid", required=True, help="media_id (64-hex)")
    sp.add_argument("--index-path", default="", help="Override --index for this command")

    sp = sub.add_parser("missing", help="List media_ids missing a specific artifact family")
    sp.add_argument("--kind", required=True, help="manifest|asr_json|wav|srt|vtt|chunks|embeddings|enrich|keyframes_dir")
    sp.add_argument("--source-contains", default="", help="Filter by source_path substring")
    sp.add_argument("--index-path", default="", help="Override --index for this command")

    sp = sub.add_parser("diff", help="Diff two index files")
    sp.add_argument("--a", required=True, help="Path to index A")
    sp.add_argument("--b", required=True, help="Path to index B")

    args = ap.parse_args()

    default_index = Path(args.index)
    if args.cmd == "diff":
        cmd_diff(Path(args.a), Path(args.b))
        return

    index_path = default_index
    # command-level override
    override = getattr(args, "index_path", "")
    if override and str(override).strip():
        index_path = Path(str(override).strip())

    if args.cmd == "summary":
        cmd_summary(index_path)
    elif args.cmd == "list":
        cmd_list(index_path, tier=str(args.tier).strip(), source_contains=str(args.source_contains or ""))
    elif args.cmd == "explain":
        cmd_explain(index_path, mid=str(args.mid).strip())
    elif args.cmd == "missing":
        cmd_missing(index_path, kind=str(args.kind).strip(), source_contains=str(args.source_contains or ""))
    else:
        fail(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    main()
