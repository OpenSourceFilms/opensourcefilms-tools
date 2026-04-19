"""
Stage 6b: detect_objects(asset_id)

Runs GroundingDINO on one representative keyframe per shot.
Writes ObjectTrack rows and saves 224×224 crops to MinIO.
Skips gracefully if GroundingDINO weights are absent.
"""
import logging
import os
import tempfile
import uuid

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Shot, Keyframe, ObjectTrack

logger = logging.getLogger(__name__)

PRODUCER_MODEL = "groundingdino/swint_ogc"
PRODUCER_VERSION = "0.1"


def _weights_present() -> bool:
    return (
        os.path.isfile(settings.groundingdino_weights)
        and os.path.isfile(settings.groundingdino_config)
    )


_gdino_model = None


def _get_gdino():
    global _gdino_model
    if _gdino_model is None:
        from groundingdino.util.inference import load_model
        _gdino_model = load_model(
            settings.groundingdino_config,
            settings.groundingdino_weights,
        )
        logger.info("[objects] GroundingDINO loaded")
    return _gdino_model


def _run_gdino(image_path: str, labels: str, img_w: int, img_h: int) -> list[dict]:
    """
    Returns list of {label, bbox {x,y,w,h} normalised, confidence}.
    """
    from groundingdino.util.inference import predict
    import cv2
    import torch
    from torchvision.transforms.functional import to_tensor

    model = _get_gdino()

    img_bgr = cv2.imread(image_path)
    if img_bgr is None:
        return []
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

    # GroundingDINO expects a PIL-like image tensor; use its built-in loader
    from groundingdino.util.inference import load_image
    _, image_tensor = load_image(image_path)

    boxes, logits, phrases = predict(
        model=model,
        image=image_tensor,
        caption=labels,
        box_threshold=settings.groundingdino_confidence,
        text_threshold=settings.groundingdino_confidence,
    )

    results = []
    for box, logit, phrase in zip(boxes.tolist(), logits.tolist(), phrases):
        cx, cy, bw, bh = box  # cx/cy/w/h in [0,1] relative
        x = cx - bw / 2
        y = cy - bh / 2
        results.append({
            "label": phrase.strip(),
            "bbox": {
                "x": round(max(0.0, x), 4),
                "y": round(max(0.0, y), 4),
                "w": round(min(1.0, bw), 4),
                "h": round(min(1.0, bh), 4),
            },
            "confidence": round(float(logit), 4),
        })
    return results


def _save_crop(img_path: str, bbox: dict, img_w: int, img_h: int) -> str | None:
    """Crop bbox from image, resize to 224×224, save to MinIO, return bucket key."""
    import cv2
    import store.client as store_client

    img = cv2.imread(img_path)
    if img is None:
        return None

    x1 = int(bbox["x"] * img_w)
    y1 = int(bbox["y"] * img_h)
    x2 = int((bbox["x"] + bbox["w"]) * img_w)
    y2 = int((bbox["y"] + bbox["h"]) * img_h)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(img_w, x2), min(img_h, y2)

    if x2 <= x1 or y2 <= y1:
        return None

    crop = img[y1:y2, x1:x2]
    crop_resized = cv2.resize(crop, (224, 224), interpolation=cv2.INTER_AREA)

    crop_key = f"crops/{uuid.uuid4()}.jpg"
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        cv2.imwrite(tmp.name, crop_resized, [cv2.IMWRITE_JPEG_QUALITY, 90])
        store_client.upload_file(tmp.name, "media-crops", crop_key)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    return f"media-crops/{crop_key}"


def detect_objects(asset_id: str):
    import store.client as store_client

    if not settings.object_detection_enabled:
        logger.info("[objects] disabled — skipping asset_id=%s", asset_id)
        _mark_enrich_done_if_ready(asset_id)
        return

    if not _weights_present():
        logger.warning(
            "[objects] GroundingDINO weights absent at %s — skipping stage 6b for asset_id=%s",
            settings.groundingdino_weights,
            asset_id,
        )
        _mark_enrich_done_if_ready(asset_id)
        return

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[objects] asset not found: %s", asset_id)
            return

        shots = (
            db.query(Shot)
            .filter(Shot.asset_id == asset_id, Shot.deleted_at.is_(None))
            .order_by(Shot.shot_index)
            .all()
        )
        logger.info("[objects] start asset_id=%s shots=%d", asset_id, len(shots))

        img_w = asset.width or 1920
        img_h = asset.height or 1080
        labels = settings.groundingdino_default_labels
        total_tracks = 0

        try:
            _get_gdino()  # warm up

            for shot in shots:
                # Use the middle keyframe as representative
                keyframes = (
                    db.query(Keyframe)
                    .filter(Keyframe.shot_id == shot.id, Keyframe.deleted_at.is_(None))
                    .order_by(Keyframe.pts)
                    .all()
                )
                if not keyframes:
                    continue

                rep_kf = keyframes[len(keyframes) // 2]

                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp.close()
                try:
                    bucket_key = rep_kf.path.removeprefix("media-keyframes/")
                    store_client.download_file("media-keyframes", bucket_key, tmp.name)
                    dets = _run_gdino(tmp.name, labels, img_w, img_h)
                    logger.info("[objects] shot %s: %d detections", shot.id, len(dets))

                    # Delete old object_tracks for this shot (idempotent re-run)
                    db.query(ObjectTrack).filter(
                        ObjectTrack.shot_id == shot.id,
                        ObjectTrack.deleted_at.is_(None),
                    ).delete()

                    for idx, det in enumerate(dets):
                        crop_path = _save_crop(tmp.name, det["bbox"], img_w, img_h)
                        ot = ObjectTrack(
                            shot_id=shot.id,
                            asset_id=asset_id,
                            track_index=idx,
                            label=det["label"],
                            confidence=det["confidence"],
                            start_pts=shot.start_pts,
                            end_pts=shot.end_pts,
                            bbox_series=[{"pts": rep_kf.pts, "bbox": det["bbox"]}],
                            crop_path=crop_path,
                            producer_model=PRODUCER_MODEL,
                            producer_version=PRODUCER_VERSION,
                        )
                        db.add(ot)
                        total_tracks += 1
                finally:
                    try:
                        os.unlink(tmp.name)
                    except OSError:
                        pass

            db.commit()
            logger.info("[objects] done asset_id=%s total_tracks=%d", asset_id, total_tracks)

        except Exception:
            logger.exception("[objects] failed asset_id=%s", asset_id)
            db.rollback()
            raise

    _mark_enrich_done_if_ready(asset_id)


def _mark_enrich_done_if_ready(asset_id: str):
    from ingest.faces import _enrich_barrier
    _enrich_barrier(asset_id)
