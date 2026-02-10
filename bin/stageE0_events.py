#!/usr/bin/env python3
import argparse
import hashlib
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

CATALOG = "/workspace/catalog"
CHUNKS_DIR = f"{CATALOG}/chunks"
SCENES_DIR = f"{CATALOG}/scenes"
MOTION_DIR = f"{CATALOG}/motion"
EVENTS_DIR = f"{CATALOG}/events"
EMB_DIR = f"{CATALOG}/embeddings"

VECTOR_DIM = 384

DEFAULT_SIM_THRESHOLD = 0.80
DEFAULT_MAX_EVENT_S = 120.0
DEFAULT_MAX_CHUNKS = 6

# Uniform artifact fingerprint
FINGERPRINT_SCHEMA_VERSION = 1
FINGERPRINT_ID = "stageE0_events_text_first_v1"
FINGERPRINT_VERSION = "2026-02-02.1"


def die(msg: str, rc: int = 2) -> None:
    print("FATAL:", msg, file=sys.stderr)
    sys.exit(rc)


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def build_artifact_fingerprint(sim_threshold: float, max_event_s: float, max_chunks: int) -> Dict[str, Any]:
    """
    Explicit, uniform fingerprint for deprecated-tool detection.

    Config includes semantic knobs and stable algorithm descriptors.
    Excludes data-dependent signals (presence of scenes/motion) and operational details.
    """
    cfg = {
        "vector_dim": VECTOR_DIM,
        "sim_threshold": float(sim_threshold),
        "max_event_s": float(max_event_s),
        "max_chunks": int(max_chunks),
        "split_rule": "split_on_cos_sim_drop_or_duration_or_count",
        "missing_vector_policy": "no_forced_split_inherits_current_segment",
        "event_text_policy": "text_first_concat_chunk_texts",
        "event_type_default": "unknown",
    }
    cfg_sha = sha256_bytes(canonical_json_bytes(cfg))
    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "stage": "E0",
        "producer": os.path.basename(__file__),
        "producer_sha256": sha256_self(),
        "fingerprint_id": FINGERPRINT_ID,
        "fingerprint_version": FINGERPRINT_VERSION,
        "config_sha256": cfg_sha,
        "config": cfg,
    }


def atomic_write_text(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path: str, ctx: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        die(f"missing {ctx}: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:
        die(f"failed to read JSON for {ctx} at {path}: {type(e).__name__}: {e}")
    if not isinstance(obj, dict):
        die(f"{ctx} is not a JSON object: {path}")
    return obj


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
                die(f"invalid JSON in {ctx} at {path}:{ln}: {type(e).__name__}: {e}")
            if not isinstance(obj, dict):
                die(f"non-object JSON in {ctx} at {path}:{ln}")
            rows.append(obj)
    if not rows:
        die(f"{ctx} has no records (blank/empty forbidden): {path}")
    return rows


def sec_to_ms(s: float) -> int:
    return int(round(float(s) * 1000.0))


def clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else v


def choose_scene_for_time(scene_ts: List[Tuple[float, float]], t_s: float) -> Optional[int]:
    for i, (a, b) in enumerate(scene_ts):
        if t_s >= a and t_s <= b:
            return i
    return None


def build_event_id(mid: str, t0_ms: int, t1_ms: int, chunk_ids: List[str], scenes_sha: str, chunks_sha: str) -> str:
    h = hashlib.sha256()
    h.update(mid.encode("utf-8"))
    h.update(b"|")
    h.update(str(t0_ms).encode("utf-8"))
    h.update(b"|")
    h.update(str(t1_ms).encode("utf-8"))
    h.update(b"|")
    h.update(chunks_sha.encode("utf-8"))
    h.update(b"|")
    h.update(scenes_sha.encode("utf-8"))
    h.update(b"|")
    for cid in chunk_ids:
        h.update(cid.encode("utf-8"))
        h.update(b",")
    return h.hexdigest()


def cos_sim(a: List[float], b: List[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def load_vec_map_from_b1(mid: str) -> Dict[str, List[float]]:
    """
    Load embeddings/<mid>.json and return chunk_id -> vector for eligible vectors.
    """
    emb_path = f"{EMB_DIR}/{mid}.json"
    if not os.path.isfile(emb_path):
        return {}
    obj = load_json(emb_path, "embeddings")
    vecs = obj.get("vectors", [])
    if not isinstance(vecs, list):
        return {}
    m: Dict[str, List[float]] = {}
    for r in vecs:
        elig = (r.get("eligibility") or {})
        if not isinstance(elig, dict) or not bool(elig.get("eligible", False)):
            continue
        cid = r.get("chunk_id")
        v = r.get("vector")
        if not isinstance(cid, str) or not isinstance(v, list) or len(v) != VECTOR_DIM:
            continue
        m[cid] = v
    return m


def group_chunks_by_scene(chunks: List[Dict[str, Any]], scene_ts: List[Tuple[float, float]]) -> Dict[int, List[Dict[str, Any]]]:
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for ch in chunks:
        a = float(ch.get("start_s", 0.0))
        b = float(ch.get("end_s", 0.0))
        mid_t = (a + b) * 0.5
        idx = choose_scene_for_time(scene_ts, mid_t)
        if idx is None:
            best_i = None
            best_d = None
            for i, (sa, sb) in enumerate(scene_ts):
                sm = (sa + sb) * 0.5
                d = abs(mid_t - sm)
                if best_d is None or d < best_d:
                    best_d = d
                    best_i = i
            if best_i is None:
                continue
            idx = int(best_i)
        groups.setdefault(idx, []).append(ch)
    for k in list(groups.keys()):
        groups[k].sort(key=lambda x: float(x.get("start_s", 0.0)))
    return groups


def split_chunks_into_events(
    rows: List[Dict[str, Any]],
    vec_map: Dict[str, List[float]],
    sim_threshold: float,
    max_event_s: float,
    max_chunks: int,
) -> List[List[Dict[str, Any]]]:
    """
    Deterministic segmentation over a sequence of chunks.
    Split when cosine similarity between consecutive chunk vectors drops below threshold,
    OR when event duration exceeds max_event_s,
    OR when chunk count exceeds max_chunks.
    If a chunk lacks a vector, it does NOT force split; it inherits the current segment.
    """
    if not rows:
        return []

    out: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = [rows[0]]

    def cur_start() -> float:
        return float(cur[0].get("start_s", 0.0))

    def cur_end() -> float:
        return float(cur[-1].get("end_s", 0.0))

    for i in range(1, len(rows)):
        prev = rows[i - 1]
        nxt = rows[i]

        duration = max(0.0, cur_end() - cur_start())
        too_long = duration >= float(max_event_s)
        too_many = len(cur) >= int(max_chunks)

        split = False
        if too_long or too_many:
            split = True
        else:
            pcid = prev.get("chunk_id")
            ncid = nxt.get("chunk_id")
            pv = vec_map.get(pcid) if isinstance(pcid, str) else None
            nv = vec_map.get(ncid) if isinstance(ncid, str) else None
            if pv is not None and nv is not None:
                s = cos_sim(pv, nv)
                if s < float(sim_threshold):
                    split = True

        if split:
            out.append(cur)
            cur = [nxt]
        else:
            cur.append(nxt)

    if cur:
        out.append(cur)

    return out


def make_event_record(
    mid: str,
    rows: List[Dict[str, Any]],
    scene_idx: Optional[int],
    scene_bounds: Optional[Tuple[float, float]],
    chunks_sha: str,
    scenes_sha: str,
    tags: List[str],
    reasons: Dict[str, int],
) -> Optional[Dict[str, Any]]:
    if not rows:
        return None

    c0 = min(float(r.get("start_s", 0.0)) for r in rows)
    c1 = max(float(r.get("end_s", 0.0)) for r in rows)
    if scene_bounds is not None:
        s0, s1 = scene_bounds
        t0_s = max(c0, s0)
        t1_s = min(c1, s1)
    else:
        t0_s, t1_s = c0, c1

    t0_ms = sec_to_ms(clamp01(t0_s))
    t1_ms = sec_to_ms(clamp01(t1_s))

    chunk_ids = [str(r.get("chunk_id")) for r in rows if r.get("chunk_id")]
    chunk_ids = [c for c in chunk_ids if c]
    if not chunk_ids:
        return None

    eid = build_event_id(mid, t0_ms, t1_ms, chunk_ids, scenes_sha, chunks_sha)

    texts = [str(r.get("text", "")).strip() for r in rows]
    texts = [t for t in texts if t]
    event_text = " ".join(texts)

    evidence: Dict[str, Any] = {"chunk_ids": chunk_ids}
    if scene_idx is not None:
        evidence["scene_index"] = int(scene_idx)
    if scene_bounds is not None:
        evidence["scene_t0_s"] = float(scene_bounds[0])
        evidence["scene_t1_s"] = float(scene_bounds[1])

    ev = {
        "schema_version": 1,
        "record_type": "event_record",
        "media_id": mid,
        "event_id": eid,
        "t0_ms": t0_ms,
        "t1_ms": t1_ms,
        "event_type": "unknown",
        "event_text": event_text,
        "tags": list(tags),
        "flags": {"ok": True, "skipped": False, "partial": False, "blocked": False, "invalid": False, "degraded": False},
        "reasons": dict(reasons),
        "evidence": evidence,
        "provenance": {
            "created_utc": now_utc_iso(),
            "source": {
                "chunks_sha256": chunks_sha,
                "scenes_sha256": (None if scenes_sha == "MISSING" else scenes_sha),
            },
        },
    }
    return ev


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage E0: derive canonical events from chunks, scenes, motion (optional).")
    ap.add_argument("--mid", required=True)
    ap.add_argument("--out", default=None, help="override output path (absolute), default /workspace/catalog/events/<mid>.jsonl")
    ap.add_argument("--sim_threshold", type=float, default=DEFAULT_SIM_THRESHOLD)
    ap.add_argument("--max_event_s", type=float, default=DEFAULT_MAX_EVENT_S)
    ap.add_argument("--max_chunks", type=int, default=DEFAULT_MAX_CHUNKS)
    args = ap.parse_args()

    mid = args.mid
    out_path = args.out if args.out else f"{EVENTS_DIR}/{mid}.jsonl"
    if not os.path.isabs(out_path):
        die("--out must be absolute if provided")

    chunks_path = f"{CHUNKS_DIR}/{mid}.jsonl"
    scenes_path = f"{SCENES_DIR}/{mid}.json"
    motion_path = f"{MOTION_DIR}/{mid}.json"
    emb_path = f"{EMB_DIR}/{mid}.json"

    if not os.path.isdir(EVENTS_DIR):
        die(f"events dir missing (must be provisioned): {EVENTS_DIR}")

    # Build fingerprint once per run and attach to outcome.
    artifact_fingerprint = build_artifact_fingerprint(
        sim_threshold=float(args.sim_threshold),
        max_event_s=float(args.max_event_s),
        max_chunks=int(args.max_chunks),
    )

    input_identities: Dict[str, Any] = {}

    # Chunks required
    if not os.path.isfile(chunks_path):
        outcome = {
            "schema_version": 1,
            "record_type": "events_outcome",
            "media_id": mid,
            "ok": False,
            "skipped": True,
            "partial": False,
            "reasons": {"MISSING_INPUT": 1},
            "artifact_fingerprint": artifact_fingerprint,
            "input_identities": {"chunks_path": chunks_path, "chunks_sha256": None},
            "event_count": 0,
        }
        atomic_write_text(out_path, json.dumps(outcome, ensure_ascii=False) + "\n")
        die(f"missing chunks input: {chunks_path}")

    chunks_sha = sha256_file(chunks_path)
    input_identities["chunks_path"] = chunks_path
    input_identities["chunks_sha256"] = chunks_sha

    chunks_rows = load_jsonl(chunks_path, "chunks")
    eligible_chunks: List[Dict[str, Any]] = []
    for ch in chunks_rows:
        if ch.get("record_type") != "chunk":
            continue
        pol = ch.get("policy") or {}
        if pol.get("allow_indexing") is False or pol.get("allow_serving") is False:
            continue
        eligible_chunks.append(ch)

    if not eligible_chunks:
        outcome = {
            "schema_version": 1,
            "record_type": "events_outcome",
            "media_id": mid,
            "ok": True,
            "skipped": False,
            "partial": False,
            "reasons": {"NO_ELIGIBLE_SPANS": 1},
            "artifact_fingerprint": artifact_fingerprint,
            "input_identities": input_identities,
            "event_count": 0,
        }
        atomic_write_text(out_path, json.dumps(outcome, ensure_ascii=False) + "\n")
        return

    # Embeddings are optional for splitting, but strongly preferred.
    vec_map = load_vec_map_from_b1(mid)
    input_identities["embeddings_path"] = emb_path
    input_identities["embeddings_present"] = bool(vec_map)

    have_scenes = os.path.isfile(scenes_path)
    scenes_sha = "MISSING"
    scene_ts: List[Tuple[float, float]] = []

    if have_scenes:
        scenes_sha = sha256_file(scenes_path)
        input_identities["scenes_path"] = scenes_path
        input_identities["scenes_sha256"] = scenes_sha
        sj = load_json(scenes_path, "scenes")
        ts = sj.get("scene_timestamps")
        if isinstance(ts, list) and ts:
            for pair in ts:
                if isinstance(pair, list) and len(pair) == 2:
                    scene_ts.append((float(pair[0]), float(pair[1])))

    if not have_scenes or not scene_ts:
        input_identities["scenes_path"] = scenes_path
        input_identities["scenes_sha256"] = None
        scenes_sha = "MISSING"

    if os.path.isfile(motion_path):
        input_identities["motion_path"] = motion_path
        input_identities["motion_sha256"] = sha256_file(motion_path)
    else:
        input_identities["motion_path"] = motion_path
        input_identities["motion_sha256"] = None

    all_events: List[Dict[str, Any]] = []

    base_tags = ["event.derived", "event.text_first"]
    base_reasons = {"OK": 1}

    if have_scenes and scene_ts:
        groups = group_chunks_by_scene(eligible_chunks, scene_ts)
        for scene_idx in sorted(groups.keys()):
            rows = groups[scene_idx]
            bounds = scene_ts[scene_idx] if scene_idx < len(scene_ts) else None
            segments = split_chunks_into_events(
                rows,
                vec_map=vec_map,
                sim_threshold=float(args.sim_threshold),
                max_event_s=float(args.max_event_s),
                max_chunks=int(args.max_chunks),
            )
            for seg in segments:
                ev = make_event_record(
                    mid=mid,
                    rows=seg,
                    scene_idx=scene_idx,
                    scene_bounds=bounds,
                    chunks_sha=chunks_sha,
                    scenes_sha=scenes_sha,
                    tags=base_tags,
                    reasons=base_reasons,
                )
                if ev is not None:
                    all_events.append(ev)
    else:
        tags = ["event.derived", "event.text_first", "event.fallback_no_scenes"]
        reasons = {"MISSING_INPUT": 1}
        eligible_chunks.sort(key=lambda x: float(x.get("start_s", 0.0)))
        segments = split_chunks_into_events(
            eligible_chunks,
            vec_map=vec_map,
            sim_threshold=float(args.sim_threshold),
            max_event_s=float(args.max_event_s),
            max_chunks=int(args.max_chunks),
        )
        for seg in segments:
            ev = make_event_record(
                mid=mid,
                rows=seg,
                scene_idx=None,
                scene_bounds=None,
                chunks_sha=chunks_sha,
                scenes_sha="MISSING",
                tags=tags,
                reasons=reasons,
            )
            if ev is not None:
                all_events.append(ev)

    if not all_events:
        outcome = {
            "schema_version": 1,
            "record_type": "events_outcome",
            "media_id": mid,
            "ok": True,
            "skipped": False,
            "partial": False,
            "reasons": {"ZERO_EVENTS_VALID": 1},
            "artifact_fingerprint": artifact_fingerprint,
            "input_identities": input_identities,
            "event_count": 0,
        }
        atomic_write_text(out_path, json.dumps(outcome, ensure_ascii=False) + "\n")
        return

    outcome = {
        "schema_version": 1,
        "record_type": "events_outcome",
        "media_id": mid,
        "ok": True,
        "skipped": False,
        "partial": False,
        "reasons": {"OK": 1},
        "artifact_fingerprint": artifact_fingerprint,
        "input_identities": input_identities,
        "event_count": len(all_events),
    }

    lines: List[str] = []
    for ev in all_events:
        lines.append(json.dumps(ev, ensure_ascii=False))
    lines.append(json.dumps(outcome, ensure_ascii=False))
    atomic_write_text(out_path, "\n".join(lines) + "\n")


if __name__ == "__main__":
    main()