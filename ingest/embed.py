"""
Stage 5: embed_asset
Embeds keyframes (visual), sentences+turns (text), OCR spans.
Writes vectors to Qdrant. Updates qdrant_point_id on PG rows.
Advances asset state: embedding → complete.

Models (with fallbacks):
  Visual: Qwen3-VL-Embedding-2B (2048d) → google/siglip-so400m-patch14-384 (1152d)
  Text:   Qwen3-Embedding-8B (4096d) → BAAI/bge-m3 (1024d)
"""
import logging
import os
import sys
import tempfile
import uuid
from typing import Optional

import torch
from sqlalchemy import text

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Shot, Keyframe, TranscriptTurn, Sentence, OcrSpan
from ingest.gpu_pod_manager import gpu_available

logger = logging.getLogger(__name__)

VL_SCRIPTS_DIR = "/workspace/weights/qwen3/Qwen3-VL-Embedding-2B/scripts"

# ── cached model handles (one per worker process) ──────────────────────────
_visual_model = None
_visual_dim = None
_text_model = None
_text_dim = None
_visual_model_name = None
_text_model_name = None


def _load_visual_model():
    global _visual_model, _visual_dim, _visual_model_name
    if _visual_model is not None:
        return _visual_model, _visual_dim

    device = "cuda" if gpu_available() else "cpu"

    for model_name, dim in [
        (settings.qwen3_vl_model, settings.qwen3_vl_dim),
        ("google/siglip-so400m-patch14-384", 1152),
    ]:
        try:
            model = _init_visual(model_name, device)
            _visual_model = model
            _visual_dim = dim
            _visual_model_name = model_name
            logger.info("[embed] visual model loaded: %s dim=%d device=%s", model_name, dim, device)
            return _visual_model, _visual_dim
        except Exception as exc:
            logger.warning("[embed] visual model %s failed: %s", model_name, exc)

    raise RuntimeError("All visual model candidates failed")


def _init_visual(model_name: str, device: str):
    if "Qwen3-VL" in model_name:
        if VL_SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, VL_SCRIPTS_DIR)
        from qwen3_vl_embedding import Qwen3VLEmbedder
        embedder = Qwen3VLEmbedder(
            model_name,
            torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
        )
        return ("qwen3vl", embedder, device)

    if "siglip" in model_name.lower():
        from transformers import AutoProcessor, AutoModel
        processor = AutoProcessor.from_pretrained(model_name)
        model = AutoModel.from_pretrained(
            model_name,
            torch_dtype=torch.float16 if device == "cuda" else torch.float32,
        ).to(device).eval()
        return ("siglip", processor, model, device)

    raise ValueError(f"Unsupported visual model: {model_name}")


def _embed_images(image_paths: list[str]) -> list[list[float]]:
    model_pack, dim = _load_visual_model()
    model_type = model_pack[0]
    results = []
    batch_size = settings.embed_batch_size

    for i in range(0, len(image_paths), batch_size):
        batch = image_paths[i:i + batch_size]

        if model_type == "qwen3vl":
            _, embedder, device = model_pack
            inputs_list = [{"image": p} for p in batch]
            feats = embedder.process(inputs_list)  # normalized tensor, bfloat16
            feats = feats.cpu().to(torch.float32)
            results.extend(feats.tolist())

        else:  # siglip
            from PIL import Image
            _, processor, model, device = model_pack
            images = [Image.open(p).convert("RGB") for p in batch]
            with torch.no_grad():
                inputs = processor(images=images, return_tensors="pt", padding=True).to(device)
                out = model.vision_model(pixel_values=inputs["pixel_values"])
                feats = out.pooler_output  # [B, 1152]
                feats = feats / feats.norm(dim=-1, keepdim=True)
            results.extend(feats.float().cpu().tolist())

    return results


def _load_text_model():
    global _text_model, _text_dim, _text_model_name
    if _text_model is not None:
        return _text_model, _text_dim

    device = "cuda" if gpu_available() else "cpu"

    for model_name, dim in [
        (settings.qwen3_text_model, settings.qwen3_text_dim),
        ("BAAI/bge-m3", 1024),
    ]:
        try:
            model = _init_text(model_name, device)
            _text_model = model
            _text_dim = dim
            _text_model_name = model_name
            logger.info("[embed] text model loaded: %s dim=%d device=%s", model_name, dim, device)
            return _text_model, _text_dim
        except Exception as exc:
            logger.warning("[embed] text model %s failed: %s", model_name, exc)

    raise RuntimeError("All text model candidates failed")


def _init_text(model_name: str, device: str):
    if "Qwen3-Embedding" in model_name or os.path.isdir(model_name):
        # Use SentenceTransformer for Qwen3-Embedding (last-token pooling, 4096d)
        from sentence_transformers import SentenceTransformer
        dtype = torch.float16 if device == "cuda" else torch.float32
        model = SentenceTransformer(
            model_name,
            model_kwargs={"dtype": dtype},
            trust_remote_code=True,
        )
        model.to(device)
        return ("st", model, device)

    # Fallback: raw transformers with mean pooling (bge-m3 etc.)
    from transformers import AutoTokenizer, AutoModel
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    model = AutoModel.from_pretrained(
        model_name, trust_remote_code=True,
        torch_dtype=torch.float16 if device == "cuda" else torch.float32,
    ).to(device).eval()
    return ("hf", tokenizer, model, device)


def _embed_texts(texts: list[str]) -> list[list[float]]:
    model_pack, dim = _load_text_model()
    model_type = model_pack[0]
    results = []
    batch_size = settings.embed_batch_size

    if model_type == "st":
        _, model, device = model_pack
        # Use document prompt (empty prefix) for indexing
        embeddings = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            prompt_name="document",
            convert_to_numpy=True,
        )
        return [e.tolist() for e in embeddings]

    # hf fallback: mean pooling
    _, tokenizer, model, device = model_pack
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        with torch.no_grad():
            encoded = tokenizer(batch, padding=True, truncation=True,
                                max_length=512, return_tensors="pt").to(device)
            out = model(**encoded)
            mask = encoded["attention_mask"].unsqueeze(-1).float()
            feats = (out.last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        results.extend(feats.float().cpu().tolist())
    return results


def _get_qdrant():
    from qdrant_client import QdrantClient
    return QdrantClient(url=settings.qdrant_url)


def _ensure_collection(client, name: str, dim: int):
    from qdrant_client.models import VectorParams, Distance, PayloadSchemaType
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        for field in ["asset_id", "shot_id", "project_id", "embedding_version", "deleted"]:
            ftype = PayloadSchemaType.BOOL if field == "deleted" else PayloadSchemaType.KEYWORD
            client.create_payload_index(collection_name=name, field_name=field, field_schema=ftype)
        logger.info("[embed] created Qdrant collection %s dim=%d", name, dim)


def embed_asset(asset_id: str):
    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[embed] asset not found: %s", asset_id)
            return

        logger.info("[embed] start asset_id=%s", asset_id)

        try:
            _run_visual_embed(db, asset_id, asset.project_id)
            _run_text_embed(db, asset_id, asset.project_id)
            _run_ocr_embed(db, asset_id, asset.project_id)

            asset = db.query(Asset).filter(Asset.id == asset_id).first()
            asset.ingest_state = "complete"
            asset.ingest_error = None
            db.commit()
            logger.info("[embed] done asset_id=%s → state=complete", asset_id)

        except Exception as exc:
            logger.exception("[embed] failed asset_id=%s", asset_id)
            try:
                db.rollback()
                asset = db.query(Asset).filter(Asset.id == asset_id).first()
                if asset:
                    asset.ingest_state = "failed"
                    asset.ingest_error = f"embed: {exc}"
                    db.commit()
            except Exception:
                pass
            raise


def _run_visual_embed(db, asset_id: str, project_id: str):
    import store.client as store_client
    from qdrant_client.models import PointStruct

    _, dim = _load_visual_model()
    qdrant = _get_qdrant()
    _ensure_collection(qdrant, "visual_keyframe_qwen3vl", dim)
    _ensure_collection(qdrant, "visual_shot_qwen3vl", dim)

    emb_version = _visual_model_name or "unknown"
    shots = (
        db.query(Shot)
        .filter(Shot.asset_id == asset_id, Shot.deleted_at.is_(None))
        .order_by(Shot.shot_index)
        .all()
    )
    shot_map = {s.id: s for s in shots}

    keyframes = (
        db.query(Keyframe)
        .filter(Keyframe.asset_id == asset_id, Keyframe.deleted_at.is_(None))
        .order_by(Keyframe.pts)
        .all()
    )

    if not keyframes:
        logger.warning("[embed] no keyframes for asset_id=%s", asset_id)
        return

    # Download keyframes to tmp files
    tmp_files = []
    valid_kfs = []
    for kf in keyframes:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        try:
            bucket_key = kf.path.removeprefix("media-keyframes/")
            store_client.download_file("media-keyframes", bucket_key, tmp.name)
            tmp_files.append(tmp.name)
            valid_kfs.append(kf)
        except Exception as exc:
            logger.warning("[embed] failed to download keyframe %s: %s", kf.id, exc)
            os.unlink(tmp.name)

    try:
        vectors = _embed_images(tmp_files)
    finally:
        for f in tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass

    # Write keyframe-level points
    kf_points = []
    shot_vectors: dict[str, list[list[float]]] = {}
    for kf, vec in zip(valid_kfs, vectors):
        point_id = str(uuid.uuid4())
        kf.qdrant_point_id = point_id
        shot = shot_map.get(kf.shot_id)
        kf_points.append(PointStruct(
            id=point_id,
            vector=vec,
            payload={
                "asset_id": asset_id,
                "shot_id": kf.shot_id,
                "keyframe_id": kf.id,
                "project_id": project_id,
                "pts": kf.pts,
                "embedding_version": emb_version,
                "deleted": False,
            },
        ))
        shot_vectors.setdefault(kf.shot_id, []).append(vec)

    qdrant.upsert(collection_name="visual_keyframe_qwen3vl", points=kf_points)
    db.commit()

    # Write shot-level mean-pooled points
    shot_points = []
    for shot in shots:
        vecs = shot_vectors.get(shot.id)
        if not vecs:
            continue
        mean_vec = [sum(v[i] for v in vecs) / len(vecs) for i in range(len(vecs[0]))]
        norm = sum(x * x for x in mean_vec) ** 0.5
        mean_vec = [x / norm for x in mean_vec]
        point_id = str(uuid.uuid4())
        shot_points.append(PointStruct(
            id=point_id,
            vector=mean_vec,
            payload={
                "asset_id": asset_id,
                "shot_id": shot.id,
                "project_id": project_id,
                "start_pts": shot.start_pts,
                "end_pts": shot.end_pts,
                "shot_size_class": shot.shot_size_class,
                "motion_score": shot.motion_score,
                "embedding_version": emb_version,
                "deleted": False,
            },
        ))

    if shot_points:
        qdrant.upsert(collection_name="visual_shot_qwen3vl", points=shot_points)

    logger.info("[embed] visual: %d keyframe points, %d shot points", len(kf_points), len(shot_points))


def _run_text_embed(db, asset_id: str, project_id: str):
    from qdrant_client.models import PointStruct

    _, dim = _load_text_model()
    qdrant = _get_qdrant()
    _ensure_collection(qdrant, "text_sentence_qwen3", dim)
    _ensure_collection(qdrant, "text_turn_qwen3", dim)

    emb_version = _text_model_name or "unknown"

    sentences = (
        db.query(Sentence)
        .filter(Sentence.asset_id == asset_id)
        .order_by(Sentence.start_pts)
        .all()
    )
    turns = (
        db.query(TranscriptTurn)
        .filter(TranscriptTurn.asset_id == asset_id, TranscriptTurn.deleted_at.is_(None))
        .order_by(TranscriptTurn.turn_index)
        .all()
    )
    turn_map = {t.id: t for t in turns}

    if sentences:
        texts = [s.text for s in sentences]
        vectors = _embed_texts(texts)
        sent_points = []
        for sent, vec in zip(sentences, vectors):
            point_id = str(uuid.uuid4())
            sent.qdrant_point_id = point_id
            turn = turn_map.get(sent.turn_id)
            sent_points.append(PointStruct(
                id=point_id,
                vector=vec,
                payload={
                    "asset_id": asset_id,
                    "shot_id": turn.shot_id if turn else None,
                    "turn_id": sent.turn_id,
                    "sentence_id": sent.id,
                    "project_id": project_id,
                    "start_pts": sent.start_pts,
                    "end_pts": sent.end_pts,
                    "speaker_label": turn.speaker_label if turn else None,
                    "embedding_version": emb_version,
                    "deleted": False,
                },
            ))
        qdrant.upsert(collection_name="text_sentence_qwen3", points=sent_points)
        db.commit()
        logger.info("[embed] text: %d sentence points", len(sent_points))

    if turns:
        texts = [t.text for t in turns]
        vectors = _embed_texts(texts)
        turn_points = []
        for turn, vec in zip(turns, vectors):
            point_id = str(uuid.uuid4())
            turn.qdrant_point_id = point_id
            turn_points.append(PointStruct(
                id=point_id,
                vector=vec,
                payload={
                    "asset_id": asset_id,
                    "shot_id": turn.shot_id,
                    "turn_id": turn.id,
                    "project_id": project_id,
                    "start_pts": turn.start_pts,
                    "end_pts": turn.end_pts,
                    "speaker_label": turn.speaker_label,
                    "embedding_version": emb_version,
                    "deleted": False,
                },
            ))
        qdrant.upsert(collection_name="text_turn_qwen3", points=turn_points)
        db.commit()
        logger.info("[embed] text: %d turn points", len(turn_points))


def _run_ocr_embed(db, asset_id: str, project_id: str):
    from qdrant_client.models import PointStruct

    _, dim = _load_text_model()
    qdrant = _get_qdrant()
    _ensure_collection(qdrant, "ocr_span_qwen3", dim)

    emb_version = _text_model_name or "unknown"
    threshold = settings.ocr_confidence_threshold

    spans = (
        db.query(OcrSpan)
        .filter(
            OcrSpan.asset_id == asset_id,
            OcrSpan.confidence >= threshold,
            OcrSpan.deleted_at.is_(None),
        )
        .order_by(OcrSpan.id)
        .all()
    )
    if not spans:
        return

    kf_map = {kf.id: kf for kf in (
        db.query(Keyframe).filter(Keyframe.asset_id == asset_id).all()
    )}

    texts = [s.text for s in spans]
    vectors = _embed_texts(texts)
    points = []
    for span, vec in zip(spans, vectors):
        point_id = str(uuid.uuid4())
        span.qdrant_point_id = point_id
        kf = kf_map.get(span.keyframe_id)
        points.append(PointStruct(
            id=point_id,
            vector=vec,
            payload={
                "asset_id": asset_id,
                "keyframe_id": span.keyframe_id,
                "ocr_span_id": span.id,
                "project_id": project_id,
                "pts": kf.pts if kf else None,
                "embedding_version": emb_version,
                "deleted": False,
            },
        ))
    qdrant.upsert(collection_name="ocr_span_qwen3", points=points)
    db.commit()
    logger.info("[embed] ocr: %d span points", len(points))
