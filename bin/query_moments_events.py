#!/usr/bin/env python3
import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional

MILVUS_DB = "/workspace/catalog/milvus/moments.db"
EVENTS_DIR = "/workspace/catalog/events"
CHUNKS_DIR = "/workspace/catalog/chunks"

COLL_EVENTS = "moments_events_v1"

# Canonical embedder artifacts
ONNX_MODEL_PATH = "/workspace/models/onnx/e5-small-v2/model.onnx"
TOKENIZER_PATH = "/workspace/models/onnx/e5-small-v2/tokenizer.json"

CANON_DIM = 384
CANON_MAX_LEN = 256
CANON_QUERY_PREFIX = "query: "


def die(msg: str, rc: int = 2) -> None:
    print("FATAL:", msg, file=sys.stderr)
    sys.exit(rc)


def load_jsonl(path: str, ctx: str) -> List[Dict[str, Any]]:
    if not os.path.isfile(path):
        die(f"missing {ctx}: {path}")
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, start=1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                die(f"invalid json in {ctx} at {path}:{ln}: {type(e).__name__}: {e}")
            if not isinstance(obj, dict):
                die(f"non-object json in {ctx} at {path}:{ln}")
            rows.append(obj)
    if not rows:
        die(f"{ctx} has no records (blank/empty forbidden): {path}")
    return rows


def find_event_in_events_jsonl(events_path: str, event_id: str) -> Optional[Dict[str, Any]]:
    rows = load_jsonl(events_path, "events_jsonl")
    for r in rows:
        if r.get("record_type") == "event_record" and r.get("event_id") == event_id:
            return r
    return None


def build_chunk_map(chunks_path: str) -> Dict[str, Dict[str, Any]]:
    rows = load_jsonl(chunks_path, "chunks_jsonl")
    m: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r.get("record_type") != "chunk":
            continue
        cid = r.get("chunk_id")
        if isinstance(cid, str) and cid:
            m[cid] = r
    return m


def canonical_hydrate_event(event_hit: Dict[str, Any], hydrate_chunks: bool = False) -> Dict[str, Any]:
    eid = event_hit.get("pk")
    mid = event_hit.get("media_id")
    if not isinstance(eid, str) or not eid:
        return {"hydrate_ok": False, "reason": "missing_event_id"}
    if not isinstance(mid, str) or not mid:
        return {"hydrate_ok": False, "reason": "missing_media_id"}

    events_path = f"{EVENTS_DIR}/{mid}.jsonl"
    ev = None
    if os.path.isfile(events_path):
        ev = find_event_in_events_jsonl(events_path, eid)

    out: Dict[str, Any] = {
        "hydrate_ok": bool(ev is not None),
        "event_id": eid,
        "media_id": mid,
        "events_path": events_path,
        "event_record": ev,
    }

    if not ev:
        out["reason"] = "event_record_not_found"
        return out

    if hydrate_chunks:
        evidence = ev.get("evidence") or {}
        chunk_ids = None
        if isinstance(evidence, dict):
            chunk_ids = evidence.get("chunk_ids")
        if not isinstance(chunk_ids, list):
            out["chunks_hydrated"] = False
            out["chunks_reason"] = "missing_evidence_chunk_ids"
            return out

        chunks_path = f"{CHUNKS_DIR}/{mid}.jsonl"
        if not os.path.isfile(chunks_path):
            out["chunks_hydrated"] = False
            out["chunks_reason"] = "missing_chunks_file"
            out["chunks_path"] = chunks_path
            return out

        cmap = build_chunk_map(chunks_path)
        hydrated: List[Dict[str, Any]] = []
        missing: List[str] = []
        for cid in chunk_ids:
            if not isinstance(cid, str):
                continue
            ch = cmap.get(cid)
            if ch is None:
                missing.append(cid)
                continue
            hydrated.append(
                {
                    "chunk_id": cid,
                    "start_s": ch.get("start_s"),
                    "end_s": ch.get("end_s"),
                    "text": ch.get("text"),
                }
            )
        out["chunks_hydrated"] = True
        out["chunks_path"] = chunks_path
        out["chunks"] = hydrated
        out["chunks_missing"] = missing

    return out


def sanitize_hit(h: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {}
    if hasattr(h, "distance"):
        item["distance"] = getattr(h, "distance")
    if hasattr(h, "id"):
        item["id"] = getattr(h, "id")
    fields = None
    if hasattr(h, "fields"):
        fields = getattr(h, "fields")
    if isinstance(fields, dict):
        for k in ["pk", "media_id", "start_s", "end_s", "text", "t0_ms", "t1_ms"]:
            if k in fields:
                item[k] = fields.get(k)
    return item


def l2_normalize(v: List[float]) -> List[float]:
    s = 0.0
    for x in v:
        s += float(x) * float(x)
    n = math.sqrt(s)
    if n == 0.0:
        return v
    return [float(x) / n for x in v]


def mean_pool(last_hidden: List[List[float]], attention_mask: List[int]) -> List[float]:
    # last_hidden: [seq, dim], attention_mask: [seq]
    dim = len(last_hidden[0]) if last_hidden else 0
    acc = [0.0] * dim
    count = 0.0
    for i, m in enumerate(attention_mask):
        if int(m) == 0:
            continue
        row = last_hidden[i]
        for j in range(dim):
            acc[j] += float(row[j])
        count += 1.0
    if count == 0.0:
        return acc
    inv = 1.0 / count
    return [x * inv for x in acc]


def embed_query_onnx(text: str) -> List[float]:
    # imports inside to keep startup fast and errors crisp
    try:
        import numpy as np
        import onnxruntime as ort
    except Exception as e:
        die(f"missing numpy/onnxruntime in this env: {type(e).__name__}: {e}")

    try:
        from tokenizers import Tokenizer
    except Exception as e:
        die(f"missing tokenizers in this env: {type(e).__name__}: {e}")

    if not os.path.isfile(ONNX_MODEL_PATH):
        die(f"missing onnx model: {ONNX_MODEL_PATH}")
    if not os.path.isfile(TOKENIZER_PATH):
        die(f"missing tokenizer: {TOKENIZER_PATH}")

    tok = Tokenizer.from_file(TOKENIZER_PATH)

    q = CANON_QUERY_PREFIX + text
    enc = tok.encode(q)

    input_ids = enc.ids[:CANON_MAX_LEN]
    attn = enc.attention_mask[:CANON_MAX_LEN]

    # pad to max len
    pad_id = tok.token_to_id("[PAD]")
    if pad_id is None:
        # fallback: assume 0 is PAD, but still deterministic
        pad_id = 0

    while len(input_ids) < CANON_MAX_LEN:
        input_ids.append(int(pad_id))
    while len(attn) < CANON_MAX_LEN:
        attn.append(0)

    token_type_ids = [0] * CANON_MAX_LEN

    input_ids_np = np.array([input_ids], dtype=np.int64)
    attn_np = np.array([attn], dtype=np.int64)
    tt_np = np.array([token_type_ids], dtype=np.int64)

    # keep ORT threading conservative
    so = ort.SessionOptions()
    so.intra_op_num_threads = 1
    so.inter_op_num_threads = 1

    sess = ort.InferenceSession(ONNX_MODEL_PATH, sess_options=so, providers=["CPUExecutionProvider"])

    outs = sess.run(None, {
        "input_ids": input_ids_np,
        "attention_mask": attn_np,
        "token_type_ids": tt_np,
    })

    # E5 ONNX export typically returns last_hidden_state as first output: [1, seq, dim]
    last_hidden = outs[0]
    if last_hidden is None or len(last_hidden.shape) != 3:
        die(f"unexpected onnx output shape: {getattr(last_hidden, 'shape', None)}")

    seq = last_hidden.shape[1]
    dim = last_hidden.shape[2]
    if int(dim) != CANON_DIM:
        die(f"unexpected embedding dim: {dim} != {CANON_DIM}")

    lh = last_hidden[0].tolist()  # [seq, dim]
    pooled = mean_pool(lh[:seq], attn[:seq])
    v = l2_normalize(pooled)
    if len(v) != CANON_DIM:
        die(f"embedding length mismatch: {len(v)} != {CANON_DIM}")
    return v


def main() -> None:
    ap = argparse.ArgumentParser(description="Query moments_events_v1 and deterministically hydrate from canonical events.jsonl")
    ap.add_argument("--q", required=True, help="natural language query string")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--db", default=MILVUS_DB)
    ap.add_argument("--hydrate", action="store_true", help="hydrate canonical event_record from /workspace/catalog/events/<mid>.jsonl")
    ap.add_argument("--hydrate_chunks", action="store_true", help="also hydrate chunk evidence (implies --hydrate)")
    args = ap.parse_args()

    if args.hydrate_chunks:
        args.hydrate = True

    try:
        from pymilvus import MilvusClient
    except Exception as e:
        die(f"cannot import pymilvus: {type(e).__name__}: {e}")

    db = args.db
    if not os.path.isfile(db):
        die(f"milvus db missing: {db}")

    client = MilvusClient(db)
    if not client.has_collection(COLL_EVENTS):
        die(f"missing collection: {COLL_EVENTS}")

    qv = embed_query_onnx(args.q)

    res = client.search(
        collection_name=COLL_EVENTS,
        data=[qv],
        anns_field="vector",
        limit=int(args.topk),
        output_fields=["pk", "media_id", "start_s", "end_s", "text", "t0_ms", "t1_ms"],
    )

    hits_out: List[Dict[str, Any]] = []
    if isinstance(res, list) and res and isinstance(res[0], list):
        for h in res[0]:
            item = sanitize_hit(h)
            if "pk" not in item and "id" in item:
                item["pk"] = item.get("id")
            if args.hydrate:
                item["hydration"] = canonical_hydrate_event(item, hydrate_chunks=args.hydrate_chunks)
            hits_out.append(item)

    print(json.dumps(
        {
            "collection": COLL_EVENTS,
            "db": db,
            "q": args.q,
            "topk": int(args.topk),
            "hits": hits_out,
        },
        indent=2,
        sort_keys=True
    ))


if __name__ == "__main__":
    main()