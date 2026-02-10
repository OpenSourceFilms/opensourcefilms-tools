#!/usr/bin/env python3
import argparse, glob, hashlib, json, os, sys, math
from typing import Any, Dict, List, Optional

EMB_SCHEMA_VERSION = 1

REQUIRED_TOP = [
    "schema_version",
    "media_id",
    "model",
    "embed_config",
    "embed_config_sha256",
    "asr_schema_version",
    "chunks_artifact_sha256",
    "created_utc",
    "runtime",
    "vectors",
]

REQUIRED_MODEL = ["model_id", "model_revision", "dimension"]
REQUIRED_CFG = [
    "normalize_embeddings",
    "max_seq_length",
    "pooling",
    "prefix_passage",
    "prefix_query",
    "batch_size",
]

REQUIRED_RUNTIME_BASE = [
    "python_version",
    "device",
    "venv",
]

REQUIRED_RUNTIME_ONNX = [
    "onnxruntime_version",
]

REQUIRED_RUNTIME_TORCH = [
    "torch_version",
    "sentence_transformers_version",
    "transformers_version",
    "huggingface_hub_version",
]

REQUIRED_VEC = ["chunk_id", "embedding_id", "start_s", "end_s", "text", "embed_text", "vector"]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return sha256_bytes(f.read())


def fail(msg: str) -> None:
    print("FAIL:", msg, file=sys.stderr)
    raise SystemExit(2)


def check_fields(obj: Dict[str, Any], req: List[str], ctx: str) -> None:
    if not isinstance(obj, dict):
        fail(f"{ctx} is not an object")
    for k in req:
        if k not in obj:
            fail(f"missing field {ctx}.{k}")


def is_finite_number(x: Any) -> bool:
    if not isinstance(x, (int, float)):
        return False
    return math.isfinite(float(x))


def l2_norm(vec: List[Any]) -> float:
    s = 0.0
    for a in vec:
        s += float(a) * float(a)
    return math.sqrt(s)


def infer_backend(emb: Dict[str, Any]) -> str:
    runtime = emb.get("runtime", {})
    if not isinstance(runtime, dict):
        return "unknown"

    backend = runtime.get("backend") or runtime.get("embedder_backend") or runtime.get("engine")
    if isinstance(backend, str):
        b = backend.strip().lower()
        if b in ("onnx", "onnxruntime", "ort"):
            return "onnx"
        if b in ("torch", "pytorch", "sentence-transformers", "sentencetransformers", "st"):
            return "torch"

    if "onnxruntime_version" in runtime:
        return "onnx"
    if "torch_version" in runtime or "sentence_transformers_version" in runtime:
        return "torch"

    return "unknown"


def load_chunks_jsonl(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError as e:
                fail(f"chunks.jsonl has invalid JSON ({e!r})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", help="media_id to check (checks one artifact)")
    ap.add_argument("--all", action="store_true", help="check all embedding artifacts")
    args = ap.parse_args()

    if not args.mid and not args.all:
        fail("provide --mid or --all")

    paths: List[str] = []
    if args.mid:
        p = f"/workspace/catalog/embeddings/{args.mid}.json"
        if not os.path.exists(p):
            fail(f"missing embedding artifact: {p}")
        paths = [p]
    else:
        paths = sorted(glob.glob("/workspace/catalog/embeddings/*.json"))

    if not paths:
        fail("no embedding artifacts found")

    for p in paths:
        mid = os.path.splitext(os.path.basename(p))[0]

        chunks_path = f"/workspace/catalog/chunks/{mid}.jsonl"
        if not os.path.exists(chunks_path):
            fail(f"{mid}: missing chunks artifact {chunks_path}")

        emb_b = open(p, "rb").read()
        try:
            emb = json.loads(emb_b)
        except Exception as e:
            fail(f"{mid}: embeddings artifact is not valid JSON ({e!r})")

        check_fields(emb, REQUIRED_TOP, "root")

        if emb["schema_version"] != EMB_SCHEMA_VERSION:
            fail(f"{mid}: schema_version {emb['schema_version']} != {EMB_SCHEMA_VERSION}")

        if emb["media_id"] != mid:
            fail(f"{mid}: media_id mismatch in artifact (got {emb['media_id']})")

        check_fields(emb["model"], REQUIRED_MODEL, "model")
        check_fields(emb["embed_config"], REQUIRED_CFG, "embed_config")

        runtime = emb["runtime"]
        check_fields(runtime, REQUIRED_RUNTIME_BASE, "runtime")

        backend = infer_backend(emb)
        if backend == "onnx":
            check_fields(runtime, REQUIRED_RUNTIME_ONNX, "runtime")
        elif backend == "torch":
            check_fields(runtime, REQUIRED_RUNTIME_TORCH, "runtime")
        else:
            fail(f"{mid}: cannot infer embedding backend from runtime; add runtime.backend or runtime.onnxruntime_version/torch_version")

        cfg_dump = json.dumps(emb["embed_config"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        cfg_sha = sha256_bytes(cfg_dump)
        if emb["embed_config_sha256"] != cfg_sha:
            fail(f"{mid}: embed_config_sha256 mismatch (got {emb['embed_config_sha256']}, expected {cfg_sha})")

        chunks_sha = sha256_file(chunks_path)
        if emb["chunks_artifact_sha256"] != chunks_sha:
            fail(f"{mid}: chunks_artifact_sha256 mismatch")

        vecs = emb["vectors"]
        if not isinstance(vecs, list):
            fail(f"{mid}: vectors is not a list")

        pref_passage = emb["embed_config"]["prefix_passage"]
        if not isinstance(pref_passage, str) or not pref_passage:
            fail(f"{mid}: prefix_passage invalid")

        dim = emb["model"]["dimension"]
        if not isinstance(dim, int) or dim <= 0:
            fail(f"{mid}: invalid model.dimension")

        # Determine whether vectors should exist based on *chunk records*, not outcome lines
        chunk_objs = load_chunks_jsonl(chunks_path)
        chunk_records = [c for c in chunk_objs if isinstance(c, dict) and c.get("record_type") != "chunking_outcome"]
        chunk_record_count = len(chunk_records)

        if chunk_record_count > 0 and len(vecs) == 0:
            fail(f"{mid}: chunks non-empty but vectors empty")

        if chunk_record_count == 0 and len(vecs) != 0:
            fail(f"{mid}: chunks empty but vectors non-empty")

        normalize = bool(emb["embed_config"].get("normalize_embeddings", False))
        checked_norm = False

        seen_chunk_ids = set()
        model_id = emb["model"]["model_id"]
        model_rev = emb["model"]["model_revision"] or ""
        cfg_sha_recorded = emb["embed_config_sha256"]

        for i, v in enumerate(vecs):
            if not isinstance(v, dict):
                fail(f"{mid}: vectors[{i}] not an object")
            check_fields(v, REQUIRED_VEC, f"vectors[{i}]")

            cid = v["chunk_id"]
            if cid in seen_chunk_ids:
                fail(f"{mid}: duplicate chunk_id in vectors: {cid}")
            seen_chunk_ids.add(cid)

            et = v["embed_text"]
            if not isinstance(et, str) or not et.startswith(pref_passage):
                fail(f"{mid}: vectors[{i}].embed_text missing passage prefix")

            vec = v["vector"]
            if not isinstance(vec, list) or len(vec) != dim:
                got = len(vec) if isinstance(vec, list) else "not-a-list"
                fail(f"{mid}: vectors[{i}].vector dim {got} != {dim}")

            for j, a in enumerate(vec):
                if not is_finite_number(a):
                    fail(f"{mid}: vectors[{i}].vector[{j}] not finite numeric")

            if not checked_norm:
                n = l2_norm(vec)
                if normalize:
                    if not (0.98 <= n <= 1.02):
                        fail(f"{mid}: normalize_embeddings=true but first vector L2 norm={n:.6f} not near 1.0")
                checked_norm = True

            material = (cid + "|" + model_id + "|" + model_rev + "|" + cfg_sha_recorded).encode("utf-8")
            expected_eid = sha256_bytes(material)
            if v["embedding_id"] != expected_eid:
                fail(f"{mid}: vectors[{i}].embedding_id mismatch")

        # Enforce one-to-one mapping when chunk records exist
        if chunk_record_count > 0:
            chunk_ids = [c.get("chunk_id") for c in chunk_records]
            if any((not isinstance(x, str) or not x) for x in chunk_ids):
                fail(f"{mid}: chunks contain missing/invalid chunk_id")
            if set(chunk_ids) != seen_chunk_ids:
                fail(f"{mid}: chunk_id set mismatch between chunks and vectors")

    print("OK:", len(paths), "artifact(s) checked")


if __name__ == "__main__":
    main()