#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            rows.append(json.loads(ln))
    return rows

def write_jsonl(path: str, rows: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def build_embedding_id(chunk_id: str, model_id: str, model_revision: str, embed_config_sha256: str) -> str:
    payload = {
        "chunk_id": chunk_id,
        "model_id": model_id,
        "model_revision": model_revision,
        "embed_config_sha256": embed_config_sha256,
    }
    return sha256_bytes(canonical_json_bytes(payload))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True, help="media_id (64 hex)")
    ap.add_argument("--chunks", required=True, help="Stage B.0 chunks JSONL")
    ap.add_argument("--out", required=True, help="Embeddings JSONL output")
    ap.add_argument("--model-id", default="sentence-transformers/all-mpnet-base-v2")
    ap.add_argument("--model-revision", default="", help="Strongly recommended: pin to commit hash")
    ap.add_argument("--hf-home", default="/workspace/.cache/huggingface")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--max-seq-length", type=int, default=384)
    ap.add_argument("--normalize", action="store_true", default=True)
    ap.add_argument("--device", default="", help="cpu|cuda|auto (default auto)")
    ap.add_argument("--venv-name", default="", help="recorded only (e.g. venv_embed_cpu)")
    args = ap.parse_args()

    # Cache dirs (avoid migrations and scattering)
    os.environ["HF_HOME"] = args.hf_home
    os.environ["HF_HUB_CACHE"] = os.path.join(args.hf_home, "hub")
    os.environ["TRANSFORMERS_CACHE"] = os.path.join(args.hf_home, "transformers")

    # Import heavy deps late
    from sentence_transformers import SentenceTransformer
    import torch

    device = args.device.strip().lower()
    if device in ("", "auto"):
        device = "cuda" if torch.cuda.is_available() else "cpu"

    # Embed config + hash
    embed_cfg = {
        "schema_version": 1,
        "task": "text_embedding",
        "pooling": "sentence-transformers-default",
        "normalize_embeddings": bool(args.normalize),
        "batch_size": int(args.batch_size),
        "max_seq_length": int(args.max_seq_length),
    }
    embed_cfg_sha = sha256_bytes(canonical_json_bytes(embed_cfg))

    # Load chunks
    chunks = load_jsonl(args.chunks)
    if not chunks:
        # Must still write an empty file
        write_jsonl(args.out, [])
        return

    # Load model
    # Note: SentenceTransformer supports passing a local path. For strict pinning,
    # you should provide a revision and pre-download via snapshot_download into a local dir.
    # For initial bring-up, we load by model_id directly and record model_revision as provided.
    model = SentenceTransformer(args.model_id, device=device)
    try:
        model.max_seq_length = int(args.max_seq_length)
    except Exception:
        pass

    texts = [c["text"] for c in chunks]
    # Safe batching: if OOM or runtime error occurs, caller can rerun with smaller batch_size.
    vecs = model.encode(
        texts,
        batch_size=int(args.batch_size),
        convert_to_numpy=True,
        normalize_embeddings=bool(args.normalize),
        show_progress_bar=False,
    )

    # Build outputs (1:1 with chunks)
    out_rows: List[Dict[str, Any]] = []
    for c, v in zip(chunks, vecs):
        chunk_id = c["chunk_id"]
        emb_id = build_embedding_id(chunk_id, args.model_id, args.model_revision, embed_cfg_sha)
        out_rows.append({
            "schema_version": 1,
            "media_id": args.mid,
            "chunk_id": chunk_id,
            "embedding_id": emb_id,
            "model_id": args.model_id,
            "model_revision": args.model_revision,
            "embed_config_sha256": embed_cfg_sha,
            "vector_dim": int(v.shape[0]),
            "vector": [float(x) for x in v.tolist()],
            "created_utc": utc_now(),
            "runtime": {
                "python": sys.version.split()[0],
                "torch": getattr(torch, "__version__", ""),
                "device": device,
                "venv": args.venv_name or "",
            }
        })

    write_jsonl(args.out, out_rows)

if __name__ == "__main__":
    main()
