#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


FINGERPRINT_SCHEMA_VERSION = 1
FINGERPRINT_ID = "stageB1_embed_e5_onnx"
FINGERPRINT_VERSION = "2026-02-04.1"


def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def log(level: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"embed_onnx|{level}|{ts} {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    log("ERROR", msg)
    raise SystemExit(2)


def warn(msg: str) -> None:
    log("WARN", msg)


def info(msg: str) -> None:
    log("INFO", msg)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_self() -> str:
    return sha256_file(os.path.abspath(__file__))


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def l2_normalize(x: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.maximum(n, eps)


def mean_attention_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """
    last_hidden: [B, T, D]
    attention_mask: [B, T] int64 or int32
    """
    mask = attention_mask.astype(np.float32)[:, :, None]  # [B, T, 1]
    summed = (last_hidden * mask).sum(axis=1)  # [B, D]
    denom = np.maximum(mask.sum(axis=1), 1e-6)  # [B, 1]
    return summed / denom


def iter_jsonl(path: str) -> Iterable[Tuple[int, Dict[str, Any]]]:
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                warn(f"bad_jsonl_line|path={path}|line={line_no}|err={e}")
                continue
            if isinstance(obj, dict):
                yield line_no, obj


def parse_chunks_jsonl(path: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Returns (chunks, stats).

    "eligible chunk record" means: dict with "chunk_id" and "text" keys.
    Non-chunk records are ignored.
    """
    chunks: List[Dict[str, Any]] = []
    stats = {
        "lines_nonblank": 0,
        "lines_bad_json": 0,   # best-effort, not exact due to warn-only
        "records_total": 0,
        "records_chunk": 0,
        "records_ignored": 0,
    }

    for _, obj in iter_jsonl(path):
        stats["records_total"] += 1
        if "chunk_id" in obj and "text" in obj:
            stats["records_chunk"] += 1
            chunks.append(obj)
        else:
            stats["records_ignored"] += 1

    return chunks, stats


def build_embed_config(batch_size: int, max_seq_length: int, normalize: bool) -> Dict[str, Any]:
    """
    Operational config (may include batch_size).
    This is recorded for provenance but MUST NOT be used to define embedding-space identity.
    """
    return {
        "normalize_embeddings": bool(normalize),
        "max_seq_length": int(max_seq_length),
        "pooling": "mean_attention",
        "prefix_passage": "passage: ",
        "prefix_query": "query: ",
        "batch_size": int(batch_size),
        "backend": "onnxruntime",
    }


def build_fingerprint_config(
    model_id: str,
    onnx_model_sha256: str,
    tokenizer_sha256: str,
    normalize: bool,
    max_seq_length: int,
    pooling: str,
    prefix_passage: str,
    prefix_query: str,
    backend: str,
) -> Dict[str, Any]:
    """
    Semantic config, stable, explicit, and stage-agnostic.
    Excludes operational knobs like batch_size and ORT thread counts.
    """
    return {
        "model_id": model_id,
        "onnx_model_sha256": onnx_model_sha256,
        "tokenizer_sha256": tokenizer_sha256,
        "normalize_embeddings": bool(normalize),
        "max_seq_length": int(max_seq_length),
        "pooling": pooling,
        "prefix_passage": prefix_passage,
        "prefix_query": prefix_query,
        "backend": backend,
    }


def resolve_model_revision(
    explicit_revision: str,
    onnx_model_path: str,
    onnx_model_sha256: str,
    tokenizer_path: str,
    tokenizer_sha256: str,
) -> str:
    r = (explicit_revision or "").strip()
    if r:
        return r
    h = hashlib.sha256()
    h.update(onnx_model_path.encode("utf-8") + b"\0")
    h.update(onnx_model_sha256.encode("utf-8") + b"\0")
    h.update(tokenizer_path.encode("utf-8") + b"\0")
    h.update(tokenizer_sha256.encode("utf-8") + b"\0")
    return h.hexdigest()


def make_session(
    onnx_model_path: str,
    ort_intra_threads: int,
    ort_inter_threads: int,
) -> ort.InferenceSession:
    sess_options = ort.SessionOptions()
    sess_options.intra_op_num_threads = int(ort_intra_threads)
    sess_options.inter_op_num_threads = int(ort_inter_threads)
    sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    return ort.InferenceSession(
        onnx_model_path,
        sess_options=sess_options,
        providers=["CPUExecutionProvider"],
    )


def choose_output_name(sess: ort.InferenceSession) -> str:
    outs = sess.get_outputs()
    if not outs:
        fail("onnx_session_no_outputs")
    names = [o.name for o in outs]
    # Prefer common transformer naming if present
    for preferred in ("last_hidden_state", "output", "hidden_states"):
        if preferred in names:
            return preferred
    return names[0]


def encode_texts(
    tok: Tokenizer,
    texts: List[str],
    max_seq_length: int,
) -> Tuple[np.ndarray, np.ndarray]:
    encs = [tok.encode(t) for t in texts]
    B = len(encs)
    T = int(max_seq_length)
    input_ids = np.zeros((B, T), dtype=np.int64)
    attn = np.zeros((B, T), dtype=np.int64)
    for i, e in enumerate(encs):
        ids = e.ids[:T]
        if ids:
            input_ids[i, : len(ids)] = np.asarray(ids, dtype=np.int64)
            attn[i, : len(ids)] = 1
    return input_ids, attn


def embed_text_batch(
    sess: ort.InferenceSession,
    out_name: str,
    input_ids: np.ndarray,
    attn: np.ndarray,
    normalize: bool,
) -> np.ndarray:
    feeds: Dict[str, Any] = {"input_ids": input_ids, "attention_mask": attn}
    in_names = {i.name for i in sess.get_inputs()}
    if "token_type_ids" in in_names:
        feeds["token_type_ids"] = np.zeros_like(input_ids)

    out = sess.run([out_name], feeds)[0]
    if not isinstance(out, np.ndarray):
        out = np.asarray(out)
    if out.ndim != 3:
        fail(f"unexpected_onnx_output_shape|shape={getattr(out,'shape',None)}")

    pooled = mean_attention_pool(out, attn)
    if normalize:
        pooled = l2_normalize(pooled)
    return pooled


def embed_query_json_mode(
    tok: Tokenizer,
    sess: ort.InferenceSession,
    out_name: str,
) -> None:
    """
    Reads a single JSON payload from stdin and prints a single JSON object to stdout.

    Input payload (minimum):
      {"text": "...", "max_seq_length": 256, "normalize": true, "mode":"query"}

    Output:
      {"ok": true, "vector": [...], "dim": 384}
    """
    raw = sys.stdin.read()
    if not raw.strip():
        fail("stdin_empty_json_payload")
    try:
        payload = json.loads(raw)
    except Exception as e:
        fail(f"stdin_invalid_json|err={e}")

    text = str(payload.get("text", "") or "")
    if not text:
        fail("payload_missing_text")

    max_seq_length = int(payload.get("max_seq_length", 256))
    normalize = bool(payload.get("normalize", True))

    input_ids, attn = encode_texts(tok, [text], max_seq_length=max_seq_length)
    pooled = embed_text_batch(sess, out_name, input_ids, attn, normalize=normalize)
    vec = pooled[0].tolist()
    out = {"ok": True, "vector": vec, "dim": int(pooled.shape[1])}
    print(json.dumps(out, ensure_ascii=False))
    return


def main() -> None:
    ap = argparse.ArgumentParser()

    # Two modes:
    # 1) passage embedding: --mid + --venv, writes /workspace/catalog/embeddings/{mid}.json
    # 2) query embedding tool: --json reads stdin JSON and prints JSON (no writes)
    ap.add_argument("--json", action="store_true", help="JSON stdin->stdout query embed mode (no file writes)")

    ap.add_argument("--mid", default="", help="media_id to embed (passage mode)")
    ap.add_argument(
        "--venv",
        default="",
        help="REQUIRED in passage mode: venv label to record for provenance (e.g. /workspace/venvs/cpu)",
    )

    ap.add_argument("--batch_size", type=int, default=32)
    ap.add_argument("--max_seq_length", type=int, default=256)
    ap.add_argument("--normalize", action="store_true", default=True)
    ap.add_argument("--no-normalize", dest="normalize", action="store_false")

    ap.add_argument("--model_id", default="intfloat/e5-small-v2")
    ap.add_argument("--onnx_model_path", default="/workspace/models/onnx/e5-small-v2/model.onnx")
    ap.add_argument("--tokenizer_path", default="/workspace/models/onnx/e5-small-v2/tokenizer.json")
    ap.add_argument("--model_revision", default="", help="Optional explicit revision label")

    ap.add_argument("--ort_intra_threads", type=int, default=4)
    ap.add_argument("--ort_inter_threads", type=int, default=1)

    args = ap.parse_args()

    if not os.path.exists(args.onnx_model_path):
        fail(f"missing_onnx_model_path|path={args.onnx_model_path}")
    if not os.path.exists(args.tokenizer_path):
        fail(f"missing_tokenizer_path|path={args.tokenizer_path}")

    # Strong content hashes (authoritative)
    onnx_model_sha256 = sha256_file(args.onnx_model_path)
    tokenizer_sha256 = sha256_file(args.tokenizer_path)

    embed_config = build_embed_config(args.batch_size, args.max_seq_length, args.normalize)

    # Semantic fingerprint config (stable)
    fp_config = build_fingerprint_config(
        model_id=args.model_id,
        onnx_model_sha256=onnx_model_sha256,
        tokenizer_sha256=tokenizer_sha256,
        normalize=bool(args.normalize),
        max_seq_length=int(args.max_seq_length),
        pooling=str(embed_config["pooling"]),
        prefix_passage=str(embed_config["prefix_passage"]),
        prefix_query=str(embed_config["prefix_query"]),
        backend=str(embed_config["backend"]),
    )
    fp_config_sha256 = sha256_bytes(canonical_json_bytes(fp_config))

    model_revision = resolve_model_revision(
        explicit_revision=args.model_revision,
        onnx_model_path=args.onnx_model_path,
        onnx_model_sha256=onnx_model_sha256,
        tokenizer_path=args.tokenizer_path,
        tokenizer_sha256=tokenizer_sha256,
    )

    artifact_fingerprint = {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "stage": "B1",
        "producer": os.path.basename(__file__),
        "producer_sha256": sha256_self(),
        "fingerprint_id": FINGERPRINT_ID,
        "fingerprint_version": FINGERPRINT_VERSION,
        "config_sha256": fp_config_sha256,
        "config": fp_config,
    }

    # Tokenizer + ORT session
    tok = Tokenizer.from_file(args.tokenizer_path)
    sess = make_session(args.onnx_model_path, args.ort_intra_threads, args.ort_inter_threads)
    out_name = choose_output_name(sess)

    # JSON query mode: no file writes, stdin->stdout
    if args.json:
        embed_query_json_mode(tok, sess, out_name)
        return

    # Passage embedding mode
    mid = (args.mid or "").strip()
    if not mid:
        fail("missing_mid_in_passage_mode")
    if not args.venv.strip():
        fail("missing_required_venv_in_passage_mode")

    chunks_path = f"/workspace/catalog/chunks/{mid}.jsonl"
    if not os.path.exists(chunks_path):
        fail(f"missing_chunks_jsonl|path={chunks_path}")

    chunks, chunks_stats = parse_chunks_jsonl(chunks_path)

    vectors: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    prefix_passage = str(embed_config["prefix_passage"])

    dim: Optional[int] = None

    if not chunks:
        skipped.append(
            {
                "reason": "NO_ELIGIBLE_CHUNKS",
                "detail": "no eligible chunk records present in chunks jsonl",
            }
        )
        # Determine dim from the model without guessing.
        # Use a single empty-string passage (with prefix) to get pooled shape.
        probe_text = prefix_passage  # minimal, deterministic
        input_ids, attn = encode_texts(tok, [probe_text], max_seq_length=int(args.max_seq_length))
        pooled = embed_text_batch(sess, out_name, input_ids, attn, normalize=bool(args.normalize))
        dim = int(pooled.shape[1])
    else:
        texts = [str(c.get("text", "") or "") for c in chunks]
        embed_texts = [prefix_passage + t for t in texts]

        bs = int(args.batch_size)
        max_len = int(args.max_seq_length)

        for b0 in range(0, len(embed_texts), bs):
            batch = embed_texts[b0 : b0 + bs]
            input_ids, attn = encode_texts(tok, batch, max_seq_length=max_len)
            pooled = embed_text_batch(sess, out_name, input_ids, attn, normalize=bool(args.normalize))

            if dim is None:
                dim = int(pooled.shape[1])
            elif int(pooled.shape[1]) != int(dim):
                fail("embedding_dim_changed_across_batches")

            pooled_list = pooled.tolist()

            # Deterministic embedding_id:
            # Use the SEMANTIC fingerprint config hash, not operational embed_config hash.
            for c, et, vec in zip(chunks[b0 : b0 + len(batch)], batch, pooled_list):
                cid = str(c["chunk_id"])
                material = (cid + "|" + args.model_id + "|" + model_revision + "|" + fp_config_sha256).encode("utf-8")
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

    quality_status = "ok"
    quality_reasons: List[str] = []
    if skipped:
        quality_status = "warn"
        quality_reasons = [s.get("reason", "UNKNOWN") for s in skipped if isinstance(s, dict) and s.get("reason")]
        if not quality_reasons:
            quality_reasons = ["NO_ELIGIBLE_CHUNKS"]

    if dim is None:
        fail("failed_to_determine_embedding_dim")

    artifact = {
        "schema_version": 1,
        "media_id": mid,
        "artifact_fingerprint": artifact_fingerprint,
        "model": {
            "model_id": args.model_id,
            "model_revision": model_revision,
            "dimension": int(dim),
        },
        # Operational config for provenance (may include batch_size)
        "embed_config": embed_config,
        # IMPORTANT: this is the semantic fingerprint hash, not operational config hash
        "embed_config_sha256": fp_config_sha256,
        "asr_schema_version": 2,
        "chunks_artifact_sha256": sha256_file(chunks_path),
        "created_utc": utc_now_iso(),
        "runtime": {
            "python_version": platform.python_version(),
            "venv": args.venv,
            "onnxruntime_version": getattr(ort, "__version__", "unknown"),
            "numpy_version": getattr(np, "__version__", "unknown"),
            "device": "cpu",
            "backend": "onnxruntime",
            "onnx_model_path": args.onnx_model_path,
            "tokenizer_path": args.tokenizer_path,
            "onnx_model_sha256": onnx_model_sha256,
            "tokenizer_sha256": tokenizer_sha256,
            "ort_intra_threads": int(args.ort_intra_threads),
            "ort_inter_threads": int(args.ort_inter_threads),
            "ort_execution_mode": "ORT_SEQUENTIAL",
            "onnx_output_name": out_name,
        },
        "quality": {
            "status": quality_status,
            "reasons": quality_reasons,
            "stats": {
                "vectors_produced": int(len(vectors)),
                "vectors_skipped": int(len(skipped)),
                "chunks_seen": int(len(chunks)),
                "records_total": int(chunks_stats.get("records_total", 0)),
                "records_chunk": int(chunks_stats.get("records_chunk", 0)),
                "records_ignored": int(chunks_stats.get("records_ignored", 0)),
            },
        },
        "skipped": skipped,
        "vectors": vectors,
    }

    out_path = f"/workspace/catalog/embeddings/{mid}.json"
    tmp_path = out_path + ".tmp"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, out_path)

    # stdout: compact, intended summary (safe for piping)
    print(f"WROTE: {out_path}")
    print(f"media_id: {mid}")
    print(f"vectors: {len(vectors)} dim: {int(dim)}")
    print(f"model_id: {args.model_id}")
    print(f"model_revision: {model_revision}")
    print(f"fingerprint_id: {artifact_fingerprint['fingerprint_id']} fingerprint_version: {artifact_fingerprint['fingerprint_version']}")
    print(f"producer_sha256: {artifact_fingerprint['producer_sha256']}")
    print(f"config_sha256: {artifact_fingerprint['config_sha256']}")
    print(f"venv: {args.venv}")
    print(f"onnx_output_name: {out_name}")
    print(f"ort_intra_threads: {int(args.ort_intra_threads)} ort_inter_threads: {int(args.ort_inter_threads)}")


if __name__ == "__main__":
    main()