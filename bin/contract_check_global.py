#!/usr/bin/env python3
import json
import os
import sys
import glob
import time
import random
import urllib.request

IDX = "/workspace/catalog/audit/index_latest.jsonl"
MILVUS_DB = "/workspace/catalog/milvus/moments.db"
MILVUS_COLL = "moments_asr_v1"
WEBROOT_HEALTH = "http://127.0.0.1:3000/api/health"

# Canonical embedding contract (must match Stage B1 artifacts)
CANON_MODEL_ID = "intfloat/e5-small-v2"
CANON_DIM = 384
CANON_PREFIX_QUERY = "query: "
CANON_NORMALIZE = True

def die(msg, rc=2):
    print("FATAL:", msg, file=sys.stderr)
    sys.exit(rc)

def warn(msg):
    print("WARN:", msg, file=sys.stderr)

def ok(msg):
    print("OK:", msg)

def load_json(path, ctx):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        die(f"failed to read JSON for {ctx} at {path}: {type(e).__name__}: {e}")

def iter_index():
    if not os.path.isfile(IDX):
        die(f"missing index_latest: {IDX}")
    with open(IDX, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)

def fetch_health():
    try:
        with urllib.request.urlopen(WEBROOT_HEALTH, timeout=3) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except Exception as e:
        die(f"failed to fetch {WEBROOT_HEALTH}: {type(e).__name__}: {e}")

def check_embeddings_vs_asr():
    total = 0
    have_emb = 0
    empty_blank = 0
    empty_no_audio = 0
    empty_other = 0
    emb_with_empty = []

    for rec in iter_index():
        total += 1
        ex = rec.get("exists", {})
        has_emb = ex.get("embeddings_json", {}).get("exists", False)
        if has_emb:
            have_emb += 1

        # classify ASR empty reasons
        if ex.get("asr_json", {}).get("exists", False):
            ap = rec.get("paths", {}).get("asr_json")
            if ap and os.path.exists(ap):
                a = load_json(ap, "asr_json")
                if a.get("empty_transcript") is True:
                    reason = a.get("reason")
                    if reason == "blank_audio":
                        empty_blank += 1
                    elif reason == "no_audio_stream":
                        empty_no_audio += 1
                    else:
                        empty_other += 1

                    if has_emb:
                        emb_with_empty.append((rec.get("media_id"), reason))

    expected = total - empty_blank - empty_no_audio - empty_other

    print("coverage:",
          json.dumps({
              "index_total": total,
              "have_embeddings": have_emb,
              "asr_empty_blank_audio": empty_blank,
              "asr_empty_no_audio_stream": empty_no_audio,
              "asr_empty_other": empty_other,
              "expected_embeddings_if_only_non_empty_ASR": expected
          }, indent=2))

    if have_emb != expected:
        die(f"embeddings coverage mismatch: have_embeddings={have_emb} expected={expected}")

    if emb_with_empty:
        sample = "\n".join([f"  {mid} reason={reason}" for mid, reason in emb_with_empty[:10]])
        die("found embeddings for empty_transcript ASR (should be skipped):\n" + sample)

    ok("embeddings vs ASR empty_transcript checks passed")

def check_embedding_provenance_sample():
    paths = sorted(glob.glob("/workspace/catalog/embeddings/*.json"))
    if not paths:
        die("no embeddings/*.json found")
    p = paths[0]
    obj = load_json(p, "embeddings_sample")

    model = obj.get("model", {})
    dim = model.get("dimension")
    model_id = model.get("model_id")
    norm = (obj.get("embed_config") or {}).get("normalize_embeddings")

    if model_id != CANON_MODEL_ID:
        die(f"embeddings model_id mismatch: {model_id} != {CANON_MODEL_ID}")
    if int(dim) != CANON_DIM:
        die(f"embeddings dim mismatch: {dim} != {CANON_DIM}")
    if bool(norm) != bool(CANON_NORMALIZE):
        die(f"embeddings normalize_embeddings mismatch: {norm} != {CANON_NORMALIZE}")

    ok(f"embeddings sample provenance OK ({os.path.basename(p)})")

def check_service_embedder_contract():
    h = fetch_health()
    emb = h.get("embedder") or {}
    if not emb.get("ready"):
        die("service embedder not ready")

    # These fields are exposed by your patched webroot_server.py
    if emb.get("model_id") != CANON_MODEL_ID:
        die(f"service embedder model_id mismatch: {emb.get('model_id')} != {CANON_MODEL_ID}")
    if emb.get("query_prefix") != CANON_PREFIX_QUERY:
        die(f"service embedder query_prefix mismatch: {emb.get('query_prefix')} != {CANON_PREFIX_QUERY}")
    if bool(emb.get("normalize")) != bool(CANON_NORMALIZE):
        die(f"service embedder normalize mismatch: {emb.get('normalize')} != {CANON_NORMALIZE}")

    # Optional sanity: onnx paths exist
    mp = emb.get("onnx_model_path")
    tp = emb.get("tokenizer_path")
    if not (mp and os.path.exists(mp)):
        die(f"service onnx_model_path missing: {mp}")
    if not (tp and os.path.exists(tp)):
        die(f"service tokenizer_path missing: {tp}")

    ok("service embedder contract matches canonical embedding space")

def check_milvus_ready_and_indexed():
    # health says ready, but we also want to ensure search won't 700 due to missing vector index
    h = fetch_health()
    m = h.get("milvus") or {}
    if not m.get("ready"):
        die(f"milvus not ready per health: {m}")

    try:
        from pymilvus import MilvusClient
    except Exception as e:
        die(f"cannot import pymilvus in this env: {type(e).__name__}: {e}")

    client = MilvusClient(MILVUS_DB)
    if not client.has_collection(MILVUS_COLL):
        die(f"milvus collection missing: {MILVUS_COLL}")

    # Attempt a tiny search to confirm index exists and schema matches "vector" field.
    # Use a deterministic pseudo-random vector.
    rnd = random.Random(0)
    qv = [rnd.uniform(-0.01, 0.01) for _ in range(CANON_DIM)]
    try:
        _ = client.search(
            collection_name=MILVUS_COLL,
            data=[qv],
            anns_field="vector",
            limit=1,
            output_fields=["media_id", "start_s", "end_s", "text"],
        )
    except Exception as e:
        die(f"milvus search sanity failed: {type(e).__name__}: {e}")

    ok("milvus search sanity OK (vector index present, search callable)")

def main():
    t0 = time.time()
    check_embedding_provenance_sample()
    check_embeddings_vs_asr()
    check_service_embedder_contract()
    check_milvus_ready_and_indexed()
    ok(f"contract_check_global passed in {round(time.time()-t0,2)}s")

if __name__ == "__main__":
    main()
