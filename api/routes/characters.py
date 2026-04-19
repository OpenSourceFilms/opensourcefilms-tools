"""
Character clustering and editor endpoints.

POST /projects/{project_id}/cluster_characters
    Run DBSCAN on face embeddings for all assets in the project.
    Creates/updates Character rows and sets face_track.character_id.

GET  /projects/{project_id}/characters
    List all characters for the project.

GET  /projects/{project_id}/characters/{character_id}
    Get a single character with face track summary.

PATCH /projects/{project_id}/characters/{character_id}
    Update name / confirmed flag.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.sync_session import get_sync_db
from db.models import Project, Character, FaceTrack
from config.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── schemas ──────────────────────────────────────────────────────────────────

class CharacterOut(BaseModel):
    id: str
    project_id: str
    name: Optional[str]
    cluster_label: str
    reference_count: int
    confirmed: bool

    class Config:
        from_attributes = True


class CharacterPatch(BaseModel):
    name: Optional[str] = None
    confirmed: Optional[bool] = None


class ClusterResult(BaseModel):
    characters_created: int
    characters_updated: int
    face_tracks_assigned: int


# ─── helpers ──────────────────────────────────────────────────────────────────

def _run_dbscan(embeddings, eps: float, min_samples: int):
    """
    Returns label array (-1 = noise). Works on cosine distance.
    """
    import numpy as np
    from sklearn.cluster import DBSCAN

    embs = np.array(embeddings, dtype=np.float32)
    # Normalise so that cosine distance = euclidean distance / 2 in unit sphere
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    embs_norm = embs / norms

    # DBSCAN with cosine metric directly
    db = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine", n_jobs=-1)
    labels = db.fit_predict(embs_norm)
    return labels


# ─── routes ───────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/cluster_characters", response_model=ClusterResult)
def cluster_characters(project_id: str):
    with get_sync_db() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="project not found")

        face_tracks = (
            db.query(FaceTrack)
            .filter(FaceTrack.asset_id.in_(
                [a.id for a in project.assets]
            ), FaceTrack.deleted_at.is_(None))
            .all()
        )

        tracks_with_emb = [ft for ft in face_tracks if ft.embedding]
        if not tracks_with_emb:
            logger.info("[cluster] no face tracks with embeddings for project %s", project_id)
            return ClusterResult(characters_created=0, characters_updated=0, face_tracks_assigned=0)

        embeddings = [ft.embedding for ft in tracks_with_emb]
        labels = _run_dbscan(
            embeddings,
            eps=settings.face_dbscan_eps,
            min_samples=settings.face_dbscan_min_samples,
        )

        # Map cluster_label → Character (create or reuse)
        existing_chars = {
            c.cluster_label: c
            for c in db.query(Character).filter(Character.project_id == project_id).all()
        }
        created = 0
        updated = 0
        assigned = 0

        label_to_char: dict[int, Character] = {}
        for label in set(labels):
            if label == -1:
                continue  # noise — leave unassigned
            cluster_label = f"cluster_{label}"
            if cluster_label in existing_chars:
                char = existing_chars[cluster_label]
                updated += 1
            else:
                char = Character(
                    project_id=project_id,
                    cluster_label=cluster_label,
                )
                db.add(char)
                created += 1
            label_to_char[label] = char

        db.flush()  # assign PKs to new Character rows

        for ft, label in zip(tracks_with_emb, labels):
            if label == -1:
                ft.character_id = None
            else:
                ft.character_id = label_to_char[label].id
                assigned += 1

        # Update reference counts
        for label, char in label_to_char.items():
            count = int((labels == label).sum())
            char.reference_count = count

        db.commit()
        logger.info(
            "[cluster] project %s: created=%d updated=%d assigned=%d",
            project_id, created, updated, assigned,
        )
        return ClusterResult(
            characters_created=created,
            characters_updated=updated,
            face_tracks_assigned=assigned,
        )


@router.get("/projects/{project_id}/characters", response_model=list[CharacterOut])
def list_characters(project_id: str):
    with get_sync_db() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        chars = (
            db.query(Character)
            .filter(Character.project_id == project_id)
            .order_by(Character.cluster_label)
            .all()
        )
        return [CharacterOut.model_validate(c) for c in chars]


@router.get("/projects/{project_id}/characters/{character_id}", response_model=CharacterOut)
def get_character(project_id: str, character_id: str):
    with get_sync_db() as db:
        char = (
            db.query(Character)
            .filter(Character.id == character_id, Character.project_id == project_id)
            .first()
        )
        if not char:
            raise HTTPException(status_code=404, detail="character not found")
        return CharacterOut.model_validate(char)


@router.patch("/projects/{project_id}/characters/{character_id}", response_model=CharacterOut)
def patch_character(project_id: str, character_id: str, body: CharacterPatch):
    with get_sync_db() as db:
        char = (
            db.query(Character)
            .filter(Character.id == character_id, Character.project_id == project_id)
            .first()
        )
        if not char:
            raise HTTPException(status_code=404, detail="character not found")
        if body.name is not None:
            char.name = body.name
        if body.confirmed is not None:
            char.confirmed = body.confirmed
        db.commit()
        db.refresh(char)
        return CharacterOut.model_validate(char)
