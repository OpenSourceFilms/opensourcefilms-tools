#!/usr/bin/env python3
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import platform
import sys
from typing import Any, Dict, List, Optional


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return sha256_bytes(f.read())


def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def detect_venv_label(cli_venv: str) -> str:
    """
    This script requires an explicit --venv label.
    We still keep a defensive fallback for completeness, but under normal
    operation cli_venv will always be non-empty.

    IMPORTANT:
    Do NOT use os.path.realpath(sys.executable). Many venv python binaries are
    symlinks to the base interpreter, and realpath() destroys the venv identity.
    """
    if cli_venv.strip():
        return cli_venv.strip()

    venv_env = os.environ.get("VIRTUAL_ENV", "")
    if venv_env and venv_env.strip():
        return venv_env.strip()

    exe = sys.executable  # keep as invoked path
    exe_dir = os.path.dirname(exe)

    # Typical venv layout: <venv>/bin/python*
    if exe_dir.endswith("/bin"):
        venv_root = os.path.dirname(exe_dir)
        # Confirm if possible
        if os.path.isfile(os.path.join(venv_root, "pyvenv.cfg")):
            return venv_root
        return venv_root

    # Fallback: adjacent pyvenv.cfg (unusual)
    if os.path.isfile(os.path.join(exe_dir, "pyvenv.cfg")):
        return exe_dir

    return "unknown"


def compute_st_cache_snapshot_sha256(model_id: str) -> Optional[str]:
    """
    SentenceTransformers frequently stores models in:
      /root/.cache/torch/sentence_transformers/<model_id normalized>/
    For this pipeline, we treat a hash of key files in that directory as the
    "model_revision" when HF commit hash is not readily accessible.
    """
    norm = model_id.replace("/", "_")
    base = f"/root/.cache/torch/sentence_transformers/{norm}"
    if not os.path.isdir(base):
        return None

    patterns = [
        "**/modules.json",
        "**/config.json",
        "**/sentence_bert_config.json",
        "**/tokenizer.json",
        "**/vocab.txt",
        "**/model.safetensors",
        "**/pytorch_model.bin",
    ]
    candidates: List[str] = []
    for pat in patterns:
        candidates += glob.glob(os.path.join(base, pat), recursive=True)

    candidates = sorted(set([p for p in candidates if os.path.isfile(p)]))
    if not candidates:
        return None

    h = hashlib.sha256()
    for p in candidates:
        h.update(p.encode("utf-8") + b"\0")
        with open(p, "rb") as f:
            while True:
                b = f.read(1024 * 1024)
                if not b:
                    break
                h.update(b)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True, help="media_id to embed")
    ap.add_argument(
        "--venv",
        required=True,
        help="REQUIRED: venv label to record for provenance (e.g. /workspace/pipeline/venv_embed_cpu)",
    )
    ap.add_argument("--device", default="cpu", help="device for sentence-transformers (cpu/cuda)")
    ap.add_argument("--batch_size", type=int, default=32)
    ap.add_argument("--max_seq_length", type=int, default=256)
    ap.add_argument("--normalize", action="store_true", default=True)
    ap.add_argument("--no-normalize", dest="normalize", action="store_false")
    ap.add_argument("--model_id", default="intfloat/e5-small-v2")
    ap.add_argument(
        "--model_revision",
        default="",
        help="Optional explicit revision label (HF commit hash/tag). If empty, compute a local snapshot hash once cached.",
    )
    args = ap.parse_args()

    mid = args.mid
    chunks_path = f"/workspace/catalog/chunks/{mid}.jsonl"
    if not os.path.exists(chunks_path):
        print(f"Missing chunks: {chunks_path}", file=sys.stderr)
        raise SystemExit(2)

    # Import versions from current interpreter environment
    import torch
    import sentence_transformers
    import transformers
    import huggingface_hub
    from sentence_transformers import SentenceTransformer

    # Load model (will download into cache if needed)
    model = SentenceTransformer(args.model_id, device=args.device)
    model.max_seq_length = args.max_seq_length

    # Resolve a non-empty revision label
    model_revision = args.model_revision.strip()
    if not model_revision:
        snap = compute_st_cache_snapshot_sha256(args.model_id)
        if snap:
            model_revision = snap
        else:
            # Never blank, but clearly marked
            model_revision = "unresolved"

    # Determine dimension
    test_vec = model.encode(["passage: test"], normalize_embeddings=args.normalize, show_progress_bar=False)
    dim = int(test_vec.shape[1])

    prefix_passage = "passage: "
    prefix_query = "query: "

    embed_config = {
        "normalize_embeddings": bool(args.normalize),
        "max_seq_length": int(args.max_seq_length),
        "pooling": "mean_attention",
        "prefix_passage": prefix_passage,
        "prefix_query": prefix_query,
        "batch_size": int(args.batch_size),
    }
    cfg_dump = json.dumps(embed_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    cfg_sha = sha256_bytes(cfg_dump)

    # Read chunk lines; tolerate non-chunk records (e.g. outcome/sentinel lines)
    raw_lines = [ln for ln in open(chunks_path, "r", encoding="utf-8").read().splitlines() if ln.strip()]
    parsed: List[Dict[str, Any]] = []
    for ln in raw_lines:
        try:
            obj = json.loads(ln)
        except Exception:
            # If chunks file is corrupted, fail loudly as a tooling error.
            print("ERROR: chunks jsonl contains non-json line", file=sys.stderr)
            raise SystemExit(2)
        parsed.append(obj)

    # Keep only actual chunk records (must have chunk_id + text)
    chunks = [o for o in parsed if isinstance(o, dict) and "chunk_id" in o and "text" in o]
    vectors: List[Dict[str, Any]] = []
    skipped_vectors: List[Dict[str, Any]] = []

    if chunks:
        texts = [c.get("text", "") for c in chunks]
        embed_texts = [prefix_passage + t for t in texts]

        embs = model.encode(
            embed_texts,
            batch_size=args.batch_size,
            normalize_embeddings=args.normalize,
            show_progress_bar=False,
        )
        embs_list = embs.tolist()

        for c, et, vec in zip(chunks, embed_texts, embs_list):
            cid = c["chunk_id"]
            material = (cid + "|" + args.model_id + "|" + model_revision + "|" + cfg_sha).encode("utf-8")
            embedding_id = sha256_bytes(material)
            vectors.append(
                {
                    "chunk_id": cid,
                    "embedding_id": embedding_id,
                    "start_s": c.get("start_s"),
                    "end_s": c.get("end_s"),
                    "text": c.get("text", ""),
                    "embed_text": et,
                    "vector": vec,
                    "eligibility": {"eligible": True},
                }
            )
    else:
        # No chunk vectors can be produced. Record an explicit skip outcome (contract-aligned).
        skipped_vectors.append(
            {
                "reason": "NO_ELIGIBLE_CHUNKS",
                "detail": "no eligible chunk records present in chunks jsonl",
            }
        )

    # Artifact-level quality must reflect skipped vectors
    quality_status = "ok"
    quality_reasons: List[str] = []
    if skipped_vectors:
        quality_status = "warn"
        quality_reasons = ["NO_ELIGIBLE_CHUNKS"]

    artifact = {
        "schema_version": 1,
        "media_id": mid,
        "model": {
            "model_id": args.model_id,
            "model_revision": model_revision,
            "dimension": dim,
        },
        "embed_config": embed_config,
        "embed_config_sha256": cfg_sha,
        "asr_schema_version": 2,
        "chunks_artifact_sha256": sha256_file(chunks_path),
        "created_utc": utc_now_iso(),
        "runtime": {
            "python_version": platform.python_version(),
            "torch_version": getattr(torch, "__version__", "unknown"),
            "device": args.device,
            "venv": detect_venv_label(args.venv),
            "sentence_transformers_version": getattr(sentence_transformers, "__version__", "unknown"),
            "transformers_version": getattr(transformers, "__version__", "unknown"),
            "huggingface_hub_version": getattr(huggingface_hub, "__version__", "unknown"),
        },
        "quality": {
            "status": quality_status,
            "reasons": quality_reasons,
            "stats": {
                "vectors_produced": int(len(vectors)),
                "vectors_skipped": int(len(skipped_vectors)),
                "chunks_seen": int(len(chunks)),
                "records_seen": int(len(parsed)),
            },
        },
        "skipped": skipped_vectors,
        "vectors": vectors,
    }

    out_path = f"/workspace/catalog/embeddings/{mid}.json"
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, out_path)

    print("WROTE:", out_path)
    print("vectors:", len(vectors), "dim:", dim, "cfg_sha:", cfg_sha)
    print("model_revision:", model_revision)
    print("venv:", artifact["runtime"]["venv"])


if __name__ == "__main__":
    main()