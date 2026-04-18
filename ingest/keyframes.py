"""
Stage 3a: extract_keyframes
Extracts N keyframes per shot, deduplicates by perceptual hash,
stores in MinIO, writes Keyframe rows. Enqueues run_ocr on completion.
"""
import logging
import os
import tempfile

import cv2
import dramatiq
import imagehash
from PIL import Image

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Shot, Keyframe
import store.client as store

logger = logging.getLogger(__name__)


def _compute_phash(img_path: str) -> str:
    return str(imagehash.phash(Image.open(img_path)))


def _extract_frames(video_path: str, start_pts: float, end_pts: float, n: int) -> list[tuple[float, str]]:
    duration = end_pts - start_pts
    if duration <= 0:
        return []
    pts_list = [start_pts + duration * (i + 0.5) / n for i in range(n)]
    results = []
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    for pts in pts_list:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(pts * fps))
        ret, frame = cap.read()
        if not ret:
            continue
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        cv2.imwrite(tmp.name, frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        results.append((round(pts, 4), tmp.name))
    cap.release()
    return results


@dramatiq.actor(queue_name="ingest", max_retries=2)
def extract_keyframes(asset_id: str):
    from ingest.ocr import run_ocr

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[keyframes] asset not found: %s", asset_id)
            return

        shots = (
            db.query(Shot)
            .filter(Shot.asset_id == asset_id, Shot.deleted_at.is_(None))
            .order_by(Shot.shot_index)
            .all()
        )

        video_path = asset.proxy_path or asset.original_path
        n = settings.keyframes_per_shot
        logger.info("[keyframes] start asset_id=%s shots=%d n=%d", asset_id, len(shots), n)

        seen: set[str] = set()
        total = 0

        try:
            for shot in shots:
                frames = _extract_frames(video_path, shot.start_pts, shot.end_pts, n)
                for pts, tmp_path in frames:
                    try:
                        phash = _compute_phash(tmp_path)
                        if phash in seen:
                            continue
                        seen.add(phash)

                        key = f"{asset_id}/{shot.id}/{pts:.3f}.jpg"
                        store.upload_file(tmp_path, "media-keyframes", key)

                        db.add(Keyframe(
                            shot_id=shot.id,
                            asset_id=asset_id,
                            pts=pts,
                            path=f"media-keyframes/{key}",
                            phash=phash,
                        ))
                        total += 1
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

            db.commit()
            logger.info("[keyframes] done asset_id=%s keyframes=%d", asset_id, total)
        except Exception as exc:
            logger.exception("[keyframes] failed asset_id=%s", asset_id)
            asset = db.query(Asset).filter(Asset.id == asset_id).first()
            if asset:
                asset.ingest_state = "failed"
                asset.ingest_error = f"keyframes: {exc}"
                db.commit()
            raise

    run_ocr.send(asset_id)
