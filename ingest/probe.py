"""
Stage 1: probe_asset
Runs ffprobe on the asset file, extracts media metadata, advances state to 'segmenting'.
"""
import hashlib
import json
import logging
import subprocess

import dramatiq

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset

logger = logging.getLogger(__name__)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ffprobe(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr}")
    return json.loads(result.stdout)


def _extract_meta(probe: dict) -> dict:
    fmt = probe.get("format", {})
    video = next(
        (s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    duration = float(fmt.get("duration") or video.get("duration") or 0) or None
    fps = None
    if "r_frame_rate" in video:
        num, den = video["r_frame_rate"].split("/")
        fps = round(int(num) / max(int(den), 1), 4)
    return {
        "duration_s": duration,
        "fps": fps,
        "width": video.get("width"),
        "height": video.get("height"),
        "codec": video.get("codec_name"),
        "container": fmt.get("format_name", "").split(",")[0] or None,
    }


@dramatiq.actor(queue_name="ingest", max_retries=2)
def probe_asset(asset_id: str):
    from ingest.segment import segment_shots

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[probe] asset not found: %s", asset_id)
            return

        logger.info("[probe] start asset_id=%s file=%s", asset_id, asset.original_path)
        asset.ingest_state = "probing"
        db.commit()

        try:
            probe_data = _ffprobe(asset.original_path)
            meta = _extract_meta(probe_data)
            sha = _sha256(asset.original_path)

            asset.duration_s = meta["duration_s"]
            asset.fps = meta["fps"]
            asset.width = meta["width"]
            asset.height = meta["height"]
            asset.codec = meta["codec"]
            asset.container = meta["container"]
            asset.sha256 = sha
            asset.ingest_state = "segmenting"
            asset.ingest_error = None
            db.commit()

            logger.info(
                "[probe] done asset_id=%s duration=%.1fs fps=%s %sx%s codec=%s",
                asset_id, meta["duration_s"] or 0, meta["fps"],
                meta["width"], meta["height"], meta["codec"],
            )
        except Exception as exc:
            logger.exception("[probe] failed asset_id=%s", asset_id)
            asset.ingest_state = "failed"
            asset.ingest_error = str(exc)
            db.commit()
            raise

    segment_shots.send(asset_id)
