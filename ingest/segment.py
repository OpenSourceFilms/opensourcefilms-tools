"""
Stage 2: segment_shots
Runs TransNetV2 (or PySceneDetect fallback) to detect shot boundaries.
Writes Shot rows. Enqueues extract_keyframes and run_asr in parallel.
"""
import logging
import sys
from pathlib import Path

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Shot

logger = logging.getLogger(__name__)
TRANSNETV2_VERSION = "2.0.0"


def _run_transnetv2(video_path: str, duration_s: float) -> list[tuple[float, float]]:
    weights_file = Path(settings.transnetv2_weights) / "transnetv2-weights.npz"
    if weights_file.exists():
        try:
            return _transnetv2_native(video_path, str(weights_file))
        except Exception as exc:
            logger.warning("[segment] TransNetV2 native failed (%s), falling back", exc)

    logger.info("[segment] Using PySceneDetect fallback")
    return _pyscenedetect_fallback(video_path, duration_s)


def _transnetv2_native(video_path: str, weights_path: str) -> list[tuple[float, float]]:
    sys.path.insert(0, str(Path(__file__).parent.parent / "third_party" / "transnetv2"))
    from transnetv2 import TransNetV2
    import cv2

    model = TransNetV2(model_dir=str(Path(weights_path).parent))
    video_frames, single_frame_predictions, _ = model.predict_video(video_path)
    scenes = model.predictions_to_scenes(single_frame_predictions)

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    cap.release()
    return [(round(s / fps, 4), round(e / fps, 4)) for s, e in scenes]


def _pyscenedetect_fallback(video_path: str, duration_s: float) -> list[tuple[float, float]]:
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import AdaptiveDetector

        video = open_video(video_path)
        manager = SceneManager()
        manager.add_detector(AdaptiveDetector())
        manager.detect_scenes(video, show_progress=False)
        scenes = manager.get_scene_list()
        if scenes:
            return [(round(s.get_seconds(), 4), round(e.get_seconds(), 4)) for s, e in scenes]
    except Exception as exc:
        logger.warning("[segment] PySceneDetect failed (%s), single-shot fallback", exc)

    return [(0.0, round(duration_s, 4))]


def segment_shots(asset_id: str):
    from workers.rq_config import ingest_queue, ingest_gpu_queue
    from ingest.keyframes import extract_keyframes
    from ingest.asr import run_asr

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[segment] asset not found: %s", asset_id)
            return

        video_path = asset.proxy_path or asset.original_path
        duration_s = asset.duration_s or 0.0
        logger.info("[segment] start asset_id=%s video=%s", asset_id, video_path)
        asset.ingest_state = "segmenting"
        db.commit()

        try:
            boundaries = _run_transnetv2(video_path, duration_s)

            for idx, (start, end) in enumerate(boundaries):
                db.add(Shot(
                    asset_id=asset_id,
                    shot_index=idx,
                    start_pts=start,
                    end_pts=end,
                    producer_model="transnetv2",
                    producer_version=TRANSNETV2_VERSION,
                ))

            asset.shot_count = len(boundaries)
            asset.ingest_state = "extracting"
            asset.ingest_error = None
            db.commit()

            logger.info("[segment] done asset_id=%s shots=%d", asset_id, len(boundaries))
        except Exception as exc:
            logger.exception("[segment] failed asset_id=%s", asset_id)
            asset.ingest_state = "failed"
            asset.ingest_error = str(exc)
            db.commit()
            raise

    ingest_queue.enqueue(extract_keyframes, asset_id, job_timeout=3600)
    ingest_gpu_queue.enqueue(run_asr, asset_id, job_timeout=7200)
