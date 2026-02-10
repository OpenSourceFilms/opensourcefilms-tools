#!/usr/bin/env python3
"""
stageB1_search_local.py

Local semantic search over embedding artifacts.

Design goals for pipeline use:
- Do not mix embedding spaces: query embedding must match artifact embed_config and model metadata.
- Stream artifacts and vectors; keep only Top-K to avoid high RAM use.
- stdout is for results, stderr is for logs. No file writes.
- Prefer audit index as source of truth; allow fallback to scanning embeddings root.
"""

import argparse
import heapq
import json
import math
import os
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(level: str, msg: str) -> None:
    print(f"search_local|{level}|{utc_ts()} {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    log("ERROR", msg)
    raise SystemExit(2)


def warn(msg: str) -> None:
    log("WARN", msg)


def info(msg: str) -> None:
    log("INFO", msg)


def iter_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            s = line.strip()
            if not s:
                continue
            try:
                yield json.loads(s)
            except Exception as e:
                warn(f"bad_jsonl_line|path={path}|line={line_no}|err={e}")
                continue


def bool_get(d: Dict[str, Any], path: Sequence[str], default: bool = False) -> bool:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return bool(cur)


def str_get(d: Dict[str, Any], path: Sequence[str], default: str = "") -> str:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return default if cur is None else str(cur)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_embedding_artifact_paths_from_index(
    index_path: str,
    require_embeddings: bool,
    media_id_filter: Optional[str],
    source_contains: Optional[str],
    require_enrich: bool,
    require_keyframes: bool,
    limit_media: int,
) -> List[str]:
    if not os.path.exists(index_path):
        fail(f"audit_index_missing|path={index_path}")

    embed_paths: List[str] = []
    seen_mids: set = set()

    for rec in iter_jsonl(index_path):
        mid = str(rec.get("media_id", "")).strip()
        if not mid:
            continue

        if media_id_filter and mid != media_id_filter:
            continue

        if source_contains:
            sp = str(rec.get("source_path", "") or "")
            if source_contains not in sp:
                continue

        if require_embeddings and not bool_get(rec, ["exists", "embeddings_json", "exists"], False):
            continue

        if require_enrich and not bool_get(rec, ["exists", "enrich_json", "exists"], False):
            continue

        if require_keyframes and not bool_get(rec, ["exists", "keyframes_dir", "exists"], False):
            continue

        p = str_get(rec, ["paths", "embeddings_json"], "")
        if not p:
            p = str_get(rec, ["paths", "embeddings"], "")
        if not p:
            continue

        if mid in seen_mids:
            continue
        seen_mids.add(mid)
        embed_paths.append(p)

        if limit_media and len(embed_paths) >= int(limit_media):
            break

    return embed_paths


def load_embedding_artifacts_from_paths(paths: List[str]) -> List[Dict[str, Any]]:
    arts: List[Dict[str, Any]] = []
    for p in paths:
        if not p:
            continue
        if not os.path.exists(p):
            warn(f"missing_embedding_artifact|path={p}")
            continue
        try:
            arts.append(load_json(p))
        except Exception as e:
            warn(f"bad_embedding_artifact|path={p}|err={e}")
            continue
    return arts


def load_embedding_artifacts_from_root(root: str) -> List[Dict[str, Any]]:
    try:
        names = sorted([n for n in os.listdir(root) if n.endswith(".json")])
    except FileNotFoundError:
        fail(f"embeddings_root_missing|path={root}")
    paths = [os.path.join(root, n) for n in names]
    return load_embedding_artifacts_from_paths(paths)


def enforce_consistency(arts: List[Dict[str, Any]]) -> Tuple[str, str, int, str, Dict[str, Any]]:
    """
    Enforce that all artifacts agree on:
      - model.model_id
      - model.model_revision
      - model.dimension
      - embed_config_sha256

    Also return embed_config from the first artifact (as the canonical config for the run).
    """
    if not arts:
        fail("no_embedding_artifacts_loaded")

    def req(a: Dict[str, Any], path: Sequence[str]) -> Any:
        cur: Any = a
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                fail(f"missing_required_field|field={'.'.join(path)}")
            cur = cur[k]
        return cur

    a0 = arts[0]
    model_id = str(req(a0, ["model", "model_id"]))
    model_rev = str(req(a0, ["model", "model_revision"]))
    dim = int(req(a0, ["model", "dimension"]))
    cfg_sha = str(req(a0, ["embed_config_sha256"]))
    embed_cfg = req(a0, ["embed_config"])
    if not isinstance(embed_cfg, dict):
        fail("embed_config_not_dict")

    for a in arts[1:]:
        if str(req(a, ["model", "model_id"])) != model_id:
            fail("inconsistent_model_id_across_artifacts")
        if str(req(a, ["model", "model_revision"])) != model_rev:
            fail("inconsistent_model_revision_across_artifacts")
        if int(req(a, ["model", "dimension"])) != dim:
            fail("inconsistent_dimension_across_artifacts")
        if str(req(a, ["embed_config_sha256"])) != cfg_sha:
            fail("inconsistent_embed_config_sha256_across_artifacts")

    return model_id, model_rev, dim, cfg_sha, embed_cfg


def dot(a: List[float], b: List[float]) -> float:
    return sum((x * y) for x, y in zip(a, b))


def l2_norm(a: List[float]) -> float:
    return math.sqrt(sum((x * x) for x in a))


def cosine_sim(a: List[float], b: List[float], assume_normalized: bool) -> float:
    if assume_normalized:
        return dot(a, b)
    na = l2_norm(a)
    nb = l2_norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot(a, b) / (na * nb)


def embed_query_via_sentence_transformers(
    model_id: str,
    query_prefix: str,
    query: str,
    device: str,
    max_seq_length: int,
    normalize: bool,
) -> List[float]:
    """
    Fallback embedder. This is ONLY safe if your artifacts were produced with the same library/settings.

    In your environment, production may use an ONNX path. Prefer --embedder onnx when available.
    """
    from sentence_transformers import SentenceTransformer  # type: ignore

    model = SentenceTransformer(model_id, device=device)
    try:
        model.max_seq_length = int(max_seq_length)
    except Exception:
        pass

    text = (query_prefix or "") + query
    vec = model.encode([text], normalize_embeddings=normalize, show_progress_bar=False)
    return vec[0].tolist()


def embed_query_via_onnx_tool(
    onnx_embed_py: str,
    query_prefix: str,
    query: str,
    max_seq_length: int,
    normalize: bool,
    device: str,
) -> List[float]:
    """
    Calls the pipeline's ONNX embedder tool to ensure the same embedding space as production.

    Expected contract for the tool:
      - reads query text argument
      - emits a JSON object on stdout containing {"vector":[...]} or similar
    This wrapper is defensive: it accepts a few likely shapes.
    """
    if not os.path.exists(onnx_embed_py):
        fail(f"onnx_embedder_missing|path={onnx_embed_py}")

    payload = {
        "text": (query_prefix or "") + query,
        "max_seq_length": int(max_seq_length),
        "normalize": bool(normalize),
        "device": device,
        "mode": "query",
    }

    cmd = [sys.executable, onnx_embed_py, "--json"]
    p = subprocess.run(
        cmd,
        input=json.dumps(payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if p.returncode != 0:
        fail(f"onnx_embedder_failed|rc={p.returncode}|stderr={p.stderr.strip()}")

    out = p.stdout.strip()
    try:
        j = json.loads(out)
    except Exception:
        fail("onnx_embedder_invalid_json_stdout")

    vec = None
    if isinstance(j, dict):
        if isinstance(j.get("vector"), list):
            vec = j.get("vector")
        elif isinstance(j.get("embedding"), list):
            vec = j.get("embedding")
        elif isinstance(j.get("data"), dict) and isinstance(j["data"].get("vector"), list):
            vec = j["data"]["vector"]

    if not isinstance(vec, list):
        fail("onnx_embedder_missing_vector_in_output")
    return [float(x) for x in vec]


def query_audit_list_mids(query_audit_py: str, tier: str, source_contains: str, index_path: str) -> List[str]:
    """
    Calls the canonical audit query tool to list media_ids by tier.
    If that fails for any reason, we hard fail (contract discipline).
    """
    if not os.path.exists(query_audit_py):
        fail(f"query_audit_missing|path={query_audit_py}")

    cmd = [sys.executable, query_audit_py, "list", "--tier", tier, "--index-path", index_path]
    if source_contains:
        cmd += ["--source-contains", source_contains]

    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        fail(f"query_audit_list_failed|rc={p.returncode}|stderr={p.stderr.strip()}")

    try:
        payload = json.loads(p.stdout)
    except Exception:
        fail("query_audit_list_invalid_json")

    if not bool(payload.get("ok", False)):
        fail(f"query_audit_list_ok_false|payload={payload}")

    mids = payload.get("media_ids", [])
    if not isinstance(mids, list):
        fail("query_audit_list_media_ids_not_list")

    out = [str(m).strip() for m in mids if str(m).strip()]
    return out


def iter_vectors_from_artifacts(arts: List[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """
    Stream rows (vector records) from embedding artifacts.
    Expected schema: artifact["vectors"] is a list of dicts containing "vector" plus metadata.
    """
    for a in arts:
        mid = str(a.get("media_id", "") or "")
        vecs = a.get("vectors", [])
        if not isinstance(vecs, list):
            continue
        for v in vecs:
            if not isinstance(v, dict):
                continue
            yield {
                "media_id": mid,
                "chunk_id": v.get("chunk_id"),
                "embedding_id": v.get("embedding_id"),
                "start_s": v.get("start_s"),
                "end_s": v.get("end_s"),
                "text": v.get("text", ""),
                "vector": v.get("vector", []),
            }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True, help="query text (without prefix)")
    ap.add_argument("--topk", type=int, default=10)

    # Primary source of truth: artifact-derived audit index
    ap.add_argument("--audit-index", default="/workspace/catalog/audit/index_latest.jsonl")
    mx = ap.add_mutually_exclusive_group()
    mx.add_argument("--use-audit-index", dest="use_audit_index", action="store_true", default=True)
    mx.add_argument("--no-audit-index", dest="use_audit_index", action="store_false")

    # Canonical audit query tool
    ap.add_argument("--query-audit", default="/workspace/pipeline/bin/query_audit.py")

    # Tier selection (when using audit index)
    ap.add_argument("--tier", default="semantic_ready", help="none|stageA_ready|semantic_ready|editorial_ready")
    ap.add_argument("--mid", default="", help="restrict to a single media_id (overrides --tier)")
    ap.add_argument("--source-contains", default="", help="restrict to records whose source_path contains substring")
    ap.add_argument("--limit-media", type=int, default=0, help="cap number of media_ids loaded (0 means no cap)")

    # Legacy flags retained (but enforced)
    ap.add_argument("--require-enrich", action="store_true", help="legacy; prefer --tier editorial_ready")
    ap.add_argument("--require-keyframes", action="store_true", help="legacy; prefer --tier editorial_ready")

    # Fallback: scan embeddings-root directly
    ap.add_argument("--embeddings-root", default="/workspace/catalog/embeddings")

    # Embedder selection
    ap.add_argument("--embedder", default="onnx", help="onnx|st (default onnx)")
    ap.add_argument("--onnx-embedder", default="/workspace/pipeline/bin/stageB1_embed_e5_onnx.py", help="path to ONNX embedder tool")

    ap.add_argument("--device", default="cpu", help="cpu|cuda")
    ap.add_argument("--max-seq-length", type=int, default=256)
    ap.add_argument("--normalize", action="store_true", default=True)
    ap.add_argument("--no-normalize", dest="normalize", action="store_false")

    ap.add_argument("--require-nonempty", action="store_true", help="fail if there are zero vectors searched")
    ap.add_argument("--allow-empty-results", action="store_true", help="do not fail if no hits (still prints header)")

    args = ap.parse_args()

    allowed_tiers = {"none", "stageA_ready", "semantic_ready", "editorial_ready"}
    if args.tier not in allowed_tiers:
        fail(f"invalid_tier|tier={args.tier}|allowed={sorted(allowed_tiers)}")

    if args.use_audit_index:
        if args.require_enrich or args.require_keyframes:
            if args.tier != "editorial_ready" and not args.mid.strip():
                fail("legacy_flags_require_editorial_ready_or_mid")

        if args.mid.strip():
            embed_paths = iter_embedding_artifact_paths_from_index(
                index_path=args.audit_index,
                require_embeddings=True,
                media_id_filter=args.mid.strip(),
                source_contains=args.source_contains.strip() or None,
                require_enrich=bool(args.require_enrich),
                require_keyframes=bool(args.require_keyframes),
                limit_media=1,
            )
            if not embed_paths:
                fail("no_embedding_paths_selected_from_index_for_mid")
        else:
            mids = query_audit_list_mids(
                query_audit_py=args.query_audit,
                tier=args.tier,
                source_contains=args.source_contains.strip(),
                index_path=args.audit_index,
            )
            if int(args.limit_media or 0) > 0:
                mids = mids[: int(args.limit_media)]
            if not mids:
                fail(f"no_media_ids_matched|tier={args.tier}|source_contains={args.source_contains!r}")

            # Select embedding paths for these mids by scanning index once
            midset = set(mids)
            embed_paths = []
            for rec in iter_jsonl(args.audit_index):
                mid = str(rec.get("media_id", "")).strip()
                if not mid or mid not in midset:
                    continue
                if not bool_get(rec, ["exists", "embeddings_json", "exists"], False):
                    continue
                p = str_get(rec, ["paths", "embeddings_json"], "")
                if p:
                    embed_paths.append(p)

            embed_paths = sorted(set(embed_paths))
            if not embed_paths:
                fail("no_embedding_paths_selected_from_index_for_tier")
    else:
        # Fallback mode
        embed_paths = []

    if args.use_audit_index:
        arts = load_embedding_artifacts_from_paths(embed_paths)
    else:
        arts = load_embedding_artifacts_from_root(args.embeddings_root)

    if not arts:
        fail("no_embedding_artifacts_loaded_after_filters")

    model_id, model_rev, dim, cfg_sha, embed_cfg = enforce_consistency(arts)

    # Enforce prefix discipline
    prefix_query = str(embed_cfg.get("prefix_query", "") or "")
    normalize_embeddings = bool(embed_cfg.get("normalize_embeddings", True))

    if bool(args.normalize) != bool(normalize_embeddings):
        fail(f"normalize_mismatch|cli_normalize={args.normalize}|artifact_normalize={normalize_embeddings}")

    # Embed query in the same space
    if args.embedder == "onnx":
        qvec = embed_query_via_onnx_tool(
            onnx_embed_py=args.onnx_embedder,
            query_prefix=prefix_query,
            query=args.q,
            max_seq_length=args.max_seq_length,
            normalize=normalize_embeddings,
            device=args.device,
        )
    elif args.embedder == "st":
        warn("embedder_sentence_transformers_selected|this_must_match_artifact_producer_exactly")
        qvec = embed_query_via_sentence_transformers(
            model_id=model_id,
            query_prefix=prefix_query,
            query=args.q,
            device=args.device,
            max_seq_length=args.max_seq_length,
            normalize=normalize_embeddings,
        )
    else:
        fail(f"invalid_embedder|embedder={args.embedder}|allowed=['onnx','st']")

    if not isinstance(qvec, list) or len(qvec) != dim:
        fail(f"query_vector_dim_mismatch|got={len(qvec) if isinstance(qvec, list) else 'nonlist'}|expected={dim}")

    # If embeddings are normalized, cosine == dot; otherwise use cosine with norms.
    assume_normalized = bool(normalize_embeddings)

    # Stream vectors, keep Top-K with a min-heap
    topk = max(0, int(args.topk))
    heap: List[Tuple[float, int, Dict[str, Any]]] = []
    n_seen = 0
    n_scored = 0
    skipped_bad_dim = 0

    for row in iter_vectors_from_artifacts(arts):
        n_seen += 1
        vec = row.get("vector")
        if not isinstance(vec, list) or len(vec) != dim:
            skipped_bad_dim += 1
            continue
        try:
            score = float(cosine_sim(qvec, vec, assume_normalized=assume_normalized))
        except Exception:
            continue

        n_scored += 1

        if topk == 0:
            continue

        # Tie-breaker with n_seen for stable-ish ordering
        item = (score, n_seen, row)
        if len(heap) < topk:
            heapq.heappush(heap, item)
        else:
            if score > heap[0][0]:
                heapq.heapreplace(heap, item)

    if args.require_nonempty and n_scored == 0:
        fail("no_vectors_scored")

    # Sort descending for output
    results = sorted(heap, key=lambda x: x[0], reverse=True)

    # Header on stdout
    print("MODEL_ID:", model_id)
    print("MODEL_REVISION:", model_rev)
    print("EMBED_CONFIG_SHA256:", cfg_sha)
    print("EMBEDDER:", args.embedder)
    print("DEVICE:", args.device)
    print("MAX_SEQ_LENGTH:", int(args.max_seq_length))
    print("NORMALIZE:", bool(normalize_embeddings))
    print("MEDIA_ARTIFACTS_LOADED:", len(arts))
    print("VECTORS_SEEN:", n_seen)
    print("VECTORS_SCORED:", n_scored)
    if skipped_bad_dim:
        print("VECTORS_SKIPPED_BAD_DIM:", skipped_bad_dim)

    if args.use_audit_index:
        print("AUDIT_INDEX:", args.audit_index)
        if args.mid.strip():
            print("FILTER_mid:", args.mid.strip())
        else:
            print("FILTER_tier:", args.tier)
        if args.source_contains.strip():
            print("FILTER_source_contains:", args.source_contains.strip())
        if int(args.limit_media or 0) > 0:
            print("FILTER_limit_media:", int(args.limit_media))
    else:
        print("EMBEDDINGS_ROOT:", args.embeddings_root)
    print()

    if not results:
        if args.allow_empty_results:
            print("No results.")
            return
        print("No results.")
        raise SystemExit(1)

    for rank, (s, _, r) in enumerate(results, start=1):
        print(f"#{rank}  score={s:.4f}")
        print(f"     media_id: {r.get('media_id','')}")
        print(f"     t: {r.get('start_s')}-{r.get('end_s')}")
        print(f"     chunk_id: {r.get('chunk_id')}")
        text = r.get("text", "")
        print(f"     text: {text}")
        print()


if __name__ == "__main__":
    main()