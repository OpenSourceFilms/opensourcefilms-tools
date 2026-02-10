#!/usr/bin/env python3
# /workspace/pipeline/bin/inspect_manifest_duration.py
#
# Usage:
#   inspect_manifest_duration.py /path/to/manifest.json

import json
import sys
from pathlib import Path


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            yield from walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            yield from walk(v, p)
    else:
        yield path, obj


def main():
    if len(sys.argv) != 2:
        print("usage: inspect_manifest_duration.py <manifest.json>", file=sys.stderr)
        return 2

    p = Path(sys.argv[1])
    j = json.loads(p.read_text())

    hits = []
    for k, v in walk(j):
        kl = k.lower()
        if any(tok in kl for tok in ["duration", "length", "time"]):
            hits.append((k, v))

    print(f"manifest: {p}")
    print(f"duration-like keys found: {len(hits)}")
    for k, v in hits:
        print(f"- {k}: {v!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
