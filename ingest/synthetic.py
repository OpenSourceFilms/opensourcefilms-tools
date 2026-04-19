"""
Stage 7: generate_synthetic_queries(asset_id)

Runs in the embed worker (qwen3_embed_clean venv).
For each shot and each substantial transcript_turn, calls the configured LLM
to generate hypothetical editor search queries, then embeds them with
Qwen3-Embedding-8B and writes to Qdrant synthetic_query_qwen3.

Runs after the enrich barrier fires (asset is already complete). Additive —
does not change asset state.
"""
import logging
import uuid

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Shot, TranscriptTurn, ObjectTrack, SyntheticQuery

logger = logging.getLogger(__name__)

PRODUCER_MODEL = f"ollama/{settings.ollama_model}" if settings.synthetic_query_llm == "ollama" \
    else f"anthropic/claude-haiku-4-5-20251001" if settings.synthetic_query_llm == "anthropic" \
    else f"local/{settings.synthetic_query_local_model}"
PRODUCER_VERSION = "1.0"
COLLECTION = "synthetic_query_qwen3"
TEXT_DIM = settings.qwen3_text_dim

_SHOT_PROMPT = """\
You are helping build a video retrieval system for a film production.
Given a description of a video shot, generate {n} short search queries that an editor might type when looking for this clip.
Each query should be 4-10 words, natural language, varied in angle (visual description, action, mood, context).
Return only the queries, one per line, no numbering, no explanation.

Shot description:
{context}"""

_TURN_PROMPT = """\
You are helping build a video retrieval system for a film production.
Given a transcript excerpt, generate {n} short search queries an editor might type to find this dialogue moment.
Each query should be 4-10 words. Return only the queries, one per line, no numbering, no explanation.

Transcript:
{context}"""


def _parse_queries(raw: str, max_n: int) -> list[str]:
    """Extract non-empty lines from LLM output, up to max_n."""
    lines = [l.strip().strip('"\'').strip() for l in raw.splitlines()]
    return [l for l in lines if 4 <= len(l) <= 200][:max_n]


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with Qwen3-Embedding-8B (sentence-transformers)."""
    import torch
    from sentence_transformers import SentenceTransformer
    from ingest.gpu_pod_manager import gpu_available

    if not hasattr(_embed_texts, "_model"):
        device = "cuda" if gpu_available() else "cpu"
        dtype = torch.float16 if device == "cuda" else torch.float32
        _embed_texts._model = SentenceTransformer(
            settings.qwen3_text_model,
            model_kwargs={"dtype": dtype},
            trust_remote_code=True,
        ).to(device)
        logger.info("[synthetic] loaded text model %s", settings.qwen3_text_model)

    vecs = _embed_texts._model.encode(
        texts,
        normalize_embeddings=True,
        prompt_name="query",
        batch_size=settings.embed_batch_size,
        show_progress_bar=False,
    )
    return vecs.tolist()


def _ensure_qdrant_collection(client):
    from qdrant_client.models import VectorParams, Distance, PayloadSchemaType
    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=TEXT_DIM, distance=Distance.COSINE),
        )
        for field in ["asset_id", "parent_id", "parent_type", "project_id"]:
            client.create_payload_index(
                collection_name=COLLECTION,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        client.create_payload_index(
            collection_name=COLLECTION,
            field_name="deleted",
            field_schema=PayloadSchemaType.BOOL,
        )
        logger.info("[synthetic] created Qdrant collection %s (%dd)", COLLECTION, TEXT_DIM)


def generate_synthetic_queries(asset_id: str):
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct
    from ingest.llm import call_llm

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[synthetic] asset not found: %s", asset_id)
            return

        shots = (
            db.query(Shot)
            .filter(Shot.asset_id == asset_id, Shot.deleted_at.is_(None))
            .order_by(Shot.shot_index)
            .all()
        )
        logger.info("[synthetic] start asset_id=%s shots=%d", asset_id, len(shots))

        # Delete old synthetic queries for this asset (idempotent)
        db.query(SyntheticQuery).filter(
            SyntheticQuery.asset_id == asset_id,
            SyntheticQuery.deleted_at.is_(None),
        ).delete()
        db.flush()

        all_texts: list[str] = []
        all_meta: list[dict] = []  # {parent_type, parent_id, shot_id}

        # ── per-shot queries ──────────────────────────────────────────────────
        for shot in shots:
            turns = (
                db.query(TranscriptTurn)
                .filter(
                    TranscriptTurn.shot_id == shot.id,
                    TranscriptTurn.deleted_at.is_(None),
                )
                .order_by(TranscriptTurn.start_pts)
                .all()
            )
            objects = (
                db.query(ObjectTrack)
                .filter(
                    ObjectTrack.shot_id == shot.id,
                    ObjectTrack.deleted_at.is_(None),
                )
                .all()
            )

            transcript_text = " ".join(t.text for t in turns) if turns else "no dialogue"
            obj_labels = ", ".join(set(o.label for o in objects)) if objects else "none"

            context = (
                f"Shot {shot.shot_index} of {asset.filename}\n"
                f"Duration: {(shot.end_pts - shot.start_pts):.1f}s\n"
                f"Shot size: {shot.shot_size_class or 'unknown'}\n"
                f"Transcript: {transcript_text}\n"
                f"Objects detected: {obj_labels}"
            )
            prompt = _SHOT_PROMPT.format(n=settings.synthetic_queries_per_shot, context=context)

            try:
                raw = call_llm(prompt, max_tokens=256)
                queries = _parse_queries(raw, settings.synthetic_queries_per_shot)
            except Exception:
                logger.warning("[synthetic] LLM failed for shot %s, skipping", shot.id, exc_info=True)
                queries = []

            for q in queries:
                sq = SyntheticQuery(
                    asset_id=asset_id,
                    parent_type="shot",
                    parent_id=shot.id,
                    query_text=q,
                    producer_model=PRODUCER_MODEL,
                    producer_version=PRODUCER_VERSION,
                )
                db.add(sq)
                db.flush()
                all_texts.append(q)
                all_meta.append({"parent_type": "shot", "parent_id": shot.id,
                                  "shot_id": shot.id, "sq_id": sq.id})

            logger.info("[synthetic] shot %s: %d queries generated", shot.id, len(queries))

        # ── per-turn queries ──────────────────────────────────────────────────
        all_turns = (
            db.query(TranscriptTurn)
            .filter(TranscriptTurn.asset_id == asset_id, TranscriptTurn.deleted_at.is_(None))
            .all()
        )
        for turn in all_turns:
            if len(turn.text) <= 20:
                continue
            context = f"Speaker: {turn.speaker_label or 'unknown'}\nText: {turn.text}"
            prompt = _TURN_PROMPT.format(n=settings.synthetic_queries_per_turn, context=context)

            try:
                raw = call_llm(prompt, max_tokens=128)
                queries = _parse_queries(raw, settings.synthetic_queries_per_turn)
            except Exception:
                logger.warning("[synthetic] LLM failed for turn %s, skipping", turn.id, exc_info=True)
                queries = []

            for q in queries:
                sq = SyntheticQuery(
                    asset_id=asset_id,
                    parent_type="transcript_turn",
                    parent_id=turn.id,
                    query_text=q,
                    producer_model=PRODUCER_MODEL,
                    producer_version=PRODUCER_VERSION,
                )
                db.add(sq)
                db.flush()
                all_texts.append(q)
                all_meta.append({"parent_type": "transcript_turn", "parent_id": turn.id,
                                  "shot_id": turn.shot_id, "sq_id": sq.id})

        db.commit()
        logger.info("[synthetic] wrote %d SyntheticQuery rows for asset %s", len(all_texts), asset_id)

        if not all_texts:
            return

        # ── embed + upsert to Qdrant ──────────────────────────────────────────
        client = QdrantClient(url=settings.qdrant_url)
        _ensure_qdrant_collection(client)

        vecs = _embed_texts(all_texts)

        # Re-fetch SyntheticQuery rows to get their IDs (already flushed above)
        sq_rows = (
            db.query(SyntheticQuery)
            .filter(SyntheticQuery.asset_id == asset_id, SyntheticQuery.deleted_at.is_(None))
            .all()
        )
        sq_by_id = {sq.id: sq for sq in sq_rows}

        points = []
        for text, meta, vec in zip(all_texts, all_meta, vecs):
            sq_id = meta["sq_id"]
            sq = sq_by_id.get(sq_id)
            if not sq:
                continue
            point_id = str(uuid.uuid4())
            sq.qdrant_point_id = point_id
            points.append(PointStruct(
                id=point_id,
                vector=vec,
                payload={
                    "asset_id": asset_id,
                    "project_id": asset.project_id,
                    "parent_type": meta["parent_type"],
                    "parent_id": meta["parent_id"],
                    "shot_id": meta["shot_id"],
                    "query_text": text,
                    "deleted": False,
                },
            ))

        if points:
            client.upsert(collection_name=COLLECTION, points=points)
        db.commit()
        logger.info("[synthetic] upserted %d points to %s", len(points), COLLECTION)
