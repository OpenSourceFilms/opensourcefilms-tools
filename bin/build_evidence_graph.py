#!/usr/bin/env python3
"""
Build a media-local evidence graph from canonical artifacts.
"""
import sys
import json
import networkx as nx
from pathlib import Path

def main():
    if len(sys.argv) != 2:
        print("usage: build_evidence_graph.py <MEDIA_ID>")
        sys.exit(2)

    media_id = sys.argv[1]
    print(f"build_evidence_graph: stub for media_id={media_id}")

if __name__ == "__main__":
    main()
