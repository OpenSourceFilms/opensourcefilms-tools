import hashlib
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Header
from sqlalchemy import select

from config.settings import settings
from db.base import AsyncSessionLocal
from db.models import Asset, Project, Shot, Keyframe, TranscriptTurn
import store.client as store

router = APIRouter()

ORIGINALS_ROOT = Path("/workspace/media/originals")
ORIGINALS_ROOT.mkdir(parents=True, exist_ok=True)


def _sha256_stream(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@router.post("/{project_id}/assets", status_code=202)
async def upload_asset(
    project_id: str,
    file: UploadFile = File(...),
    x_project_id: str = Header(default=""),
):
    from ingest.probe import probe_asset

    async with AsyncSessionLocal() as db:
        proj = await db.execute(select(Project).where(Project.id == project_id))
        if not proj.scalar_one_or_none():
            raise HTTPException(404, "project not found")

    # Stream to temp file first for sha256
    tmp = tempfile.NamedTemporaryFile(delete=False, dir="/tmp", suffix=Path(file.filename).suffix)
    try:
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
        tmp.flush()
        tmp.close()

        sha = _sha256_stream(tmp.name)

        async with AsyncSessionLocal() as db:
            dup = await db.execute(select(Asset).where(Asset.sha256 == sha))
            if dup.scalar_one_or_none():
                raise HTTPException(409, "asset with this sha256 already ingested")

        dest_dir = ORIGINALS_ROOT / project_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{sha[:16]}_{file.filename}"
        shutil.move(tmp.name, str(dest))

        # Upload to MinIO
        store.upload_file(str(dest), "media-originals", f"{project_id}/{dest.name}")

        async with AsyncSessionLocal() as db:
            asset = Asset(
                project_id=project_id,
                filename=file.filename,
                original_path=str(dest),
                sha256=sha,
                ingest_state="pending",
            )
            db.add(asset)
            await db.commit()
            await db.refresh(asset)
            asset_id = asset.id

        from workers.rq_config import ingest_queue
        ingest_queue.enqueue(probe_asset, asset_id, job_timeout=600)

        return {
            "asset_id": asset_id,
            "filename": file.filename,
            "ingest_state": "pending",
            "message": "ingest queued",
        }
    except HTTPException:
        raise
    except Exception as exc:
        import os
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise HTTPException(500, str(exc)) from exc


@router.get("/{project_id}/assets")
async def list_assets(project_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Asset)
            .where(Asset.project_id == project_id, Asset.deleted_at.is_(None))
            .order_by(Asset.created_at.desc())
        )
        assets = result.scalars().all()
        return [
            {
                "id": a.id,
                "filename": a.filename,
                "ingest_state": a.ingest_state,
                "duration_s": a.duration_s,
                "shot_count": a.shot_count,
                "created_at": a.created_at,
                "ingest_error": a.ingest_error,
            }
            for a in assets
        ]


@router.get("/{project_id}/assets/{asset_id}")
async def get_asset(project_id: str, asset_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Asset).where(Asset.id == asset_id, Asset.project_id == project_id)
        )
        asset = result.scalar_one_or_none()
        if not asset:
            raise HTTPException(404, "asset not found")

        shots_result = await db.execute(
            select(Shot)
            .where(Shot.asset_id == asset_id, Shot.deleted_at.is_(None))
            .order_by(Shot.shot_index)
        )
        shots = shots_result.scalars().all()

        return {
            "id": asset.id,
            "filename": asset.filename,
            "ingest_state": asset.ingest_state,
            "ingest_error": asset.ingest_error,
            "duration_s": asset.duration_s,
            "fps": asset.fps,
            "width": asset.width,
            "height": asset.height,
            "codec": asset.codec,
            "container": asset.container,
            "shot_count": asset.shot_count,
            "created_at": asset.created_at,
            "shots": [
                {
                    "id": s.id,
                    "shot_index": s.shot_index,
                    "start_pts": s.start_pts,
                    "end_pts": s.end_pts,
                    "duration_s": s.duration_s,
                }
                for s in shots
            ],
        }
