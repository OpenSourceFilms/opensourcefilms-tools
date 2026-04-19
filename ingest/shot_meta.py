"""
Stage 6c: compute_shot_metadata(asset_id)

CPU-only. Computes per-shot:
  - motion_score  (mean optical flow magnitude between consecutive keyframes)
  - mean_brightness, mean_contrast (Y-channel stats over keyframes)
  - palette_hex   (k-means k=5 in LAB space → hex colours, sorted by frequency)
  - shot_size_class (heuristic from face bbox height: ECU/CU/MCU/MS/LS/ELS/unknown)

Updates shot rows directly; does not write Qdrant.
"""
import logging
import os
import tempfile

import numpy as np

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Shot, Keyframe, FaceTrack

logger = logging.getLogger(__name__)

# Shot-size heuristic: classify by the tallest face in the shot
# (face bbox h as fraction of frame height)
_SIZE_THRESHOLDS = [
    (0.75, "ECU"),   # extreme close-up: face fills > 75% of height
    (0.50, "CU"),    # close-up
    (0.30, "MCU"),   # medium close-up
    (0.20, "MS"),    # medium shot
    (0.10, "LS"),    # long shot
    (0.00, "ELS"),   # extreme long shot / no face
]


def _shot_size_from_face_tracks(face_tracks) -> str:
    """Classify shot size from tallest face bbox in the shot."""
    max_h = 0.0
    for ft in face_tracks:
        if ft.bbox_series:
            for entry in ft.bbox_series:
                max_h = max(max_h, entry.get("bbox", {}).get("h", 0.0))
    if max_h == 0.0:
        return "unknown"
    for threshold, label in _SIZE_THRESHOLDS:
        if max_h >= threshold:
            return label
    return "ELS"


def _load_gray(path: str):
    """Load image as uint8 grayscale ndarray, or None on failure."""
    import cv2
    img = cv2.imread(path)
    if img is None:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _load_rgb(path: str):
    import cv2
    img = cv2.imread(path)
    if img is None:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def _optical_flow_score(gray1, gray2) -> float:
    """
    Dense Farneback optical flow magnitude between two grayscale frames.
    Returns mean magnitude.
    """
    import cv2
    flow = cv2.calcOpticalFlowFarneback(
        gray1, gray2,
        None,
        pyr_scale=0.5, levels=3, winsize=15,
        iterations=3, poly_n=5, poly_sigma=1.2,
        flags=0,
    )
    mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
    return float(mag.mean())


def _palette_hex(rgb_frames: list, k: int = 5) -> list[str]:
    """
    K-means palette extraction in LAB colour space.
    Returns list of k hex strings sorted by cluster size (largest first).
    """
    import cv2
    from sklearn.cluster import KMeans

    pixels = []
    for rgb in rgb_frames:
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
        # downsample to 1/4 of pixels for speed
        h, w = lab.shape[:2]
        sampled = lab[::2, ::2].reshape(-1, 3)
        pixels.append(sampled)

    if not pixels:
        return []

    all_pixels = np.concatenate(pixels, axis=0).astype(np.float32)
    n_clusters = min(k, len(all_pixels))
    if n_clusters < 1:
        return []

    km = KMeans(n_clusters=n_clusters, n_init=3, random_state=42)
    km.fit(all_pixels)

    centres_lab = km.cluster_centers_.astype(np.uint8)
    counts = np.bincount(km.labels_)
    order = np.argsort(-counts)

    hexes = []
    for i in order:
        lab_px = centres_lab[i].reshape(1, 1, 3)
        rgb_px = cv2.cvtColor(lab_px, cv2.COLOR_LAB2RGB)[0, 0]
        hexes.append("#{:02x}{:02x}{:02x}".format(int(rgb_px[0]), int(rgb_px[1]), int(rgb_px[2])))
    return hexes


def compute_shot_metadata(asset_id: str):
    import store.client as store_client

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[shot_meta] asset not found: %s", asset_id)
            return

        shots = (
            db.query(Shot)
            .filter(Shot.asset_id == asset_id, Shot.deleted_at.is_(None))
            .order_by(Shot.shot_index)
            .all()
        )
        logger.info("[shot_meta] start asset_id=%s shots=%d", asset_id, len(shots))

        try:
            for shot in shots:
                keyframes = (
                    db.query(Keyframe)
                    .filter(Keyframe.shot_id == shot.id, Keyframe.deleted_at.is_(None))
                    .order_by(Keyframe.pts)
                    .all()
                )
                if not keyframes:
                    continue

                face_tracks = (
                    db.query(FaceTrack)
                    .filter(FaceTrack.shot_id == shot.id, FaceTrack.deleted_at.is_(None))
                    .all()
                )

                # Download keyframe images to temp files
                tmp_paths = []
                for kf in keyframes:
                    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                    tmp.close()
                    try:
                        bucket_key = kf.path.removeprefix("media-keyframes/")
                        store_client.download_file("media-keyframes", bucket_key, tmp.name)
                        tmp_paths.append(tmp.name)
                    except Exception:
                        logger.warning("[shot_meta] failed to download keyframe %s", kf.id)
                        try:
                            os.unlink(tmp.name)
                        except OSError:
                            pass

                if not tmp_paths:
                    continue

                try:
                    grays = [_load_gray(p) for p in tmp_paths]
                    grays = [g for g in grays if g is not None]
                    rgbs = [_load_rgb(p) for p in tmp_paths]
                    rgbs = [r for r in rgbs if r is not None]

                    # Motion score: mean of pairwise flow magnitudes
                    flow_scores = []
                    for i in range(len(grays) - 1):
                        flow_scores.append(_optical_flow_score(grays[i], grays[i + 1]))
                    motion = float(np.mean(flow_scores)) if flow_scores else 0.0

                    # Brightness + contrast from Y channel of all frames
                    y_vals = []
                    for gray in grays:
                        y_vals.append(gray.astype(np.float32))
                    if y_vals:
                        all_y = np.concatenate([y.ravel() for y in y_vals])
                        brightness = float(all_y.mean())
                        contrast = float(all_y.std())
                    else:
                        brightness = 0.0
                        contrast = 0.0

                    # Palette
                    palette = _palette_hex(rgbs) if rgbs else []

                    # Shot size class
                    size_class = _shot_size_from_face_tracks(face_tracks)

                    shot.motion_score = round(motion, 4)
                    shot.mean_brightness = round(brightness, 4)
                    shot.mean_contrast = round(contrast, 4)
                    shot.palette_hex = palette
                    shot.shot_size_class = size_class

                    logger.info(
                        "[shot_meta] shot %s: motion=%.3f bright=%.1f contrast=%.1f size=%s palette=%s",
                        shot.id, motion, brightness, contrast, size_class, palette[:2],
                    )
                finally:
                    for p in tmp_paths:
                        try:
                            os.unlink(p)
                        except OSError:
                            pass

            db.commit()
            logger.info("[shot_meta] done asset_id=%s", asset_id)

        except Exception:
            logger.exception("[shot_meta] failed asset_id=%s", asset_id)
            db.rollback()
            raise

    _mark_enrich_done_if_ready(asset_id)


def _mark_enrich_done_if_ready(asset_id: str):
    from ingest.faces import _enrich_barrier
    _enrich_barrier(asset_id)
