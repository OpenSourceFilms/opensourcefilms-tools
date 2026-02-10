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
    ap.add_argument("--nodes", required=True)
    ap.add_argument("--mentions", required=True)
    args = ap.parse_args()

    nodes = load_jsonl(args.nodes)
    mentions = load_jsonl(args.mentions)

    node_out = [r for r in nodes if r.get("record_type") == "entity_link_outcome"]
    men_out = [r for r in mentions if r.get("record_type") == "entity_link_outcome"]

    if len(node_out) != 1 or len(men_out) != 1:
        print("FAIL: expected exactly 1 outcome in both files")
        return 2

    n_nodes = [r for r in nodes if r.get("record_type") == "entity_node"]
    n_mens = [r for r in mentions if r.get("record_type") == "entity_mention"]

    if int(node_out[0].get("node_count", -1)) != len(n_nodes):
        print("FAIL: node_count mismatch")
        return 2
    if int(node_out[0].get("mention_count", -1)) != len(n_mens):
        print("FAIL: mention_count mismatch")
        return 2

    # Spot-check referential integrity
    node_ids = {r.get("entity_node_id") for r in n_nodes}
    for m in n_mens:
        if m.get("entity_node_id") not in node_ids:
            print("FAIL: mention references missing node")
            return 2

    print("OK: stageF1 link artifacts pass structural checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())