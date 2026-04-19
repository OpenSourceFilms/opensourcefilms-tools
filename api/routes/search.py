"""
Phase 1 Search API — multi-surface embed-and-fuse with RRF.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from config.settings import settings
from db.base import AsyncSessionLocal
from db.models import Asset, Shot, Keyframe, TranscriptTurn, Scene, FaceTrack, ObjectTrack
import store.client as store

logger = logging.getLogger(__name__)
router = APIRouter()


class SearchFilters(BaseModel):
    asset_ids: list[str] = []
    shot_size_class: Optional[str] = None
    min_duration_s: Optional[float] = None
    # Phase 2 filters
    character_ids: list[str] = []      # shots where any of these characters appear
    object_labels: list[str] = []      # shots where any of these object labels appear
    motion_score_min: Optional[float] = None
    motion_score_max: Optional[float] = None
    palette_hex: list[str] = []        # stub — future nearest-colour filter


class SearchRequest(BaseModel):
    query: str
    surfaces: list[str] = ["visual", "text", "ocr"]
    limit: int = 20
    filters: SearchFilters = SearchFilters()


def _rrf_fuse(ranked_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion. Returns (id, score) sorted descending."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _embed_text_query(text: str) -> list[float]:
    """Embed query text using Qwen3-Embedding (SentenceTransformer, query prompt)."""
    import torch
    from ingest.gpu_pod_manager import gpu_available

    model_name = settings.qwen3_text_model
    device = "cuda" if gpu_available() else "cpu"

    if not hasattr(_embed_text_query, "_cache"):
        _embed_text_query._cache = {}
    if model_name not in _embed_text_query._cache:
        try:
            from sentence_transformers import SentenceTransformer
            dtype = torch.float16 if device == "cuda" else torch.float32
            mdl = SentenceTransformer(
                model_name,
                model_kwargs={"dtype": dtype},
                trust_remote_code=True,
            ).to(device)
            _embed_text_query._cache[model_name] = ("st", mdl)
        except Exception:
            # Fallback to bge-m3 with mean pooling
            from transformers import AutoTokenizer, AutoModel
            tok = AutoTokenizer.from_pretrained("BAAI/bge-m3")
            mdl = AutoModel.from_pretrained(
                "BAAI/bge-m3",
                torch_dtype=torch.float16 if device == "cuda" else torch.float32,
            ).to(device).eval()
            _embed_text_query._cache[model_name] = ("hf", tok, mdl, device)

    cached = _embed_text_query._cache[model_name]
    if cached[0] == "st":
        _, mdl = cached
        vec = mdl.encode(
            [text],
            normalize_embeddings=True,
            prompt_name="query",
            convert_to_numpy=True,
        )[0]
        return vec.tolist()
    else:
        _, tok, mdl, dev = cached
        import torch
        with torch.no_grad():
            enc = tok([text], padding=True, truncation=True, max_length=512, return_tensors="pt").to(dev)
            out = mdl(**enc)
            mask = enc["attention_mask"].unsqueeze(-1).float()
            feat = (out.last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat.float().cpu().tolist()[0]


def _embed_visual_query(text: str) -> list[float]:
    """Embed text query for visual search using Qwen3-VL text path (same dim as VL embeddings)."""
    # Phase 1: reuse the text model for visual query — cross-modal cosine is directionally useful.
    # Visual keyframe/shot collections are 2048d (Qwen3-VL-2B output dim).
    # Until we have a text→2048d encoder, we embed into text space and search text collections only.
    # Visual search is therefore a no-op until a proper cross-modal query encoder is added.
    return _embed_text_query(text)


def _qdrant_search(collection: str, vector: list[float], limit: int,
                   asset_ids: Optional[list[str]] = None) -> list[str]:
    """Search Qdrant, return list of payload['shot_id'] or relevant ID."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchAny, MatchValue

    client = QdrantClient(url=settings.qdrant_url)

    must = [FieldCondition(key="deleted", match=MatchValue(value=False))]
    if asset_ids:
        must.append(FieldCondition(key="asset_id", match=MatchAny(any=asset_ids)))

    try:
        hits = client.search(
            collection_name=collection,
            query_vector=vector,
            query_filter=Filter(must=must),
            limit=limit * 3,  # oversample for RRF
            with_payload=True,
        )
        return [h.payload.get("shot_id", h.payload.get("keyframe_id", str(h.id))) for h in hits]
    except Exception as exc:
        logger.warning("[search] Qdrant search %s failed: %s", collection, exc)
        return []


@router.post("/{project_id}/search")
async def search(project_id: str, req: SearchRequest):
    import asyncio

    async with AsyncSessionLocal() as db:
        # Verify project exists
        from db.models import Project
        proj = await db.execute(select(Project).where(Project.id == project_id))
        if not proj.scalar_one_or_none():
            raise HTTPException(404, "project not found")

    if not req.query.strip():
        raise HTTPException(400, "query must not be empty")

    loop = asyncio.get_event_loop()

    # Embed query for each surface
    ranked_lists = []

    text_vec = await loop.run_in_executor(None, _embed_text_query, req.query)
    visual_vec = text_vec  # placeholder — cross-modal visual search not yet implemented

    asset_ids = req.filters.asset_ids or None

    # NOTE: visual collections are 2048d (Qwen3-VL-2B).
    # Text query vectors are 4096d — cross-modal visual search requires a dedicated
    # text→2048d encoder. Until then, visual surface is skipped for text queries.
    # Visual-only image query will be added in Phase 2.
    _ = visual_vec if "visual" in req.surfaces else None  # suppress unused var

    if "text" in req.surfaces:
        sent_results = await loop.run_in_executor(
            None, _qdrant_search, "text_sentence_qwen3",
            text_vec, req.limit, asset_ids
        )
        if sent_results:
            ranked_lists.append(sent_results)

        turn_results = await loop.run_in_executor(
            None, _qdrant_search, "text_turn_qwen3",
            text_vec, req.limit, asset_ids
        )
        if turn_results:
            ranked_lists.append(turn_results)

    if "ocr" in req.surfaces:
        ocr_results = await loop.run_in_executor(
            None, _qdrant_search, "ocr_span_qwen3",
            text_vec, req.limit, asset_ids
        )
        if ocr_results:
            ranked_lists.append(ocr_results)

    if not ranked_lists:
        return {"results": [], "total": 0}

    fused = _rrf_fuse(ranked_lists)[:req.limit]
    shot_ids = [sid for sid, _ in fused if sid]
    scores = {sid: score for sid, score in fused}

    # Fetch shot details from Postgres
    return await _build_results(shot_ids, scores, project_id, req)


async def _build_results(shot_ids: list[str], scores: dict, project_id: str, req: SearchRequest):
    if not shot_ids:
        return {"results": [], "total": 0}

    async with AsyncSessionLocal() as db:
        shots_result = await db.execute(
            select(Shot)
            .where(Shot.id.in_(shot_ids), Shot.deleted_at.is_(None))
        )
        shots = {s.id: s for s in shots_result.scalars().all()}

        # Pre-fetch face track character memberships if needed
        char_shot_ids: set[str] | None = None
        if req.filters.character_ids:
            ft_result = await db.execute(
                select(FaceTrack.shot_id).where(
                    FaceTrack.character_id.in_(req.filters.character_ids),
                    FaceTrack.deleted_at.is_(None),
                )
            )
            char_shot_ids = {row[0] for row in ft_result.all()}

        # Pre-fetch object track label memberships if needed
        obj_shot_ids: set[str] | None = None
        if req.filters.object_labels:
            ot_result = await db.execute(
                select(ObjectTrack.shot_id).where(
                    ObjectTrack.label.in_(req.filters.object_labels),
                    ObjectTrack.deleted_at.is_(None),
                )
            )
            obj_shot_ids = {row[0] for row in ot_result.all()}

        # Filter by shot constraints
        valid_shot_ids = []
        for sid in shot_ids:
            shot = shots.get(sid)
            if not shot:
                continue
            if req.filters.shot_size_class and shot.shot_size_class != req.filters.shot_size_class:
                continue
            if req.filters.min_duration_s and (shot.end_pts - shot.start_pts) < req.filters.min_duration_s:
                continue
            if req.filters.motion_score_min is not None and (
                shot.motion_score is None or shot.motion_score < req.filters.motion_score_min
            ):
                continue
            if req.filters.motion_score_max is not None and (
                shot.motion_score is None or shot.motion_score > req.filters.motion_score_max
            ):
                continue
            if char_shot_ids is not None and sid not in char_shot_ids:
                continue
            if obj_shot_ids is not None and sid not in obj_shot_ids:
                continue
            valid_shot_ids.append(sid)

        # Fetch related data
        asset_ids_needed = list({shots[s].asset_id for s in valid_shot_ids if s in shots})
        assets_result = await db.execute(
            select(Asset).where(Asset.id.in_(asset_ids_needed))
        )
        assets = {a.id: a for a in assets_result.scalars().all()}

        # Keyframes per shot
        from sqlalchemy import and_
        kf_result = await db.execute(
            select(Keyframe).where(
                Keyframe.shot_id.in_(valid_shot_ids),
                Keyframe.deleted_at.is_(None),
            ).order_by(Keyframe.pts)
        )
        kf_by_shot: dict[str, list] = {}
        for kf in kf_result.scalars().all():
            kf_by_shot.setdefault(kf.shot_id, []).append(kf)

        # Transcript turns overlapping each shot
        turns_result = await db.execute(
            select(TranscriptTurn).where(
                TranscriptTurn.shot_id.in_(valid_shot_ids),
                TranscriptTurn.deleted_at.is_(None),
            ).order_by(TranscriptTurn.start_pts)
        )
        turns_by_shot: dict[str, list] = {}
        for t in turns_result.scalars().all():
            turns_by_shot.setdefault(t.shot_id, []).append(t)

        # Scene context
        scene_ids = list({shots[s].scene_id for s in valid_shot_ids
                          if s in shots and shots[s].scene_id})
        scenes_map = {}
        if scene_ids:
            scene_result = await db.execute(select(Scene).where(Scene.id.in_(scene_ids)))
            for sc in scene_result.scalars().all():
                # Count shots in scene
                from sqlalchemy import func
                cnt = await db.execute(
                    select(func.count()).where(Shot.scene_id == sc.id, Shot.deleted_at.is_(None))
                )
                scenes_map[sc.id] = {"scene_id": sc.id, "shot_count": cnt.scalar() or 0}

        # Neighbour shots (same asset, shot_index ± 1)
        neighbours_needed = []
        for sid in valid_shot_ids:
            shot = shots.get(sid)
            if not shot:
                continue
            neighbours_needed.append((shot.asset_id, shot.shot_index - 1))
            neighbours_needed.append((shot.asset_id, shot.shot_index + 1))

        neighbour_map: dict[str, str] = {}
        if neighbours_needed:
            from sqlalchemy import tuple_
            nb_result = await db.execute(
                select(Shot).where(
                    Shot.deleted_at.is_(None),
                    tuple_(Shot.asset_id, Shot.shot_index).in_(neighbours_needed),
                )
            )
            for nb in nb_result.scalars().all():
                neighbour_map[(nb.asset_id, nb.shot_index)] = nb.id

    results = []
    for sid in valid_shot_ids[:req.limit]:
        shot = shots.get(sid)
        if not shot:
            continue
        asset = assets.get(shot.asset_id)
        if not asset:
            continue

        kfs = kf_by_shot.get(sid, [])
        keyframe_urls = [
            store.get_presigned_url("media-keyframes", kf.path.removeprefix("media-keyframes/"), 3600)
            for kf in kfs[:5]
        ]

        turns = turns_by_shot.get(sid, [])
        transcript = " | ".join(
            f"{t.speaker_label or 'SPK'}: {t.text}" for t in turns
        ) or None

        scene_ctx = scenes_map.get(shot.scene_id) if shot.scene_id else None

        prev_id = neighbour_map.get((shot.asset_id, shot.shot_index - 1))
        next_id = neighbour_map.get((shot.asset_id, shot.shot_index + 1))

        # Determine which surfaces fired
        surfaces_hit = []
        if sid in (kf_by_shot or {}):
            surfaces_hit.append("visual")
        if sid in (turns_by_shot or {}):
            surfaces_hit.append("text")

        stable_id = f"shot://{(asset.sha256 or '')[:8]}/{shot.shot_index:05d}"

        results.append({
            "shot_id": sid,
            "asset_id": shot.asset_id,
            "start_pts": shot.start_pts,
            "end_pts": shot.end_pts,
            "duration_s": round(shot.end_pts - shot.start_pts, 3),
            "score": round(scores.get(sid, 0.0), 6),
            "surfaces_hit": surfaces_hit,
            "keyframes": keyframe_urls,
            "transcript": transcript,
            "ocr_text": None,  # TODO: fetch from ocr_span if needed
            "scene_context": scene_ctx,
            "neighbour_shots": [s for s in [prev_id, next_id] if s],
            "stable_id": stable_id,
        })

    return {"results": results, "total": len(results)}


@router.get("/{project_id}/shots/{shot_id}")
async def get_shot(project_id: str, shot_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Shot).where(Shot.id == shot_id, Shot.deleted_at.is_(None))
        )
        shot = result.scalar_one_or_none()
        if not shot:
            raise HTTPException(404, "shot not found")
        asset_r = await db.execute(select(Asset).where(Asset.id == shot.asset_id))
        asset = asset_r.scalar_one_or_none()
        stable_id = f"shot://{(asset.sha256 or '')[:8]}/{shot.shot_index:05d}" if asset else None
        return {
            "id": shot.id,
            "asset_id": shot.asset_id,
            "shot_index": shot.shot_index,
            "start_pts": shot.start_pts,
            "end_pts": shot.end_pts,
            "duration_s": shot.duration_s,
            "shot_size_class": shot.shot_size_class,
            "motion_score": shot.motion_score,
            "stable_id": stable_id,
        }


@router.get("/{project_id}/shots/{shot_id}/keyframes")
async def get_shot_keyframes(project_id: str, shot_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Keyframe)
            .where(Keyframe.shot_id == shot_id, Keyframe.deleted_at.is_(None))
            .order_by(Keyframe.pts)
        )
        kfs = result.scalars().all()
        return [
            {
                "id": kf.id,
                "pts": kf.pts,
                "url": store.get_presigned_url(
                    "media-keyframes", kf.path.removeprefix("media-keyframes/"), 3600
                ),
            }
            for kf in kfs
        ]


@router.get("/{project_id}/assets/{asset_id}/transcript")
async def get_transcript(project_id: str, asset_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TranscriptTurn)
            .where(
                TranscriptTurn.asset_id == asset_id,
                TranscriptTurn.deleted_at.is_(None),
            )
            .order_by(TranscriptTurn.turn_index)
        )
        turns = result.scalars().all()
        return [
            {
                "turn_id": t.id,
                "turn_index": t.turn_index,
                "start_pts": t.start_pts,
                "end_pts": t.end_pts,
                "speaker_label": t.speaker_label,
                "text": t.text,
                "stable_id": f"turn://{asset_id[:8]}/{t.turn_index:05d}",
            }
            for t in turns
        ]
