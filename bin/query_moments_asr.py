#!/usr/bin/env python3
"""
query_moments_asr.py

DIAGNOSTIC TOOLING (NON-CANONICAL) — READ-ONLY

Status:
- Permanently non-canonical diagnostic tooling (unless explicitly promoted later).
- Not part of any stage contract.
- Not required for pipeline correctness.
- Must never write to /workspace/catalog/* (canonical artifacts) or mutate moments.db.

Purpose:
- Provide an evidence-grounded read-path over Milvus Lite moments.db (moments_asr_v1)
- Hydrate semantic hits back to canonical chunk records:
    pk = media_id:chunk_id  ->  /workspace/catalog/chunks/<media_id>.jsonl

Contracts this tool must obey:
- Canonical truth is always the chunks JSONL (and their provenance fields), not Milvus.
- No inference from absence: missing chunks file or missing chunk_id must be surfaced explicitly.
- Semantic layer is best-effort acceleration only; availability and contents must be auditable.
- Embedding identity must match Stage B1 ONNX embedder:
    /workspace/pipeline/bin/stageB1_embed_e5_onnx.py
  Do not change embedding behaviour here unless Stage B1 changes and you intentionally mirror it.

Design goals:
- Deterministic: all joins via pk = media_id:chunk_id
- No inference: always rehydrate from canonical chunks jsonl when possible
- Safe: read-only access to moments.db and /workspace/catalog/chunks/*.jsonl
- Fast: per-media chunk dict cache built once per run per media_id

Recommended runtime: /workspace/venvs/cpu (has onnxruntime + tokenizers + pymilvus)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from pymilvus import MilvusClient


DEFAULT_DB_PATH = "/workspace/catalog/milvus/moments.db"
DEFAULT_COLLECTION = "moments_asr_v1"
DEFAULT_ONNX_MODEL_PATH = "/workspace/models/onnx/e5-small-v2/model.onnx"
DEFAULT_TOKENIZER_PATH = "/workspace/models/onnx/e5-small-v2/tokenizer.json"

DEFAULT_QUERY_PREFIX = "query: "
DEFAULT_MAX_LEN = 256
DEFAULT_NORMALIZE = True

# These defaults avoid onnxruntime trying to set CPU affinity in some container setups.
# You can override from CLI if you want, but explicit is safer than implicit.
DEFAULT_ORT_INTRA_THREADS = 1
DEFAULT_ORT_INTER_THREADS = 1


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def l2_normalize(x: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    n = float(np.linalg.norm(x))
    if n < eps:
        return x
    return x / n


def mean_pool_last_hidden(last_hidden: np.ndarray, attn_mask: np.ndarray) -> np.ndarray:
    """
    last_hidden: [B, T, H]
    attn_mask:   [B, T] 0/1
    """
    mask = attn_mask.astype(np.float32)
    mask3 = mask[:, :, None]  # [B, T, 1]
    summed = (last_hidden * mask3).sum(axis=1)  # [B, H]
    denom = mask3.sum(axis=1)  # [B, 1]
    denom = np.maximum(denom, 1e-6)
    return summed / denom


@dataclass
class EmbedderConfig:
    onnx_model_path: str
    tokenizer_path: str
    query_prefix: str
    max_len: int
    normalize: bool
    ort_intra_threads: int
    ort_inter_threads: int


class E5OnnxEmbedder:
    """
    Minimal ONNX embedder for intfloat/e5-small-v2 exported to ONNX.

    This specific ONNX export requires:
      - input_ids
      - attention_mask
      - token_type_ids

    We detect accepted input names and feed what is required.
    """

    def __init__(self, cfg: EmbedderConfig):
        self.cfg = cfg
        try:
            import onnxruntime as ort  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "Missing onnxruntime. Run this script in /workspace/venvs/cpu, "
                "or install onnxruntime into the active venv."
            ) from e

        try:
            from tokenizers import Tokenizer  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "Missing tokenizers. Run this script in /workspace/venvs/cpu, "
                "or install tokenizers into the active venv."
            ) from e

        if not os.path.exists(cfg.onnx_model_path):
            raise FileNotFoundError(f"onnx_model_path not found: {cfg.onnx_model_path}")
        if not os.path.exists(cfg.tokenizer_path):
            raise FileNotFoundError(f"tokenizer_path not found: {cfg.tokenizer_path}")

        self.ort = ort

        so = ort.SessionOptions()
        # Avoid affinity errors in containers by setting explicit thread counts.
        so.intra_op_num_threads = int(cfg.ort_intra_threads)
        so.inter_op_num_threads = int(cfg.ort_inter_threads)

        self.sess = ort.InferenceSession(
            cfg.onnx_model_path,
            sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        self.tokenizer = Tokenizer.from_file(cfg.tokenizer_path)

        self.input_names = [i.name for i in self.sess.get_inputs()]
        self.output_names = [o.name for o in self.sess.get_outputs()]

        # Resolve expected input names.
        # For this model we expect these, but do not assume ordering.
        self.input_ids_name = "input_ids" if "input_ids" in self.input_names else self.input_names[0]
        self.attn_name = "attention_mask" if "attention_mask" in self.input_names else None
        self.tt_name = "token_type_ids" if "token_type_ids" in self.input_names else None

    def embed_query(self, q: str) -> np.ndarray:
        text = (self.cfg.query_prefix or "") + q.strip()

        enc = self.tokenizer.encode(text)
        ids = enc.ids[: self.cfg.max_len]

        # tokenizers Encoding usually has attention_mask, if not assume all ones.
        attn_list = enc.attention_mask[: self.cfg.max_len] if hasattr(enc, "attention_mask") else [1] * len(ids)

        max_len = self.cfg.max_len
        input_ids = np.zeros((1, max_len), dtype=np.int64)
        attention = np.zeros((1, max_len), dtype=np.int64)

        n = len(ids)
        input_ids[0, :n] = np.array(ids, dtype=np.int64)
        attention[0, :n] = np.array(attn_list, dtype=np.int64)

        feeds: Dict[str, Any] = {self.input_ids_name: input_ids}

        # Feed attention_mask if the model expects it.
        if self.attn_name is not None:
            feeds[self.attn_name] = attention

        # Feed token_type_ids if the model expects it.
        if self.tt_name is not None:
            token_type_ids = np.zeros((1, max_len), dtype=np.int64)
            feeds[self.tt_name] = token_type_ids

        outs = self.sess.run(self.output_names, feeds)

        # Expect first output is last_hidden_state [B, T, H]
        last_hidden = outs[0]
        if not isinstance(last_hidden, np.ndarray) or last_hidden.ndim != 3:
            raise RuntimeError(f"Unexpected ONNX output shape/type. Got type={type(last_hidden)}")

        pooled = mean_pool_last_hidden(last_hidden.astype(np.float32), attention)
        vec = pooled[0].astype(np.float32)

        if self.cfg.normalize:
            vec = l2_normalize(vec)

        return vec


def parse_pk(pk: str) -> Tuple[str, str]:
    if ":" not in pk:
        raise ValueError(f"pk missing ':' separator: {pk}")
    mid, cid = pk.split(":", 1)
    if not mid or not cid:
        raise ValueError(f"pk malformed: {pk}")
    return mid, cid


@lru_cache(maxsize=512)
def load_chunks_for_media(media_id: str) -> Dict[str, Dict[str, Any]]:
    """
    Build a dict: chunk_id -> chunk record
    Cached per process using lru_cache to keep it simple and fast.
    """
    path = f"/workspace/catalog/chunks/{media_id}.jsonl"
    out: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(path):
        return out

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            cid = obj.get("chunk_id")
            if isinstance(cid, str) and cid:
                out[cid] = obj
    return out


def _hit_to_dict(h: Any) -> Dict[str, Any]:
    """
    Normalize different pymilvus hit representations into a consistent dict.

    We have observed:
      - Hit objects with attributes: id, distance/score, fields (dict)
      - Dict-like hits with keys: 'distance', 'entity', etc
    """
    # Preferred path: Hit object from pymilvus.client.search_result.Hit
    if hasattr(h, "fields") or hasattr(h, "distance") or hasattr(h, "score"):
        fields = getattr(h, "fields", None)
        if not isinstance(fields, dict):
            fields = {}

        score_val = None
        if hasattr(h, "distance"):
            score_val = getattr(h, "distance")
        elif hasattr(h, "score"):
            score_val = getattr(h, "score")

        # pk is in fields; fallback to hit.id if needed
        pk_val = fields.get("pk")
        if not pk_val and hasattr(h, "id"):
            pk_val = getattr(h, "id")

        return {
            "score": float(score_val) if isinstance(score_val, (int, float)) else score_val,
            "pk": pk_val,
            "media_id": fields.get("media_id"),
            "start_s": fields.get("start_s"),
            "end_s": fields.get("end_s"),
            "text": fields.get("text"),
        }

    # Fallback path: dict-like hit
    if isinstance(h, dict):
        ent = h.get("entity", {})
        if not isinstance(ent, dict):
            ent = {}
        score_val = h.get("distance", None)
        return {
            "score": float(score_val) if isinstance(score_val, (int, float)) else score_val,
            "pk": ent.get("pk") or h.get("id"),
            "media_id": ent.get("media_id"),
            "start_s": ent.get("start_s"),
            "end_s": ent.get("end_s"),
            "text": ent.get("text"),
        }

    # Unknown shape
    return {"score": None, "pk": None, "media_id": None, "start_s": None, "end_s": None, "text": None}


def milvus_search(
    client: MilvusClient,
    collection: str,
    vector: np.ndarray,
    topk: int,
    media_id: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Returns a list of hits with fields:
      - score
      - pk, media_id, start_s, end_s, text
    """
    filt: Optional[str] = None
    if media_id:
        filt = f'media_id == "{media_id}"'

    output_fields = ["pk", "media_id", "start_s", "end_s", "text"]

    # MilvusClient.search API varies across pymilvus versions.
    # Prefer the newer keyword signature, fall back if needed.
    try:
        res = client.search(
            collection_name=collection,
            data=[vector.tolist()],
            anns_field="vector",
            limit=topk,
            output_fields=output_fields,
            filter=filt if filt else None,
        )
    except TypeError:
        res = client.search(
            collection,
            [vector.tolist()],
            "vector",
            topk,
            output_fields=output_fields,
            expr=filt if filt else None,
        )

    hits: List[Dict[str, Any]] = []
    if not res:
        return hits

    # SearchResult is iterable and indexable; res[0] is HybridHits
    first = res[0] if hasattr(res, "__getitem__") else None
    if first is None:
        return hits

    # HybridHits is iterable over Hit objects
    for h in list(first):
        hits.append(_hit_to_dict(h))

    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Semantic search over moments_asr_v1 with canonical hydration.")
    ap.add_argument("--q", required=True, help="Query string")
    ap.add_argument("--topk", type=int, default=20, help="Top K results")
    ap.add_argument("--media_id", default="", help="Optional media_id filter")
    ap.add_argument("--db_path", default=DEFAULT_DB_PATH, help="Path to moments.db")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION, help="Collection name")
    ap.add_argument("--onnx_model_path", default=DEFAULT_ONNX_MODEL_PATH, help="ONNX model path")
    ap.add_argument("--tokenizer_path", default=DEFAULT_TOKENIZER_PATH, help="Tokenizer path")
    ap.add_argument("--max_len", type=int, default=DEFAULT_MAX_LEN, help="Tokenizer max length")
    ap.add_argument("--no_normalize", action="store_true", help="Disable L2 normalization")
    ap.add_argument("--query_prefix", default=DEFAULT_QUERY_PREFIX, help='Query prefix, default "query: "')
    ap.add_argument("--ort_intra_threads", type=int, default=DEFAULT_ORT_INTRA_THREADS, help="onnxruntime intra-op threads")
    ap.add_argument("--ort_inter_threads", type=int, default=DEFAULT_ORT_INTER_THREADS, help="onnxruntime inter-op threads")
    args = ap.parse_args()

    media_id = args.media_id.strip() or None

    if not os.path.exists(args.db_path):
        print(json.dumps({"ok": False, "error": f"db_path not found: {args.db_path}"}))
        return 2

    embed_cfg = EmbedderConfig(
        onnx_model_path=args.onnx_model_path,
        tokenizer_path=args.tokenizer_path,
        query_prefix=args.query_prefix,
        max_len=int(args.max_len),
        normalize=(not args.no_normalize),
        ort_intra_threads=int(args.ort_intra_threads),
        ort_inter_threads=int(args.ort_inter_threads),
    )

    try:
        embedder = E5OnnxEmbedder(embed_cfg)
        q_vec = embedder.embed_query(args.q)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"embed_failed: {type(e).__name__}: {str(e)}"}))
        return 3

    try:
        client = MilvusClient(args.db_path)
        collections = client.list_collections()
        if args.collection not in collections:
            print(json.dumps({"ok": False, "error": f"collection not found: {args.collection}", "collections": collections}))
            return 4
        hits = milvus_search(client, args.collection, q_vec, int(args.topk), media_id)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"milvus_search_failed: {type(e).__name__}: {str(e)}"}))
        return 5

    hydrated: List[Dict[str, Any]] = []
    for h in hits:
        pk = h.get("pk")
        if not isinstance(pk, str) or not pk:
            hydrated.append({**h, "hydrate": {"ok": False, "reason": "missing_pk"}})
            continue

        try:
            mid, cid = parse_pk(pk)
        except Exception as e:
            hydrated.append({**h, "hydrate": {"ok": False, "reason": f"bad_pk: {type(e).__name__}: {str(e)}"}})
            continue

        chunk_map = load_chunks_for_media(mid)
        chunk = chunk_map.get(cid)

        if chunk is None:
            hydrated.append(
                {
                    **h,
                    "hydrate": {
                        "ok": False,
                        "reason": "chunk_not_found",
                        "media_id_from_pk": mid,
                        "chunk_id_from_pk": cid,
                        "chunks_file_present": os.path.exists(f"/workspace/catalog/chunks/{mid}.jsonl"),
                    },
                }
            )
            continue

        mismatches: List[str] = []
        if h.get("media_id") and h.get("media_id") != chunk.get("media_id"):
            mismatches.append("media_id_mismatch")
        if isinstance(h.get("start_s"), (int, float)) and isinstance(chunk.get("start_s"), (int, float)):
            if abs(float(h["start_s"]) - float(chunk["start_s"])) > 1e-3:
                mismatches.append("start_s_mismatch")
        if isinstance(h.get("end_s"), (int, float)) and isinstance(chunk.get("end_s"), (int, float)):
            if abs(float(h["end_s"]) - float(chunk["end_s"])) > 1e-3:
                mismatches.append("end_s_mismatch")

        hydrated.append(
            {
                **h,
                "pk_parts": {"media_id": mid, "chunk_id": cid},
                "canonical_chunk": chunk,
                "hydrate": {"ok": True, "mismatches": mismatches},
            }
        )

    out = {
        "ok": True,
        "query": {"q": args.q, "topk": int(args.topk), "media_id_filter": media_id or ""},
        "embedding_identity": {
            "model_id_hint": "intfloat/e5-small-v2",
            "onnx_model_path": args.onnx_model_path,
            "tokenizer_path": args.tokenizer_path,
            "onnx_model_sha256": sha256_file(args.onnx_model_path) if os.path.exists(args.onnx_model_path) else "",
            "tokenizer_sha256": sha256_file(args.tokenizer_path) if os.path.exists(args.tokenizer_path) else "",
            "max_len": int(args.max_len),
            "normalize": (not args.no_normalize),
            "query_prefix": args.query_prefix,
            "ort_intra_threads": int(args.ort_intra_threads),
            "ort_inter_threads": int(args.ort_inter_threads),
            "onnx_inputs": ["input_ids", "attention_mask", "token_type_ids"],
        },
        "milvus": {"db_path": args.db_path, "collection": args.collection},
        "results": hydrated,
    }

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())