#!/usr/bin/env python3
#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Tuple, Optional

# Stage F1: Entity consolidation/linking
#
# Inputs:
#   - /workspace/catalog/entities/<media_id>.jsonl                 (F0-core)
#   - /workspace/noncanonical/entities_spacy/<media_id>.jsonl      (F0-spacy candidate, trf recommended)
#
# Outputs:
#   Default (noncanonical):
#     - /workspace/noncanonical/entity_linking/nodes/<media_id>.jsonl
#     - /workspace/noncanonical/entity_linking/mentions/<media_id>.jsonl
#
#   If --canonical_out is set:
#     - /workspace/catalog/entity_nodes/<media_id>.jsonl
#     - /workspace/catalog/entity_mentions/<media_id>.jsonl
#
# Deterministic: yes (pure string logic + fixed precedence + deterministic filtering)

DEFAULT_F0_CORE_DIR = "/workspace/catalog/entities"
DEFAULT_F0_SPACY_DIR = "/workspace/noncanonical/entities_spacy"

DEFAULT_NONCANON_OUT_ROOT = "/workspace/noncanonical/entity_linking"
DEFAULT_CANON_NODES_DIR = "/workspace/catalog/entity_nodes"
DEFAULT_CANON_MENTIONS_DIR = "/workspace/catalog/entity_mentions"

STOP_PROPER_NOUN = {
    "because", "cool", "dude", "yeah", "ok", "okay", "really", "right", "whatever",
    "anyway", "anyways", "holy", "roll", "put", "smoke", "smokey",
    "and", "but", "so", "the", "this", "that", "there", "here", "its", "it's", "thats", "that's",
}

STOP_QUANTITY = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "first", "second", "third", "fourth", "fifth",
    "an hour",
}

RE_HAS_DIGIT = re.compile(r"\d")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def atomic_write_text(path: str, text: str) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = f"{path}.tmp.{os.getpid()}.{int(time.time())}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def norm_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^[\W_]+|[\W_]+$", "", s)
    return s


def type_bucket(t: str) -> str:
    t = (t or "").upper()
    if t in {"PERSON", "ORG", "GPE", "LOC"}:
        return t
    if t in {"WORK_OF_ART", "EVENT"}:
        return t
    if t in {"PROP", "OBJECT"}:
        return "OBJECTLIKE"
    if t in {"DATE", "TIME", "MONEY", "QUANTITY"}:
        return "MEASURELIKE"
    return "OTHER"


def source_rank(m: Dict[str, Any]) -> int:
    tags = m.get("tags") or []
    conf = float(m.get("confidence", 0.0) or 0.0)
    etype = (m.get("entity_type") or "").upper()

    if isinstance(tags, list) and "entity.derived" in tags and conf >= 0.90 and etype not in {"PROPER_NOUN"}:
        return 0
    if isinstance(tags, list) and "entity.spacy" in tags:
        return 1
    if etype == "PROPER_NOUN":
        return 3
    return 2


def choose_node_type(mentions: List[Dict[str, Any]]) -> str:
    ranked = sorted(mentions, key=source_rank)
    for m in ranked:
        t = (m.get("entity_type") or "").upper()
        if t and t not in {"PROPER_NOUN"}:
            return t

    bucket_counts: Dict[str, int] = {}
    type_counts: Dict[str, int] = {}
    for m in mentions:
        t = (m.get("entity_type") or "").upper()
        b = type_bucket(t)
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
        type_counts[t] = type_counts.get(t, 0) + 1

    best_bucket = sorted(bucket_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    in_bucket = [(t, c) for t, c in type_counts.items() if type_bucket(t) == best_bucket]
    if in_bucket:
        best_type = sorted(in_bucket, key=lambda kv: (-kv[1], kv[0]))[0][0]
        return best_type or "PROPER_NOUN"

    return "PROPER_NOUN"


def build_node_id(media_id: str, key: str, chosen_type: str, mention_ids: List[str]) -> str:
    canonical = "|".join(["node_v1", media_id, key, chosen_type] + sorted(mention_ids))
    return sha256_bytes(canonical.encode("utf-8"))


def build_mention_id(m: Dict[str, Any]) -> str:
    media_id = m.get("media_id", "")
    scope = m.get("scope", "")
    scope_id = m.get("scope_id", "")
    s0 = str(int(m.get("start_char", 0) or 0))
    s1 = str(int(m.get("end_char", 0) or 0))
    t = (m.get("entity_type") or "").upper()
    n = norm_text(m.get("entity_text", "") or "")
    ex = m.get("extractor", "")
    ev = m.get("extractor_version", "")
    canonical = "|".join(["mention_v1", media_id, scope, scope_id, s0, s1, t, n, ex, ev])
    return sha256_bytes(canonical.encode("utf-8"))


def filter_mention(m: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    etype = (m.get("entity_type") or "").upper()
    text_norm = norm_text(m.get("entity_text", "") or "")

    if not text_norm:
        return False, "EMPTY_TEXT"
    if "inaudible" in text_norm:
        return False, "ASR_NOISE"

    if etype == "PROPER_NOUN":
        if text_norm in STOP_PROPER_NOUN:
            return False, "STOPWORD_PROPER_NOUN"

    if etype == "QUANTITY":
        if text_norm in STOP_QUANTITY:
            return False, "LOW_VALUE_QUANTITY"
        if not RE_HAS_DIGIT.search(text_norm) and len(text_norm) <= 5:
            return False, "LOW_VALUE_QUANTITY"

    if etype == "DATE":
        if not RE_HAS_DIGIT.search(text_norm) and len(text_norm) > 12:
            return False, "SUSPECT_DATE_NO_DIGITS"

    return True, None


def filter_node(display_norm: str, chosen_type: str, mention_count: int) -> Tuple[bool, Optional[str]]:
    t = (chosen_type or "").upper()

    if t == "PROPER_NOUN" and display_norm in STOP_PROPER_NOUN:
        return False, "STOPWORD_NODE_PROPER_NOUN"
    if t == "PROPER_NOUN" and mention_count < 2:
        return False, "WEAK_PROPER_NOUN_SINGLETON"

    return True, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--media_id", required=True)
    ap.add_argument("--f0_core_dir", default=DEFAULT_F0_CORE_DIR)
    ap.add_argument("--f0_spacy_dir", default=DEFAULT_F0_SPACY_DIR)
    ap.add_argument("--out_root", default=DEFAULT_NONCANON_OUT_ROOT)
    ap.add_argument("--canonical_out", action="store_true", default=False)
    args = ap.parse_args()

    media_id = args.media_id
    f0_core_path = os.path.join(args.f0_core_dir, f"{media_id}.jsonl")
    f0_spacy_path = os.path.join(args.f0_spacy_dir, f"{media_id}.jsonl")

    if args.canonical_out:
        out_nodes_path = os.path.join(DEFAULT_CANON_NODES_DIR, f"{media_id}.jsonl")
        out_mentions_path = os.path.join(DEFAULT_CANON_MENTIONS_DIR, f"{media_id}.jsonl")
        noncanonical_flag = False
    else:
        out_nodes_path = os.path.join(args.out_root, "nodes", f"{media_id}.jsonl")
        out_mentions_path = os.path.join(args.out_root, "mentions", f"{media_id}.jsonl")
        noncanonical_flag = True

    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    mentions: List[Dict[str, Any]] = []
    core_sha = sha256_file(f0_core_path)
    spacy_sha = sha256_file(f0_spacy_path) if os.path.exists(f0_spacy_path) else None

    if os.path.exists(f0_core_path):
        for r in load_jsonl(f0_core_path):
            if r.get("record_type") == "entity_record":
                r["_source"] = "f0_core"
                mentions.append(r)

    if os.path.exists(f0_spacy_path):
        for r in load_jsonl(f0_spacy_path):
            if r.get("record_type") == "entity_record":
                r["_source"] = "f0_spacy"
                mentions.append(r)

    if not mentions:
        outcome = {
            "schema_version": 1,
            "record_type": "entity_link_outcome",
            "media_id": media_id,
            "ok": True,
            "skipped": False,
            "partial": False,
            "reasons": {"NO_INPUT_MENTIONS": 1},
            "node_count": 0,
            "mention_count": 0,
            "filter_summary": {"dropped_mentions": 0, "dropped_nodes": 0, "reasons": {}},
            "input_identities": {
                "f0_core_path": f0_core_path,
                "f0_core_sha256": core_sha,
                "f0_spacy_path": f0_spacy_path,
                "f0_spacy_sha256": spacy_sha,
                "created_utc": now_utc,
            },
            "noncanonical": noncanonical_flag,
        }
        atomic_write_text(out_nodes_path, json.dumps(outcome) + "\n")
        atomic_write_text(out_mentions_path, json.dumps(outcome) + "\n")
        return 0

    seen = set()
    deduped: List[Dict[str, Any]] = []
    for m in mentions:
        mid = build_mention_id(m)
        if mid in seen:
            continue
        seen.add(mid)
        m["mention_id"] = mid
        deduped.append(m)

    filter_reasons: Dict[str, int] = {}
    filtered: List[Dict[str, Any]] = []
    dropped_mentions = 0
    for m in deduped:
        keep, reason = filter_mention(m)
        if keep:
            filtered.append(m)
        else:
            dropped_mentions += 1
            if reason:
                filter_reasons[reason] = filter_reasons.get(reason, 0) + 1

    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for m in filtered:
        txt = norm_text(m.get("entity_text", "") or "")
        if not txt:
            continue
        b = type_bucket((m.get("entity_type") or "").upper())
        key = f"{txt}|{b}"
        clusters.setdefault(key, []).append(m)

    nodes: List[Dict[str, Any]] = []
    mention_out: List[Dict[str, Any]] = []
    dropped_nodes = 0

    for key in sorted(clusters.keys()):
        ms = clusters[key]
        mention_ids = [m["mention_id"] for m in ms]
        chosen_type = choose_node_type(ms)

        surface_counts: Dict[str, int] = {}
        for m in ms:
            s = (m.get("entity_text") or "").strip()
            if s:
                surface_counts[s] = surface_counts.get(s, 0) + 1
        display = sorted(surface_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if surface_counts else key.split("|", 1)[0]
        display_norm = norm_text(display)

        keep_node, node_reason = filter_node(display_norm, chosen_type, len(mention_ids))
        if not keep_node:
            dropped_nodes += 1
            if node_reason:
                filter_reasons[node_reason] = filter_reasons.get(node_reason, 0) + 1
            continue

        node_id = build_node_id(media_id, key, chosen_type, mention_ids)

        nodes.append({
            "schema_version": 1,
            "record_type": "entity_node",
            "media_id": media_id,
            "entity_node_id": node_id,
            "entity_type": chosen_type,
            "display_name": display,
            "name_norm": display_norm,
            "mention_ids": sorted(mention_ids),
            "support": {
                "mention_count": len(mention_ids),
                "sources": sorted({m.get("_source", "") for m in ms}),
            },
            "provenance": {
                "created_utc": now_utc,
                "inputs": {"f0_core_sha256": core_sha, "f0_spacy_sha256": spacy_sha},
                "algorithm": "link_v1_filter_v1",
            },
            "noncanonical": noncanonical_flag,
        })

        for m in ms:
            mention_out.append({
                "schema_version": 1,
                "record_type": "entity_mention",
                "media_id": media_id,
                "mention_id": m["mention_id"],
                "entity_node_id": node_id,
                "scope": m.get("scope"),
                "scope_id": m.get("scope_id"),
                "start_char": m.get("start_char"),
                "end_char": m.get("end_char"),
                "entity_text": m.get("entity_text"),
                "entity_text_norm": norm_text(m.get("entity_text", "") or ""),
                "entity_type_observed": (m.get("entity_type") or "").upper(),
                "extractor": m.get("extractor"),
                "extractor_version": m.get("extractor_version"),
                "confidence": m.get("confidence"),
                "tags": m.get("tags", []),
                "evidence": m.get("evidence", {}),
                "source_stream": m.get("_source"),
                "noncanonical": noncanonical_flag,
            })

    node_outcome = {
        "schema_version": 1,
        "record_type": "entity_link_outcome",
        "media_id": media_id,
        "ok": True,
        "skipped": False,
        "partial": False,
        "reasons": {"OK": 1},
        "node_count": len(nodes),
        "mention_count": len(mention_out),
        "filter_summary": {
            "dropped_mentions": dropped_mentions,
            "dropped_nodes": dropped_nodes,
            "reasons": dict(sorted(filter_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        },
        "input_identities": {
            "f0_core_path": f0_core_path,
            "f0_core_sha256": core_sha,
            "f0_spacy_path": f0_spacy_path,
            "f0_spacy_sha256": spacy_sha,
            "created_utc": now_utc,
        },
        "noncanonical": noncanonical_flag,
    }

    nodes_lines = [json.dumps(r, ensure_ascii=False) for r in nodes] + [json.dumps(node_outcome, ensure_ascii=False)]
    mentions_lines = [json.dumps(r, ensure_ascii=False) for r in mention_out] + [json.dumps(node_outcome, ensure_ascii=False)]

    atomic_write_text(out_nodes_path, "\n".join(nodes_lines) + "\n")
    atomic_write_text(out_mentions_path, "\n".join(mentions_lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())