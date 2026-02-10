#!/usr/bin/env python3
import argparse
import json
import sys
from typing import Any, Dict, List


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                raise ValueError(f"blank line at {i}")
            rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", required=True)
    args = ap.parse_args()

    rows = load_jsonl(args.path)
    outcomes = [r for r in rows if r.get("record_type") == "entities_outcome"]
    if len(outcomes) != 1:
        print(f"FAIL: expected exactly 1 entities_outcome, got {len(outcomes)}")
        return 2
    outcome = outcomes[0]

    entities = [r for r in rows if r.get("record_type") == "entity_record"]
    entity_count = int(outcome.get("entity_count", -1))
    if entity_count != len(entities):
        print(f"FAIL: entity_count mismatch, outcome says {entity_count}, file has {len(entities)} entity_record lines")
        return 2

    # Minimal required fields
    for k in ["media_id", "ok", "skipped", "partial", "reasons", "input_identities", "entity_count"]:
        if k not in outcome:
            print(f"FAIL: outcome missing field {k}")
            return 2

    for e in entities:
        for k in ["media_id", "entity_id", "scope", "scope_id", "start_char", "end_char",
                  "entity_text", "entity_text_norm", "entity_type", "confidence", "extractor", "extractor_version",
                  "flags", "reasons", "evidence"]:
            if k not in e:
                print(f"FAIL: entity_record missing field {k}")
                return 2
        if int(e["end_char"]) <= int(e["start_char"]):
            print("FAIL: entity_record has non-positive span length")
            return 2

    print("OK: stageF0 entities artifact passes structural checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())