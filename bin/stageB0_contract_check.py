#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def load_json_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()

def fatal(msg: str):
    print("FATAL:", msg, file=sys.stderr)
    raise SystemExit(2)

def parse_jsonl_nonblank(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception as e:
                fatal(f"invalid JSONL line: {repr(e)}")
    return out

def split_chunk_records_and_outcome(objs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    chunk_records: List[Dict[str, Any]] = []
    outcome: Optional[Dict[str, Any]] = None
    for o in objs:
        rt = o.get("record_type")
        if rt == "chunking_outcome":
            # allow only one, must be last logically (we enforce after)
            outcome = o
        else:
            chunk_records.append(o)
    # If outcome exists, require it to be the final nonblank line in file
    if outcome is not None and objs and objs[-1].get("record_type") != "chunking_outcome":
        fatal("chunking_outcome must be the final JSONL record")
    return chunk_records, outcome

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asr", required=True, help="Input ASR JSON (schema v2)")
    ap.add_argument("--chunks", required=True, help="Chunk JSONL output")
    ap.add_argument("--config-json", default="", help="Config JSON string used for chunking")
    args = ap.parse_args()

    asr_bytes = load_json_bytes(args.asr)
    asr_sha = sha256_bytes(asr_bytes)
    asr = json.loads(asr_bytes.decode("utf-8"))

    if asr.get("schema_version") != 2:
        fatal(f"ASR schema_version must be 2, got {asr.get('schema_version')}")

    media_id = asr.get("media_id")
    if not isinstance(media_id, str) or len(media_id) != 64:
        fatal("ASR media_id invalid")

    segs = asr.get("segments", [])
    if not isinstance(segs, list):
        fatal("ASR segments must be list")

    # Config must match transformer default if empty
    default_cfg = {
        "schema_version": 1,
        "target_chunk_s": 20.0,
        "max_chunk_s": 30.0,
        "silence_gap_s": 1.5,
        "max_chars": 800,
        "overlap_s": 0.0,
    }
    cfg = dict(default_cfg)
    if args.config_json.strip():
        cfg.update(json.loads(args.config_json))
    cfg_sha = sha256_bytes(canonical_json_bytes(cfg))

    if not os.path.exists(args.chunks):
        fatal(f"missing chunks file: {args.chunks}")

    objs = parse_jsonl_nonblank(args.chunks)
    chunk_recs, outcome = split_chunk_records_and_outcome(objs)

    # If there are 0 ASR segments, we allow an outcome-only file (explicit outcome),
    # and we require that there are 0 chunk records.
    if len(segs) == 0:
        if chunk_recs:
            fatal("expected 0 chunk records for empty transcript")
        if outcome is None:
            fatal("empty transcript requires a final chunking_outcome record (explicit outcome)")
        # Minimal structural checks on outcome
        if outcome.get("schema_version") != 1 or outcome.get("record_type") != "chunking_outcome":
            fatal("chunking_outcome schema_version/record_type invalid")
        if outcome.get("media_id") != media_id:
            fatal("chunking_outcome media_id mismatch")
        if outcome.get("asr_schema_version") != 2:
            fatal("chunking_outcome asr_schema_version mismatch")
        if outcome.get("asr_artifact_sha256") != asr_sha:
            fatal("chunking_outcome asr_artifact_sha256 mismatch")
        if outcome.get("chunking_config_sha256") != cfg_sha:
            fatal("chunking_outcome chunking_config_sha256 mismatch")
        elig = outcome.get("eligibility") or {}
        if isinstance(elig, dict):
            # For empty transcript, embedding must be ineligible
            if elig.get("eligible_for_embedding") not in (False, 0):
                fatal("empty transcript outcome must set eligibility.eligible_for_embedding=false")
        print("OK: stageB0_contract_check passed (empty transcript outcome-only)")
        return

    # Non-empty ASR segments must produce >=1 chunk records and an outcome record.
    if not chunk_recs:
        fatal("non-empty ASR segments but chunks file has 0 chunk records")
    if outcome is None:
        fatal("non-empty ASR segments requires a final chunking_outcome record")

    all_indices: List[int] = []
    last_end = -1.0

    for c in chunk_recs:
        # required fields
        for k in [
            "schema_version","media_id","chunk_id","asr_schema_version","asr_artifact_sha256",
            "chunking_config_sha256","start_s","end_s","text","source_segments"
        ]:
            if k not in c:
                fatal(f"missing field {k}")

        if c["schema_version"] != 1:
            fatal(f"chunk schema_version must be 1, got {c['schema_version']}")
        if c["media_id"] != media_id:
            fatal("chunk media_id mismatch")
        if c["asr_schema_version"] != 2:
            fatal("chunk asr_schema_version mismatch")
        if c["asr_artifact_sha256"] != asr_sha:
            fatal("chunk asr_artifact_sha256 mismatch")
        if c["chunking_config_sha256"] != cfg_sha:
            fatal("chunk chunking_config_sha256 mismatch")

        start_s = float(c["start_s"])
        end_s = float(c["end_s"])
        if end_s < start_s:
            fatal("chunk end_s < start_s")
        if start_s < 0:
            fatal("chunk start_s < 0")
        if end_s < last_end:
            fatal("chunks not monotonic by end_s")
        last_end = end_s

        txt = str(c["text"]).strip()
        if txt == "":
            fatal("empty chunk text")
        if "[BLANK_AUDIO]" in txt:
            fatal("chunk text contains [BLANK_AUDIO]")

        idxs = c["source_segments"]
        if not isinstance(idxs, list) or not idxs:
            fatal("source_segments must be non-empty list")
        if any((not isinstance(i,int) or i < 0) for i in idxs):
            fatal("invalid source_segments values")
        if idxs != sorted(idxs):
            fatal("source_segments not sorted")
        all_indices.extend(idxs)

        payload = {
            "media_id": media_id,
            "asr_artifact_sha256": asr_sha,
            "chunk_schema_version": 1,
            "chunking_config_sha256": cfg_sha,
            "start_s": start_s,
            "end_s": end_s,
            "source_segments": idxs,
        }
        expected_chunk_id = sha256_bytes(canonical_json_bytes(payload))
        if c["chunk_id"] != expected_chunk_id:
            fatal("chunk_id mismatch (hash does not recompute)")

    expected = list(range(len(segs)))
    if sorted(all_indices) != expected:
        fatal(f"coverage mismatch: expected {len(expected)} indices, got {len(all_indices)}")

    # Outcome must match identities too
    if outcome.get("schema_version") != 1 or outcome.get("record_type") != "chunking_outcome":
        fatal("chunking_outcome schema_version/record_type invalid")
    if outcome.get("media_id") != media_id:
        fatal("chunking_outcome media_id mismatch")
    if outcome.get("asr_schema_version") != 2:
        fatal("chunking_outcome asr_schema_version mismatch")
    if outcome.get("asr_artifact_sha256") != asr_sha:
        fatal("chunking_outcome asr_artifact_sha256 mismatch")
    if outcome.get("chunking_config_sha256") != cfg_sha:
        fatal("chunking_outcome chunking_config_sha256 mismatch")

    print("OK: stageB0_contract_check passed")

if __name__ == "__main__":
    main()