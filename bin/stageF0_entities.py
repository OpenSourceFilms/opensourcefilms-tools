#!/usr/bin/env python3
"""
Stage F0: deterministic entity mention extraction.

Contract intent (MASTER.md):
- Prefer event-scoped extraction when events exist and are valid, else fall back to chunks.
- Produce entities/<media_id>.jsonl with:
  - 0..N entity_record lines
  - exactly one entities_outcome line (always last)
- Deterministic, auditable, evidence-linked. No inference via absence.
- Atomic write, never zero-byte.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


FINGERPRINT_SCHEMA_VERSION = 1
FINGERPRINT_ID = "stageF0_entities_rules_v1"
FINGERPRINT_VERSION = "2026-02-02.1"

SCHEMA_VERSION = 1
RECORD_ENTITY = "entity_record"
RECORD_OUTCOME = "entities_outcome"

DEFAULT_CATALOG = "/workspace/catalog"
DEFAULT_OUT_DIR = f"{DEFAULT_CATALOG}/entities"
DEFAULT_CHUNKS_DIR = f"{DEFAULT_CATALOG}/chunks"
DEFAULT_EVENTS_DIR = f"{DEFAULT_CATALOG}/events"
DEFAULT_GAZETTEER = "/workspace/pipeline/resources/entities_gazetteer_v1.json"

EXTRACTOR = "rules_v1"
EXTRACTOR_VERSION = "63eb1c2d5b3a6385"

RE_CAPSEQ = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4})\b")

CAPSEQ_STOP = {
    "A", "An", "And", "But", "So", "Or", "The", "This", "That", "These", "Those",
    "I", "We", "You", "He", "She", "They", "It",
    "Ok", "Okay", "Yeah", "Yes", "No", "Right", "Sure", "Well", "Just",
    "If", "When", "Then", "Because", "Also", "Really",
}

RE_WS = re.compile(r"\s+")


def utc_now() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_self() -> str:
    return sha256_file(Path(os.path.abspath(__file__)))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
    os.replace(str(tmp), str(path))


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def norm_text(s: str) -> str:
    return RE_WS.sub(" ", (s or "").strip())


def build_entity_id(
    media_id: str,
    scope: str,
    scope_id: str,
    entity_type: str,
    start_char: int,
    end_char: int,
    entity_text_norm: str,
    extractor: str,
    extractor_version: str,
) -> str:
    canonical = "|".join(
        [
            "entity_v1",
            media_id,
            scope,
            scope_id,
            entity_type,
            str(start_char),
            str(end_char),
            entity_text_norm,
            extractor,
            extractor_version,
        ]
    )
    return sha256_bytes(canonical.encode("utf-8"))


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        out.append(json.loads(ln))
    return out


def load_gazetteer(path: Path) -> Dict[str, List[str]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "types" in raw and isinstance(raw["types"], dict):
        raw = raw["types"]
    out: Dict[str, List[str]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, list):
            phrases = [norm_text(x) for x in v if isinstance(x, str) and norm_text(x)]
            if phrases:
                out[k] = phrases
    return out


def capseq_occurrences(text: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for m in RE_CAPSEQ.finditer(text):
        raw = m.group(0)
        counts[raw] = counts.get(raw, 0) + 1
    return counts


def _is_sentence_initial(text: str, start_char: int) -> bool:
    if start_char <= 0:
        return True
    i = start_char - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    if i < 0:
        return True
    return text[i] in ".?!\n\r"


def extract_entities_from_text(
    media_id: str,
    scope: str,
    scope_id: str,
    text: str,
    gaz: Dict[str, List[str]],
    evidence: Dict[str, Any],
) -> List[Dict[str, Any]]:
    spans: List[Dict[str, Any]] = []
    if not text:
        return spans

    for entity_type, phrases in gaz.items():
        for phrase in phrases:
            if not phrase:
                continue
            pat = re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)
            for m in pat.finditer(text):
                start, end = m.start(), m.end()
                entity_text = text[start:end]
                entity_text_norm = entity_text.strip().lower()
                eid = build_entity_id(
                    media_id, scope, scope_id, entity_type, start, end, entity_text_norm, EXTRACTOR, EXTRACTOR_VERSION
                )
                spans.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "record_type": RECORD_ENTITY,
                        "media_id": media_id,
                        "entity_id": eid,
                        "scope": scope,
                        "scope_id": scope_id,
                        "start_char": start,
                        "end_char": end,
                        "entity_text": entity_text,
                        "entity_text_norm": entity_text_norm,
                        "entity_type": entity_type,
                        "confidence": 0.95,
                        "extractor": EXTRACTOR,
                        "extractor_version": EXTRACTOR_VERSION,
                        "tags": ["entity.derived"],
                        "flags": {
                            "ok": True,
                            "skipped": False,
                            "partial": False,
                            "blocked": False,
                            "invalid": False,
                            "degraded": False,
                        },
                        "reasons": {"OK": 1},
                        "evidence": dict(evidence),
                    }
                )

    occ = capseq_occurrences(text)
    for m in RE_CAPSEQ.finditer(text):
        raw = m.group(0)
        start, end = m.start(), m.end()

        if raw in CAPSEQ_STOP:
            continue
        if occ.get(raw, 0) <= 1 and _is_sentence_initial(text, start):
            continue

        entity_text = raw
        entity_text_norm = raw.strip().lower()

        eid = build_entity_id(
            media_id, scope, scope_id, "PROPER_NOUN", start, end, entity_text_norm, EXTRACTOR, EXTRACTOR_VERSION
        )
        spans.append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": RECORD_ENTITY,
                "media_id": media_id,
                "entity_id": eid,
                "scope": scope,
                "scope_id": scope_id,
                "start_char": start,
                "end_char": end,
                "entity_text": entity_text,
                "entity_text_norm": entity_text_norm,
                "entity_type": "PROPER_NOUN",
                "confidence": 0.55,
                "extractor": EXTRACTOR,
                "extractor_version": EXTRACTOR_VERSION,
                "tags": ["entity.derived"],
                "flags": {
                    "ok": True,
                    "skipped": False,
                    "partial": False,
                    "blocked": False,
                    "invalid": False,
                    "degraded": False,
                },
                "reasons": {"OK": 1},
                "evidence": dict(evidence),
            }
        )

    return spans


def events_are_valid(events_items: List[Dict[str, Any]]) -> bool:
    if not events_items:
        return False
    has_event = any(isinstance(x, dict) and x.get("record_type") == "event_record" for x in events_items)
    has_outcome = any(isinstance(x, dict) and x.get("record_type") == "events_outcome" for x in events_items)
    return bool(has_event and has_outcome)


def iter_event_scopes(events_items: List[Dict[str, Any]]) -> Iterable[Tuple[str, str, Dict[str, Any]]]:
    for ev in events_items:
        if not isinstance(ev, dict):
            continue
        if ev.get("record_type") != "event_record":
            continue
        scope_id = ev.get("event_id") or ""
        text = ev.get("event_text") or ""
        evidence = {}
        ev_evidence = ev.get("evidence") or {}
        if isinstance(ev_evidence, dict):
            evidence["chunk_ids"] = ev_evidence.get("chunk_ids") or []
            if "scene_index" in ev_evidence:
                evidence["scene_index"] = ev_evidence.get("scene_index")
            if "scene_t0_s" in ev_evidence:
                evidence["scene_t0_s"] = ev_evidence.get("scene_t0_s")
            if "scene_t1_s" in ev_evidence:
                evidence["scene_t1_s"] = ev_evidence.get("scene_t1_s")
        yield scope_id, str(text), evidence


def iter_chunk_scopes(chunks_items: List[Dict[str, Any]]) -> Iterable[Tuple[str, str, Dict[str, Any]]]:
    for ch in chunks_items:
        if not isinstance(ch, dict):
            continue
        if ch.get("record_type") != "chunk":
            continue
        scope_id = ch.get("chunk_id") or ""
        text = ch.get("text") or ""
        evidence = {"chunk_ids": [scope_id]}
        for k in ("scene_index", "scene_t0_s", "scene_t1_s"):
            if k in ch:
                evidence[k] = ch.get(k)
        yield scope_id, str(text), evidence


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--media_id", required=True, help="64-hex sha256 media_id")
    ap.add_argument("--chunks_dir", default=DEFAULT_CHUNKS_DIR)
    ap.add_argument("--events_dir", default=DEFAULT_EVENTS_DIR)
    ap.add_argument("--out_dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--gazetteer_path", default=DEFAULT_GAZETTEER)
    ap.add_argument("--prefer_events", action="store_true", help="prefer event-scoped extraction when valid")
    ap.add_argument("--max_entities_per_scope", type=int, default=5000)
    args = ap.parse_args()

    mid = args.media_id.strip()
    if len(mid) != 64 or any(c not in "0123456789abcdef" for c in mid):
        print("FAIL: media_id must be 64 lowercase hex", file=sys.stderr)
        raise SystemExit(2)

    chunks_path = Path(args.chunks_dir) / f"{mid}.jsonl"
    events_path = Path(args.events_dir) / f"{mid}.jsonl"
    out_path = Path(args.out_dir) / f"{mid}.jsonl"
    gaz_path = Path(args.gazetteer_path)

    chunks_items = load_jsonl(chunks_path) if chunks_path.exists() else []
    events_items = load_jsonl(events_path) if events_path.exists() else []

    gaz = load_gazetteer(gaz_path)
    gaz_sha = sha256_file(gaz_path) if gaz_path.exists() else ""

    use_events = False
    if args.prefer_events and events_path.exists() and events_are_valid(events_items):
        use_events = True

    entity_records: List[Dict[str, Any]] = []
    if use_events:
        for scope_id, text, evidence in iter_event_scopes(events_items):
            spans = extract_entities_from_text(mid, "event", scope_id, str(text), gaz, evidence)
            if args.max_entities_per_scope > 0:
                spans = spans[: args.max_entities_per_scope]
            entity_records.extend(spans)
    else:
        for scope_id, text, evidence in iter_chunk_scopes(chunks_items):
            spans = extract_entities_from_text(mid, "chunk", scope_id, str(text), gaz, evidence)
            if args.max_entities_per_scope > 0:
                spans = spans[: args.max_entities_per_scope]
            entity_records.extend(spans)

    chunks_sha = sha256_file(chunks_path) if chunks_path.exists() else ""
    events_sha = sha256_file(events_path) if events_path.exists() else ""

    # Fingerprint config (explicit semantics)
    fp_config = {
        "extractor": EXTRACTOR,
        "extractor_version": EXTRACTOR_VERSION,
        "gazetteer_sha256": gaz_sha,
        "capseq_regex": RE_CAPSEQ.pattern,
        "capseq_stop": sorted(list(CAPSEQ_STOP)),
        "prefer_events_flag": True,  # the stage supports it, caller chooses; semantics of extraction remain stable
    }
    fp_config_sha256 = sha256_bytes(canonical_json_bytes(fp_config))

    artifact_fingerprint = {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "stage": "F0",
        "producer": os.path.basename(__file__),
        "producer_sha256": sha256_self(),
        "fingerprint_id": FINGERPRINT_ID,
        "fingerprint_version": FINGERPRINT_VERSION,
        "config_sha256": fp_config_sha256,
        "config": fp_config,
    }

    outcome = {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_OUTCOME,
        "media_id": mid,
        "ok": True,
        "skipped": False,
        "partial": False,
        "reasons": {"OK": 1},
        "entity_count": len(entity_records),
        "artifact_fingerprint": artifact_fingerprint,
        "input_identities": {
            "chunks_path": str(chunks_path),
            "chunks_sha256": chunks_sha,
            "events_path": str(events_path),
            "events_present": bool(events_path.exists()),
            "events_sha256": events_sha,
            "gazetteer_path": str(gaz_path),
            "gazetteer_sha256": gaz_sha,
            "extractor": EXTRACTOR,
            "extractor_version": EXTRACTOR_VERSION,
            "created_utc": utc_now(),
        },
    }

    lines: List[str] = []
    for rec in entity_records:
        lines.append(json.dumps(rec, ensure_ascii=False))
    lines.append(json.dumps(outcome, ensure_ascii=False))

    atomic_write_text(out_path, "\n".join(lines) + "\n")
    print(f"OK: wrote {out_path} (entities={len(entity_records)}, mode={'events' if use_events else 'chunks'})")
    print("fingerprint_id:", artifact_fingerprint["fingerprint_id"], "fingerprint_version:", artifact_fingerprint["fingerprint_version"])
    print("producer_sha256:", artifact_fingerprint["producer_sha256"])
    print("config_sha256:", artifact_fingerprint["config_sha256"])


if __name__ == "__main__":
    main()