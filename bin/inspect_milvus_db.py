#!/usr/bin/env python3
"""
Read-only Milvus Lite database inspector.
"""
import sys
from pymilvus import MilvusClient

def main():
    if len(sys.argv) != 2:
        print("usage: inspect_milvus_db.py <db_path>")
        sys.exit(2)

    db = sys.argv[1]
    c = MilvusClient(uri=db)
    cols = c.list_collections()

    print("collections:")
    for name in cols:
        stats = c.get_collection_stats(name)
        print(f"  {name}: {stats}")

if __name__ == "__main__":
    main()
