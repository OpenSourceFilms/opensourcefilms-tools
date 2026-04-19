"""
Stage 4: run_ocr
Runs PaddleOCR on every keyframe. Writes OcrSpan rows.
On completion, triggers embed if ASR is also done (atomic CAS on state).
"""
import logging
import os
import tempfile

from sqlalchemy import text

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Keyframe, OcrSpan
from ingest.gpu_pod_manager import gpu_available

logger = logging.getLogger(__name__)
PADDLEOCR_VERSION = "3.x"
_paddle = None
_paddle_broken = False


def _get_paddle():
    global _paddle, _paddle_broken
    if _paddle_broken:
        return None
    if _paddle is None:
        try:
            from paddleocr import PaddleOCR
            use_gpu = gpu_available()
            # v3 API: lang is still accepted, no use_angle_cls/use_gpu/show_log
            _paddle = PaddleOCR(
                lang=settings.paddleocr_lang,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
            )
            logger.info("[ocr] PaddleOCR loaded (GPU=%s)", use_gpu)
        except Exception as exc:
            logger.warning("[ocr] PaddleOCR init failed (%s) — OCR will be skipped", exc)
            _paddle_broken = True
            return None
    return _paddle


def _ocr_image(image_path: str) -> list[dict]:
    paddle = _get_paddle()
    if paddle is None:
        return []
    from PIL import Image
    img = Image.open(image_path)
    w, h = img.size
    try:
        results = paddle.predict(image_path)
    except Exception as exc:
        logger.warning("[ocr] predict failed for %s: %s", image_path, exc)
        return []
    spans = []
    for page_result in (results or []):
        # v3 result: list of OCRResult objects with rec_texts, rec_scores, det_polys
        rec_texts = getattr(page_result, "rec_texts", None) or []
        rec_scores = getattr(page_result, "rec_scores", None) or []
        det_polys = getattr(page_result, "det_polys", None) or []
        for text, conf, poly in zip(rec_texts, rec_scores, det_polys):
            if conf < settings.ocr_confidence_threshold:
                continue
            # poly is list of [x,y] points
            try:
                xs = [p[0] for p in poly]
                ys = [p[1] for p in poly]
            except (TypeError, IndexError):
                continue
            spans.append({
                "text": text,
                "bbox": {
                    "x": round(min(xs) / w, 4), "y": round(min(ys) / h, 4),
                    "w": round((max(xs) - min(xs)) / w, 4), "h": round((max(ys) - min(ys)) / h, 4),
                },
                "confidence": round(conf, 4),
            })
    return spans


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

            # Atomic: mark ocr_done=true, transition to embedding if asr_done too
            row = db.execute(
                text("""
                    UPDATE asset
                    SET ocr_done = true,
                        ingest_state = CASE WHEN asr_done THEN 'embedding' ELSE ingest_state END
                    WHERE id = :id AND ingest_state NOT IN ('failed', 'embedding', 'complete')
                    RETURNING ingest_state
                """),
                {"id": asset_id},
            ).fetchone()
            db.commit()

            if row and row[0] == "embedding":
                logger.info("[ocr] both asr+ocr done → queuing embed asset_id=%s", asset_id)
                _enqueue_embed(asset_id)

        except Exception as exc:
            logger.exception("[ocr] failed asset_id=%s", asset_id)
            asset = db.query(Asset).filter(Asset.id == asset_id).first()
            if asset:
                asset.ingest_state = "failed"
                asset.ingest_error = f"ocr: {exc}"
                db.commit()
            raise


def _enqueue_embed(asset_id: str):
    from workers.rq_config import embed_queue
    from ingest.embed import embed_asset
    embed_queue.enqueue(embed_asset, asset_id, job_timeout=7200)
