#!/usr/bin/env python3
from __future__ import annotations

import ast
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import psycopg
from qdrant_client import QdrantClient
from qdrant_client.models import PayloadSelectorInclude

SETTINGS_PATH = Path("/workspace/pipeline/config/settings.py")
QDRANT_URL = "http://localhost:6333"
TARGET_COLLECTIONS = ("text_sentence_qwen3", "text_turn_qwen3")
SCROLL_LIMIT = 256
EXPECTED_SHOT_ID_PREFIX = "f3ca5811"


@dataclass
class PatchStats:
    scanned: int = 0
    already_had_shot_id: int = 0
    missing_join_key: int = 0
    resolved: int = 0
    patched: int = 0
    unresolved: int = 0
    set_payload_errors: int = 0


def load_settings_module(settings_path: Path):
    if not settings_path.exists():
        raise FileNotFoundError(f"settings.py not found: {settings_path}")

    spec = importlib.util.spec_from_file_location("pipeline_settings", settings_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load settings module from {settings_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ast_literal_from_assignment(source: str, name: str) -> Optional[Any]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    try:
                        return ast.literal_eval(node.value)
                    except Exception:
                        return None
    return None


def dsn_from_mapping(cfg: Dict[str, Any]) -> Optional[str]:
    host = cfg.get("host") or cfg.get("HOST")
    port = cfg.get("port") or cfg.get("PORT") or 5432
    dbname = cfg.get("dbname") or cfg.get("database") or cfg.get("DBNAME") or cfg.get("DATABASE")
    user = cfg.get("user") or cfg.get("USER")
    password = cfg.get("password") or cfg.get("PASSWORD")

    if host and dbname and user is not None and password is not None:
        return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    return None


def extract_postgres_dsn(settings_module, settings_path: Path) -> str:
    candidate_names = [
        "POSTGRES_DSN",
        "DATABASE_URL",
        "PG_DSN",
        "SQLALCHEMY_DATABASE_URI",
        "DB_DSN",
    ]
    for name in candidate_names:
        value = getattr(settings_module, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    dict_names = ["DB_CONFIG", "POSTGRES", "DATABASE", "PG"]
    for name in dict_names:
        value = getattr(settings_module, name, None)
        if isinstance(value, dict):
            dsn = dsn_from_mapping(value)
            if dsn:
                return dsn

    source = settings_path.read_text(encoding="utf-8")
    for name in candidate_names:
        value = _ast_literal_from_assignment(source, name)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for name in dict_names:
        value = _ast_literal_from_assignment(source, name)
        if isinstance(value, dict):
            dsn = dsn_from_mapping(value)
            if dsn:
                return dsn

    raise RuntimeError(
        "Could not find a Postgres connection string in settings.py. "
        "Looked for common DSN variables and DB config dicts."
    )


def resolve_shot_id_for_turn(cur: psycopg.Cursor[Any], turn_id: str) -> Optional[str]:
    cur.execute(
        """
        SELECT tt.shot_id::text
        FROM transcript_turn AS tt
        WHERE tt.id = %s
        LIMIT 1
        """,
        (turn_id,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def resolve_shot_id_for_sentence(cur: psycopg.Cursor[Any], sentence_id: str) -> Optional[str]:
    try:
        cur.execute(
            """
            SELECT tt.shot_id::text
            FROM transcript_sentence AS ts
            JOIN transcript_turn AS tt
              ON tt.id = ts.turn_id
            WHERE ts.id = %s
            LIMIT 1
            """,
            (sentence_id,),
        )
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    except psycopg.Error:
        cur.connection.rollback()

    return resolve_shot_id_for_turn(cur, sentence_id)


def iter_points(client: QdrantClient, collection_name: str) -> Iterable[Any]:
    offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=None,
            limit=SCROLL_LIMIT,
            offset=offset,
            with_payload=PayloadSelectorInclude(include=["shot_id", "turn_id", "sentence_id"]),
            with_vectors=False,
        )
        for point in points:
            yield point
        if next_offset is None:
            break
        offset = next_offset


def patch_collection(client: QdrantClient, pg_conn: psycopg.Connection[Any], collection_name: str) -> PatchStats:
    stats = PatchStats()

    with pg_conn.cursor() as cur:
        for point in iter_points(client, collection_name):
            stats.scanned += 1
            payload = point.payload or {}

            if payload.get("shot_id"):
                stats.already_had_shot_id += 1
                continue

            turn_id = payload.get("turn_id")
            sentence_id = payload.get("sentence_id")

            if turn_id:
                shot_id = resolve_shot_id_for_turn(cur, str(turn_id))
            elif sentence_id:
                shot_id = resolve_shot_id_for_sentence(cur, str(sentence_id))
            else:
                stats.missing_join_key += 1
                continue

            if not shot_id:
                stats.unresolved += 1
                continue

            stats.resolved += 1

            if not shot_id.startswith(EXPECTED_SHOT_ID_PREFIX):
                print(
                    f"[warn] collection={collection_name} point_id={point.id} "
                    f"resolved shot_id={shot_id!r} does not start with expected prefix {EXPECTED_SHOT_ID_PREFIX!r}",
                    file=sys.stderr,
                )

            try:
                client.set_payload(
                    collection_name=collection_name,
                    payload={"shot_id": shot_id},
                    points=[point.id],
                )
                stats.patched += 1
            except Exception as exc:
                stats.set_payload_errors += 1
                print(
                    f"[error] collection={collection_name} point_id={point.id} failed set_payload: {exc!r}",
                    file=sys.stderr,
                )

    return stats


def main() -> int:
    settings_module = load_settings_module(SETTINGS_PATH)
    postgres_dsn = extract_postgres_dsn(settings_module, SETTINGS_PATH)
    client = QdrantClient(url=QDRANT_URL)

    total = PatchStats()

    print(f"Using settings: {SETTINGS_PATH}")
    print(f"Using Qdrant: {QDRANT_URL}")
    print("Target collections:", ", ".join(TARGET_COLLECTIONS))

    with psycopg.connect(postgres_dsn) as pg_conn:
        for collection_name in TARGET_COLLECTIONS:
            stats = patch_collection(client, pg_conn, collection_name)

            total.scanned += stats.scanned
            total.already_had_shot_id += stats.already_had_shot_id
            total.missing_join_key += stats.missing_join_key
            total.resolved += stats.resolved
            total.patched += stats.patched
            total.unresolved += stats.unresolved
            total.set_payload_errors += stats.set_payload_errors

            print()
            print(f"Collection: {collection_name}")
            print(f"  scanned:              {stats.scanned}")
            print(f"  already_had_shot_id:  {stats.already_had_shot_id}")
            print(f"  missing_join_key:     {stats.missing_join_key}")
            print(f"  resolved:             {stats.resolved}")
            print(f"  patched:              {stats.patched}")
            print(f"  unresolved:           {stats.unresolved}")
            print(f"  set_payload_errors:   {stats.set_payload_errors}")

    print()
    print("Total summary")
    print(f"  scanned:              {total.scanned}")
    print(f"  already_had_shot_id:  {total.already_had_shot_id}")
    print(f"  missing_join_key:     {total.missing_join_key}")
    print(f"  resolved:             {total.resolved}")
    print(f"  patched:              {total.patched}")
    print(f"  unresolved:           {total.unresolved}")
    print(f"  set_payload_errors:   {total.set_payload_errors}")

    return 0 if total.set_payload_errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
