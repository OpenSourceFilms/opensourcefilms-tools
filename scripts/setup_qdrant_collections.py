#!/usr/bin/env python3
"""
Create Qdrant collections for Phase 0.
Run once after Qdrant is started and after running setup_infrastructure.sh.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PayloadSchemaType,
    CreateAliasOperation,
)


COLLECTIONS = [
    {
        "name": "visual_keyframe_qwen3vl",
        "dim": settings.qwen3_vl_dim,
        "payload_indexes": ["asset_id", "shot_id", "project_id", "embedding_version", "deleted"],
    },
    {
        "name": "text_sentence_qwen3",
        "dim": settings.qwen3_text_dim,
        "payload_indexes": ["asset_id", "turn_id", "project_id", "embedding_version", "deleted"],
    },
    {
        "name": "ocr_span_qwen3",
        "dim": settings.qwen3_text_dim,
        "payload_indexes": ["asset_id", "keyframe_id", "project_id", "embedding_version", "deleted"],
    },
]


def main():
    client = QdrantClient(url=settings.qdrant_url)

    existing = {c.name for c in client.get_collections().collections}

    for col in COLLECTIONS:
        name = col["name"]
        if name in existing:
            print(f"  [skip] {name} already exists")
            continue

        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(
                size=col["dim"],
                distance=Distance.COSINE,
            ),
        )

        # Create payload indexes for fast filtering
        for field in col["payload_indexes"]:
            field_type = PayloadSchemaType.BOOL if field == "deleted" else PayloadSchemaType.KEYWORD
            client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=field_type,
            )

        info = client.get_collection(name)
        print(f"  [created] {name} dim={col['dim']} status={info.status}")

    print("Qdrant collections ready.")


if __name__ == "__main__":
    main()
