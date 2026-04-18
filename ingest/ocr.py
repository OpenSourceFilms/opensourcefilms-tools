"""
Stage 4: run_ocr
Runs PaddleOCR on every keyframe. Writes OcrSpan rows.
Advances asset state to 'complete' after finishing (regardless of whether ASR completed).
In practice, ASR and keyframe extraction are parallel — OCR fires after keyframes,
so 'complete' means keyframes+OCR done; ASR may still be writing concurrently.
A Phase 1 job can re-check and set to 'enriching' once both ASR and OCR are verified.
"""
import logging
import os
import tempfile

import dramatiq

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Keyframe, OcrSpan
from ingest.gpu_pod_manager import gpu_available

logger = logging.getLogger(__name__)
PADDLEOCR_VERSION = "2.8"
_paddle = None


def _get_paddle():
    global _paddle
    if _paddle is None:
        from paddleocr import PaddleOCR
        _paddle = PaddleOCR(use_angle_cls=True, lang=settings.paddleocr_lang, show_log=False)
        logger.info("[ocr] PaddleOCR loaded")
    return _paddle


def _ocr_image(image_path: str) -> list[dict]:
    from PIL import Image
    img = Image.open(image_path)
    w, h = img.size
    result = _get_paddle().ocr(image_path, cls=True)
    spans = []
    if not result or not result[0]:
        return spans
    for line in result[0]:
        if not line:
            continue
        box, (text, conf) = line
        if conf < settings.ocr_confidence_threshold:
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        spans.append({
            "text": text,
            "bbox": {
                "x": round(min(xs) / w, 4), "y": round(min(ys) / h, 4),
                "w": round((max(xs) - min(xs)) / w, 4), "h": round((max(ys) - min(ys)) / h, 4),
            },
            "confidence": round(conf, 4),
        })
    return spans


@dramatiq.actor(queue_name="ingest_gpu", max_retries=2, time_limit=3_600_000)
def run_ocr(asset_id: str):
    import store.client as store_client

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[ocr] asset not found: %s", asset_id)
            return

        keyframes = (
            db.query(Keyframe)
            .filter(Keyframe.asset_id == asset_id, Keyframe.deleted_at.is_(None))
            .order_by(Keyframe.pts)
            .all()
        )
        logger.info("[ocr] start asset_id=%s keyframes=%d", asset_id, len(keyframes))

        try:
            # If PaddleOCR not installed, skip OCR and advance state
            try:
                import paddleocr  # noqa
                paddle_available = True
            except ImportError:
                paddle_available = False
                logger.warning("[ocr] paddleocr not installed — skipping OCR for asset_id=%s", asset_id)

            total = 0
            if paddle_available:
                for kf in keyframes:
                    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                    tmp.close()
                    try:
                        bucket_key = kf.path.removeprefix("media-keyframes/")
                        store_client.download_file("media-keyframes", bucket_key, tmp.name)
                        spans = _ocr_image(tmp.name)
                        for span in spans:
                            db.add(OcrSpan(
                                keyframe_id=kf.id,
                                asset_id=asset_id,
                                text=span["text"],
                                bbox=span["bbox"],
                                confidence=span["confidence"],
                                producer_model="paddleocr",
                                producer_version=PADDLEOCR_VERSION,
                            ))
                        total += len(spans)
                    finally:
                        try:
                            os.unlink(tmp.name)
                        except OSError:
                            pass

            db.commit()
            logger.info("[ocr] done asset_id=%s spans=%d", asset_id, total)

            # Advance to complete
            asset = db.query(Asset).filter(Asset.id == asset_id).first()
            if asset and asset.ingest_state != "failed":
                asset.ingest_state = "complete"
                asset.ingest_error = None
                db.commit()
                logger.info("[ocr] asset_id=%s → state=complete", asset_id)

        except Exception as exc:
            logger.exception("[ocr] failed asset_id=%s", asset_id)
            asset = db.query(Asset).filter(Asset.id == asset_id).first()
            if asset:
                asset.ingest_state = "failed"
                asset.ingest_error = f"ocr: {exc}"
                db.commit()
            raise
