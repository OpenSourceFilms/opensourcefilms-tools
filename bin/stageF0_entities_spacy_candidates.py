#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Stage F0 Entities (spaCy candidate extractor)
#
# Runs in text_tools venv (Python 3.11 + spaCy model).
# Writes NON-CANONICAL candidate artifacts, by design.
#
# Output path default:
#   /workspace/noncanonical/entities_spacy/<media_id>.jsonl
#
# This avoids:
# - introducing new canonical write surfaces before contract updates
# - expanding record_type vocabulary in canonical artifacts prematurely
#
# Determinism:
# - spaCy NER is not guaranteed bitwise deterministic across versions/hardware.
# - We therefore treat this as a candidate stream.
# - Provenance pins spaCy version + model name + model meta.

DEFAULT_OUTDIR = "/workspace/noncanonical/entities_spacy"
DEFAULT_MODEL = "en_core_web_sm"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def atomic_write_text(path: str, text: str) -> None:
    d = os.path.dirname(path)
    ensure_dir(d)
    tmp = f"{path}.tmp.{os.getpid()}.{int(time.time())}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def normalize_entity_text(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def build_entity_id(media_id: str, scope: str, scope_id: str, entity_type: str, start_char: int, end_char: int,
                    entity_text_norm: str, extractor: str, extractor_version: str) -> str:
    canonical = "|".join([
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
    ])
    return sha256_bytes(canonical.encode("utf-8"))


def spacy_label_to_entity_type(label: str) -> str:
    # spaCy labels: PERSON, ORG, GPE, LOC, DATE, TIME, MONEY, QUANTITY, WORK_OF_ART, EVENT, PRODUCT, etc.
    # Keep close to spaCy, but preserve a controlled output.
    mapping = {
        "PERSON": "PERSON",
        "ORG": "ORG",
        "GPE": "GPE",
        "LOC": "LOC",
        "FAC": "LOC",
        "DATE": "DATE",
        "TIME": "TIME",
        "MONEY": "MONEY",
        "QUANTITY": "QUANTITY",
        "CARDINAL": "QUANTITY",
        "ORDINAL": "QUANTITY",
        "PERCENT": "QUANTITY",
        "WORK_OF_ART": "WORK_OF_ART",
        "EVENT": "EVENT",
        "PRODUCT": "OBJECT",
        "LAW": "WORK_OF_ART",
        "LANGUAGE": "MISC",
        "NORP": "GROUP",
    }
    return mapping.get(label, "MISC")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--media_id", required=True)
    ap.add_argument("--chunks_dir", default="/workspace/catalog/chunks")
    ap.add_argument("--events_dir", default="/workspace/catalog/events")
    ap.add_argument("--out_dir", default=DEFAULT_OUTDIR)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prefer_events", action="store_true", default=True)
    ap.add_argument("--max_entities_per_scope", type=int, default=8000)
    args = ap.parse_args()

    media_id = args.media_id
    chunks_path = os.path.join(args.chunks_dir, f"{media_id}.jsonl")
    events_path = os.path.join(args.events_dir, f"{media_id}.jsonl")
    out_path = os.path.join(args.out_dir, f"{media_id}.jsonl")

    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        import spacy  # type: ignore
    except Exception as e:
        # Outcome-only artifact
        outcome = {
            "schema_version": 1,
            "record_type": "entities_outcome",
            "media_id": media_id,
            "ok": False,
            "skipped": True,
            "partial": False,
            "reasons": {"TOOL_DEPENDENCY_MISSING": 1},
            "entity_count": 0,
            "input_identities": {
                "chunks_path": chunks_path,
                "chunks_sha256": sha256_file(chunks_path),
                "events_path": events_path,
                "events_present": os.path.exists(events_path),
                "events_sha256": sha256_file(events_path) if os.path.exists(events_path) else None,
                "extractor": "spacy",
                "extractor_version": "unknown",
                "created_utc": now_utc,
                "error": repr(e),
            },
            "noncanonical": True,
        }
        atomic_write_text(out_path, json.dumps(outcome, ensure_ascii=False) + "\n")
        return 0

    spacy_version = spacy.__version__
    try:
        nlp = spacy.load(args.model)
    except Exception as e:
        outcome = {
            "schema_version": 1,
            "record_type": "entities_outcome",
            "media_id": media_id,
            "ok": False,
            "skipped": True,
            "partial": False,
            "reasons": {"MODEL_MISSING": 1},
            "entity_count": 0,
            "input_identities": {
                "chunks_path": chunks_path,
                "chunks_sha256": sha256_file(chunks_path),
                "events_path": events_path,
                "events_present": os.path.exists(events_path),
                "events_sha256": sha256_file(events_path) if os.path.exists(events_path) else None,
                "extractor": "spacy",
                "extractor_version": spacy_version,
                "spacy_model": args.model,
                "created_utc": now_utc,
                "error": repr(e),
            },
            "noncanonical": True,
        }
        atomic_write_text(out_path, json.dumps(outcome, ensure_ascii=False) + "\n")
        return 0

    # Model meta hash (best-effort)
    model_meta = getattr(nlp, "meta", {}) if hasattr(nlp, "meta") else {}
    model_meta_json = json.dumps(model_meta, sort_keys=True, ensure_ascii=False)
    model_meta_sha = sha256_bytes(model_meta_json.encode("utf-8"))[:16]

    extractor = "spacy_ner"
    extractor_version = f"{spacy_version}:{args.model}:{model_meta_sha}"

    # Preconditions: chunks required
    if not os.path.exists(chunks_path):
        outcome = {
            "schema_version": 1,
            "record_type": "entities_outcome",
            "media_id": media_id,
            "ok": False,
            "skipped": True,
            "partial": False,
            "reasons": {"MISSING_INPUT": 1},
            "entity_count": 0,
            "input_identities": {
                "chunks_path": chunks_path,
                "chunks_sha256": None,
                "events_path": events_path,
                "events_present": False,
                "events_sha256": None,
                "extractor": extractor,
                "extractor_version": extractor_version,
                "spacy_model_meta": model_meta,
                "created_utc": now_utc,
            },
            "noncanonical": True,
        }
        atomic_write_text(out_path, json.dumps(outcome, ensure_ascii=False) + "\n")
        return 0

    chunks_sha = sha256_file(chunks_path)

    # Load chunks into dict for policy checks / evidence resolution
    chunk_rows = load_jsonl(chunks_path)
    chunk_by_id: Dict[str, Dict[str, Any]] = {}
    for r in chunk_rows:
        if r.get("record_type") != "chunk":
            continue
        cid = r.get("chunk_id")
        if isinstance(cid, str) and cid:
            chunk_by_id[cid] = r

    scopes: List[Tuple[str, str, str, Dict[str, Any]]] = []
    used_events = False

    if args.prefer_events and os.path.exists(events_path):
        try:
            ev_rows = load_jsonl(events_path)
            event_records = [r for r in ev_rows if r.get("record_type") == "event_record"]
            if event_records:
                used_events = True
                for er in event_records:
                    eid = er.get("event_id")
                    etext = er.get("event_text")
                    if not isinstance(eid, str) or not eid:
                        continue
                    if not isinstance(etext, str) or not etext.strip():
                        continue
                    evidence = er.get("evidence") if isinstance(er.get("evidence"), dict) else {}
                    scopes.append(("event", eid, etext, evidence))
        except Exception:
            used_events = False

    if not used_events:
        for cid, r in chunk_by_id.items():
            text = r.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            evidence = {"chunk_ids": [cid], "start_s": r.get("start_s"), "end_s": r.get("end_s")}
            scopes.append(("chunk", cid, text, evidence))

    entity_records: List[Dict[str, Any]] = []

    for scope, scope_id, text, evidence in scopes:
        # Policy gating
        allow_indexing = True
        if scope == "chunk":
            c = chunk_by_id.get(scope_id)
            pol = c.get("policy") if isinstance(c, dict) else None
            if isinstance(pol, dict) and pol.get("allow_indexing") is False:
                allow_indexing = False
        elif scope == "event":
            cids = evidence.get("chunk_ids") if isinstance(evidence, dict) else None
            if isinstance(cids, list) and cids:
                any_allowed = False
                for cid in cids:
                    cr = chunk_by_id.get(cid)
                    pol = cr.get("policy") if isinstance(cr, dict) else None
                    if not isinstance(pol, dict):
                        any_allowed = True
                        break
                    if pol.get("allow_indexing") is not False:
                        any_allowed = True
                        break
                allow_indexing = any_allowed
        if not allow_indexing:
            continue

        doc = nlp(text)
        for ent in doc.ents:
            if not ent.text or not ent.text.strip():
                continue
            start_char = int(ent.start_char)
            end_char = int(ent.end_char)
            if end_char <= start_char:
                continue
            etype = spacy_label_to_entity_type(ent.label_)
            raw = ent.text
            norm = normalize_entity_text(raw)
            eid = build_entity_id(media_id, scope, scope_id, etype, start_char, end_char, norm, extractor, extractor_version)
            entity_records.append({
                "schema_version": 1,
                "record_type": "entity_record",
                "media_id": media_id,
                "entity_id": eid,
                "scope": scope,
                "scope_id": scope_id,
                "start_char": start_char,
                "end_char": end_char,
                "entity_text": raw,
                "entity_text_norm": norm,
                "entity_type": etype,
                "confidence": 0.80,
                "extractor": extractor,
                "extractor_version": extractor_version,
                "tags": ["entity.candidate", "entity.spacy"],
                "flags": {
                    "ok": True,
                    "skipped": False,
                    "partial": False,
                    "blocked": False,
                    "invalid": False,
                    "degraded": False,
                },
                "reasons": {"OK": 1},
                "evidence": evidence,
            })

        if len(entity_records) >= args.max_entities_per_scope:
            break

    # Stable sort
    entity_records = sorted(
        entity_records,
        key=lambda r: (r.get("scope", ""), r.get("scope_id", ""), int(r.get("start_char", 0)), int(r.get("end_char", 0)),
                       r.get("entity_type", ""), r.get("entity_text_norm", "")),
    )

    outcome = {
        "schema_version": 1,
        "record_type": "entities_outcome",
        "media_id": media_id,
        "ok": True,
        "skipped": False,
        "partial": False,
        "reasons": {"OK": 1} if entity_records else {"NO_ENTITIES_DETECTED": 1},
        "entity_count": len(entity_records),
        "input_identities": {
            "chunks_path": chunks_path,
            "chunks_sha256": chunks_sha,
            "events_path": events_path,
            "events_present": bool(used_events),
            "events_sha256": sha256_file(events_path) if os.path.exists(events_path) else None,
            "extractor": extractor,
            "extractor_version": extractor_version,
            "spacy_version": spacy_version,
            "spacy_model": args.model,
            "spacy_model_meta": model_meta,
            "created_utc": now_utc,
        },
        "noncanonical": True,
    }

    lines: List[str] = []
    for r in entity_records:
        lines.append(json.dumps(r, ensure_ascii=False))
    lines.append(json.dumps(outcome, ensure_ascii=False))

    atomic_write_text(out_path, "\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())