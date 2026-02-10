#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from typing import Any, Dict, List, Tuple

DEFAULT_CONFIG = {
    "schema_version": 1,
    "target_chunk_s": 20.0,
    "max_chunk_s": 30.0,
    "silence_gap_s": 1.5,
    "max_chars": 800,
    "overlap_s": 0.0,  # disabled in v1
}

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def canonical_json_bytes(obj: Any) -> bytes:
    # Canonical: sorted keys, compact separators, UTF-8
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def load_json_bytes(path: str) -> Tuple[bytes, Dict[str, Any]]:
    with open(path, "rb") as f:
        b = f.read()
    return b, json.loads(b.decode("utf-8"))

def join_text(texts: List[str]) -> str:
    # Deterministic whitespace normalization: strip each segment and join with single spaces.
    parts = [t.strip() for t in texts if t is not None]
    return " ".join([p for p in parts if p != ""]).strip()

def build_chunk_id(payload: Dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(payload))

def build_outcome_record_empty(asr: Dict[str, Any], asr_sha: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Explicit outcome record when no chunks can be produced.
    """
    cfg_sha = sha256_bytes(canonical_json_bytes(config))

    upstream_quality = asr.get("quality") or {}
    upstream_reasons = upstream_quality.get("reasons") or []
    upstream_status = upstream_quality.get("status") or ""

    reason = "ASR_SEGMENTS_EMPTY"

    return {
        "schema_version": 1,
        "record_type": "chunking_outcome",
        "reason": reason,
        "media_id": asr.get("media_id"),
        "asr_schema_version": asr.get("schema_version"),
        "asr_artifact_sha256": asr_sha,
        "chunking_config_sha256": cfg_sha,
        "policy": asr.get("policy") if isinstance(asr.get("policy"), dict) else None,
        "quality": {
            "status": "fail" if upstream_status == "fail" else "warn",
            "reasons": [reason],
            "stats": {
                "asr_segments": int(len(asr.get("segments") or [])),
                "asr_empty_transcript": bool(asr.get("empty_transcript", False)),
                "asr_reason": str(asr.get("reason") or ""),
                "asr_quality_status": str(upstream_status),
                "asr_quality_reasons": list(upstream_reasons) if isinstance(upstream_reasons, list) else [],
            },
        },
        "eligibility": {
            "eligible_for_embedding": False,
            "eligible_for_indexing": False,
            "reason": reason,
        },
    }

def build_outcome_record_success(asr: Dict[str, Any], asr_sha: str, config: Dict[str, Any], chunk_count: int) -> Dict[str, Any]:
    """
    Explicit outcome record for the normal case (chunks produced).
    Contract: MUST be the final record in chunks JSONL when ASR segments are non-empty.
    """
    cfg_sha = sha256_bytes(canonical_json_bytes(config))

    upstream_quality = asr.get("quality") or {}
    upstream_reasons = upstream_quality.get("reasons") or []
    upstream_status = upstream_quality.get("status") or ""

    reason = "OK"

    return {
        "schema_version": 1,
        "record_type": "chunking_outcome",
        "reason": reason,
        "media_id": asr.get("media_id"),
        "asr_schema_version": asr.get("schema_version"),
        "asr_artifact_sha256": asr_sha,
        "chunking_config_sha256": cfg_sha,
        "policy": asr.get("policy") if isinstance(asr.get("policy"), dict) else None,
        "quality": {
            "status": "ok" if upstream_status != "fail" else "warn",
            "reasons": [],
            "stats": {
                "asr_segments": int(len(asr.get("segments") or [])),
                "asr_empty_transcript": bool(asr.get("empty_transcript", False)),
                "asr_reason": str(asr.get("reason") or ""),
                "asr_quality_status": str(upstream_status),
                "asr_quality_reasons": list(upstream_reasons) if isinstance(upstream_reasons, list) else [],
                "chunk_count": int(chunk_count),
            },
        },
        "eligibility": {
            "eligible_for_embedding": True,
            "eligible_for_indexing": True,
            "reason": reason,
        },
    }

def emit_chunks(asr: Dict[str, Any], asr_sha: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    segs = asr.get("segments", [])
    if not segs:
        return []

    max_chunk_s = float(config["max_chunk_s"])
    silence_gap_s = float(config["silence_gap_s"])
    max_chars = int(config["max_chars"])

    chunks: List[Dict[str, Any]] = []

    cur_indices: List[int] = []
    cur_texts: List[str] = []
    cur_start: float = None  # type: ignore
    cur_end: float = None    # type: ignore

    cfg_sha = sha256_bytes(canonical_json_bytes(config))

    def flush():
        nonlocal cur_indices, cur_texts, cur_start, cur_end
        txt = join_text(cur_texts)
        if txt == "":
            # Do not emit empty chunks; this would violate contract.
            cur_indices, cur_texts, cur_start, cur_end = [], [], None, None
            return
        payload = {
            "media_id": asr["media_id"],
            "asr_artifact_sha256": asr_sha,
            "chunk_schema_version": 1,
            "chunking_config_sha256": cfg_sha,
            "start_s": cur_start,
            "end_s": cur_end,
            "source_segments": cur_indices,
        }
        chunk = {
            "schema_version": 1,
            "record_type": "chunk",
            "media_id": asr["media_id"],
            "chunk_id": build_chunk_id(payload),
            "asr_schema_version": asr["schema_version"],
            "asr_artifact_sha256": asr_sha,
            "chunking_config_sha256": cfg_sha,
            "start_s": cur_start,
            "end_s": cur_end,
            "text": txt,
            "source_segments": cur_indices,
        }

        if isinstance(asr.get("policy"), dict):
            chunk["policy"] = asr["policy"]

        chunks.append(chunk)
        cur_indices, cur_texts, cur_start, cur_end = [], [], None, None

    for i, s in enumerate(segs):
        s_start = float(s["start"])
        s_end = float(s["end"])
        s_text = str(s["text"])

        if cur_indices:
            prev_end = cur_end
            gap = s_start - float(prev_end)
            if gap >= silence_gap_s:
                flush()

        if not cur_indices:
            cur_indices = [i]
            cur_texts = [s_text]
            cur_start = s_start
            cur_end = s_end
            continue

        proposed_start = float(cur_start)
        proposed_end = s_end
        proposed_dur = proposed_end - proposed_start
        proposed_text = join_text(cur_texts + [s_text])
        proposed_len = len(proposed_text)

        exceeds_dur = proposed_dur > max_chunk_s
        exceeds_len = proposed_len > max_chars

        if exceeds_dur or exceeds_len:
            flush()
            cur_indices = [i]
            cur_texts = [s_text]
            cur_start = s_start
            cur_end = s_end
            continue

        cur_indices.append(i)
        cur_texts.append(s_text)
        cur_end = s_end

    if cur_indices:
        flush()

    seen = []
    for c in chunks:
        if c.get("record_type") == "chunk":
            seen.extend(c["source_segments"])
    if sorted(seen) != list(range(len(segs))):
        raise RuntimeError(f"coverage invariant failed: expected [0..{len(segs)-1}], got {sorted(seen)[:20]}... total={len(seen)}")

    return chunks

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="asr_json", required=True, help="Input ASR JSON (schema v2)")
    ap.add_argument("--out", dest="out_jsonl", required=True, help="Output chunk JSONL")
    ap.add_argument("--config-json", dest="config_json", default="", help="Optional config override JSON string")
    args = ap.parse_args()

    asr_bytes, asr = load_json_bytes(args.asr_json)
    asr_sha = sha256_bytes(asr_bytes)

    if asr.get("schema_version") != 2:
        raise SystemExit(f"FATAL: ASR schema_version must be 2, got {asr.get('schema_version')}")

    config = dict(DEFAULT_CONFIG)
    if args.config_json.strip():
        override = json.loads(args.config_json)
        config.update(override)

    chunks = emit_chunks(asr, asr_sha, config)

    os.makedirs(os.path.dirname(args.out_jsonl), exist_ok=True)

    tmp_path = args.out_jsonl + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        if not chunks:
            outcome = build_outcome_record_empty(asr, asr_sha, config)
            f.write(json.dumps(outcome, ensure_ascii=False) + "\n")
        else:
            for c in chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
            outcome = build_outcome_record_success(asr, asr_sha, config, chunk_count=len(chunks))
            f.write(json.dumps(outcome, ensure_ascii=False) + "\n")

    os.replace(tmp_path, args.out_jsonl)

if __name__ == "__main__":
    main()