"""
Stage 6a: detect_faces(asset_id)

Runs InsightFace buffalo_l on every keyframe per shot.
Groups detections across keyframes into face tracks via IoU overlap.
Writes FaceTrack rows and upserts to Qdrant face_insightface collection.
"""
import logging
import os
import tempfile
import uuid

import numpy as np

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Shot, Keyframe, FaceTrack

logger = logging.getLogger(__name__)

PRODUCER_MODEL = "insightface/buffalo_l"
PRODUCER_VERSION = "0.7"

_face_app = None


def _get_face_app():
    global _face_app
    if _face_app is None:
        import insightface
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(
            name=settings.insightface_model,
            root=settings.insightface_root,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        app.prepare(ctx_id=0, det_size=(640, 640))
        _face_app = app
        logger.info("[faces] InsightFace %s loaded", settings.insightface_model)
    return _face_app


def _iou(b1: dict, b2: dict) -> float:
    """IoU between two normalised {x,y,w,h} bboxes."""
    x1, y1 = max(b1["x"], b2["x"]), max(b1["y"], b2["y"])
    x2 = min(b1["x"] + b1["w"], b2["x"] + b2["w"])
    y2 = min(b1["y"] + b1["h"], b2["y"] + b2["h"])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = b1["w"] * b1["h"] + b2["w"] * b2["h"] - inter
    return inter / union if union > 0 else 0.0


def _detect_on_image(image_path: str, img_w: int, img_h: int) -> list[dict]:
    """Return list of {bbox, embedding, confidence}."""
    import cv2
    app = _get_face_app()
    img = cv2.imread(image_path)
    if img is None:
        return []
    faces = app.get(img)
    results = []
    for face in faces:
        x1, y1, x2, y2 = face.bbox.astype(float)
        w, h = max(0.0, x2 - x1), max(0.0, y2 - y1)
        bbox = {
            "x": round(x1 / img_w, 4), "y": round(y1 / img_h, 4),
            "w": round(w / img_w, 4),  "h": round(h / img_h, 4),
        }
        results.append({
            "bbox": bbox,
            "embedding": face.normed_embedding.tolist(),  # 512d
            "confidence": float(face.det_score),
        })
    return results


def _group_into_tracks(
    kf_detections: list[tuple[float, list[dict]]]  # [(pts, [det, ...]), ...]
) -> list[dict]:
    """
    Simple greedy IoU tracker across keyframes.
    Returns list of track dicts: {start_pts, end_pts, bbox_series, embeddings, confidence}
    """
    iou_thresh = settings.face_iou_threshold
    tracks: list[dict] = []  # active tracks

    for pts, dets in kf_detections:
        matched = set()
        for det in dets:
            best_track, best_iou = None, iou_thresh
            for i, track in enumerate(tracks):
                if i in matched:
                    continue
                last_bbox = track["bbox_series"][-1]["bbox"]
                iou = _iou(last_bbox, det["bbox"])
                if iou > best_iou:
                    best_iou = iou
                    best_track = i
            if best_track is not None:
                tracks[best_track]["bbox_series"].append({"pts": pts, "bbox": det["bbox"]})
                tracks[best_track]["embeddings"].append(det["embedding"])
                tracks[best_track]["end_pts"] = pts
                tracks[best_track]["confidence"] = max(
                    tracks[best_track]["confidence"], det["confidence"]
                )
                matched.add(best_track)
            else:
                tracks.append({
                    "start_pts": pts,
                    "end_pts": pts,
                    "bbox_series": [{"pts": pts, "bbox": det["bbox"]}],
                    "embeddings": [det["embedding"]],
                    "confidence": det["confidence"],
                })

    result = []
    for track in tracks:
        embs = np.array(track["embeddings"], dtype=np.float32)
        mean_emb = embs.mean(axis=0)
        norm = np.linalg.norm(mean_emb)
        if norm > 0:
            mean_emb = mean_emb / norm
        result.append({
            "start_pts": track["start_pts"],
            "end_pts": track["end_pts"],
            "bbox_series": track["bbox_series"],
            "embedding": mean_emb.tolist(),
            "confidence": track["confidence"],
        })
    return result


def detect_faces(asset_id: str):
    import store.client as store_client

    if not settings.face_detection_enabled:
        logger.info("[faces] disabled — skipping asset_id=%s", asset_id)
        _mark_enrich_done_if_ready(asset_id, "face")
        return

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[faces] asset not found: %s", asset_id)
            return

        shots = (
            db.query(Shot)
            .filter(Shot.asset_id == asset_id, Shot.deleted_at.is_(None))
            .order_by(Shot.shot_index)
            .all()
        )
        logger.info("[faces] start asset_id=%s shots=%d", asset_id, len(shots))

        total_tracks = 0
        try:
            _get_face_app()  # warm up

            for shot in shots:
                keyframes = (
                    db.query(Keyframe)
                    .filter(Keyframe.shot_id == shot.id, Keyframe.deleted_at.is_(None))
                    .order_by(Keyframe.pts)
                    .all()
                )
                if not keyframes:
                    continue

                img_w = asset.width or 1920
                img_h = asset.height or 1080

                kf_detections = []
                for kf in keyframes:
                    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                    tmp.close()
                    try:
                        bucket_key = kf.path.removeprefix("media-keyframes/")
                        store_client.download_file("media-keyframes", bucket_key, tmp.name)
                        dets = _detect_on_image(tmp.name, img_w, img_h)
                        kf_detections.append((kf.pts, dets))
                    finally:
                        try:
                            os.unlink(tmp.name)
                        except OSError:
                            pass

                tracks = _group_into_tracks(kf_detections)
                logger.info("[faces] shot %s: %d tracks", shot.id, len(tracks))

                # Delete old face_tracks for this shot (idempotent re-run)
                db.query(FaceTrack).filter(
                    FaceTrack.shot_id == shot.id,
                    FaceTrack.deleted_at.is_(None),
                ).delete()

                for idx, track in enumerate(tracks):
                    ft = FaceTrack(
                        shot_id=shot.id,
                        asset_id=asset_id,
                        track_index=idx,
                        start_pts=track["start_pts"],
                        end_pts=track["end_pts"],
                        bbox_series=track["bbox_series"],
                        embedding=track["embedding"],
                        confidence=track["confidence"],
                        producer_model=PRODUCER_MODEL,
                        producer_version=PRODUCER_VERSION,
                    )
                    db.add(ft)
                    total_tracks += 1

            db.commit()
            logger.info("[faces] done asset_id=%s total_tracks=%d", asset_id, total_tracks)

            # Write to Qdrant
            _write_face_qdrant(asset_id, asset.project_id, db)

        except Exception as exc:
            logger.exception("[faces] failed asset_id=%s", asset_id)
            db.rollback()
            raise

    _mark_enrich_done_if_ready(asset_id, "face")


def _write_face_qdrant(asset_id: str, project_id: str, db):
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct, VectorParams, Distance, PayloadSchemaType

    client = QdrantClient(url=settings.qdrant_url)

    # Ensure collection exists
    existing = {c.name for c in client.get_collections().collections}
    if "face_insightface" not in existing:
        client.create_collection(
            collection_name="face_insightface",
            vectors_config=VectorParams(size=512, distance=Distance.COSINE),
        )
        for field in ["asset_id", "shot_id", "character_id", "project_id"]:
            client.create_payload_index(
                collection_name="face_insightface",
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        client.create_payload_index(
            collection_name="face_insightface",
            field_name="deleted",
            field_schema=PayloadSchemaType.BOOL,
        )

    tracks = (
        db.query(FaceTrack)
        .filter(FaceTrack.asset_id == asset_id, FaceTrack.deleted_at.is_(None))
        .all()
    )
    if not tracks:
        return

    points = []
    for ft in tracks:
        if not ft.embedding:
            continue
        point_id = str(uuid.uuid4())
        ft.qdrant_point_id = point_id
        points.append(PointStruct(
            id=point_id,
            vector=ft.embedding,
            payload={
                "asset_id": asset_id,
                "shot_id": ft.shot_id,
                "project_id": project_id,
                "character_id": ft.character_id,
                "track_index": ft.track_index,
                "confidence": ft.confidence,
                "deleted": False,
            },
        ))

    if points:
        client.upsert(collection_name="face_insightface", points=points)
    db.commit()
    logger.info("[faces] upserted %d face points to Qdrant", len(points))


def _mark_enrich_done_if_ready(asset_id: str, completed: str):
    """After face/object/meta stages finish, check if all three are done."""
    from workers.rq_config import ingest_queue
    from ingest.shot_meta import compute_shot_metadata
    # Enrich completion is tracked via _enrich_barrier
    _enrich_barrier(asset_id)


def _enrich_barrier(asset_id: str):
    """
    Tracks enrichment sub-stage completion via Redis.
    When all three sub-stages (face, obj, meta) are done, marks asset complete.
    Uses a simple counter in Redis.
    """
    from workers.rq_config import redis_conn
    key = f"enrich_barrier:{asset_id}"
    count = redis_conn.incr(key)
    redis_conn.expire(key, 3600)  # 1-hour TTL
    if count >= 3:
        redis_conn.delete(key)
        _finalize_enrichment(asset_id)


def _finalize_enrichment(asset_id: str):
    from sqlalchemy import text
    with get_sync_db() as db:
        db.execute(
            text("UPDATE asset SET ingest_state='complete', enrich_done=true WHERE id=:id"),
            {"id": asset_id},
        )
        db.commit()
    logger.info("[faces] enrichment complete → asset %s state=complete", asset_id)

    # Enqueue synthetic query generation (additive — does not gate on completion)
    try:
        from workers.rq_config import embed_queue
        embed_queue.enqueue(
            "ingest.synthetic.generate_synthetic_queries",
            asset_id,
            job_timeout=7200,
        )
        logger.info("[faces] enqueued synthetic query generation for asset %s", asset_id)
    except Exception:
        logger.warning("[faces] failed to enqueue synthetic generation for %s", asset_id, exc_info=True)
