import uuid
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Column, String, Float, Integer, Boolean, Text, ARRAY,
    ForeignKey, UniqueConstraint, Index, func,
    Computed,
)
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB

TIMESTAMPTZ = DateTime(timezone=True)
from sqlalchemy.orm import relationship, Mapped, mapped_column

from db.base import Base


def _uuid():
    return str(uuid.uuid4())


# ──────────────────────────── mixins ────────────────────────────

class TimestampMixin:
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EvidenceMixin(TimestampMixin):
    producer_model: Mapped[str] = mapped_column(Text, nullable=False)
    producer_version: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[Optional[str]] = mapped_column(Text)
    supersedes_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False))
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)


# ──────────────────────────── tables ────────────────────────────

class Project(TimestampMixin, Base):
    __tablename__ = "project"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    assets: Mapped[List["Asset"]] = relationship("Asset", back_populates="project")


class Asset(TimestampMixin, Base):
    __tablename__ = "asset"

    project_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("project.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    proxy_path: Mapped[Optional[str]] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    duration_s: Mapped[Optional[float]] = mapped_column(Float)
    fps: Mapped[Optional[float]] = mapped_column(Float)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    codec: Mapped[Optional[str]] = mapped_column(Text)
    container: Mapped[Optional[str]] = mapped_column(Text)
    ingest_state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    ingest_error: Mapped[Optional[str]] = mapped_column(Text)
    shot_count: Mapped[Optional[int]] = mapped_column(Integer)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    project: Mapped["Project"] = relationship("Project", back_populates="assets")
    shots: Mapped[List["Shot"]] = relationship("Shot", back_populates="asset")
    scenes: Mapped[List["Scene"]] = relationship("Scene", back_populates="asset")
    transcript_turns: Mapped[List["TranscriptTurn"]] = relationship(
        "TranscriptTurn", back_populates="asset"
    )

    __table_args__ = (
        Index("idx_asset_project", "project_id"),
        Index("idx_asset_state", "ingest_state"),
    )


class Scene(EvidenceMixin, Base):
    __tablename__ = "scene"

    asset_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("asset.id"), nullable=False
    )
    scene_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_pts: Mapped[float] = mapped_column(Float, nullable=False)
    end_pts: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(Text)

    asset: Mapped["Asset"] = relationship("Asset", back_populates="scenes")
    shots: Mapped[List["Shot"]] = relationship("Shot", back_populates="scene")

    __table_args__ = (
        UniqueConstraint("asset_id", "scene_index"),
    )


class Shot(EvidenceMixin, Base):
    __tablename__ = "shot"

    asset_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("asset.id"), nullable=False
    )
    scene_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("scene.id")
    )
    shot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_pts: Mapped[float] = mapped_column(Float, nullable=False)
    end_pts: Mapped[float] = mapped_column(Float, nullable=False)
    duration_s: Mapped[Optional[float]] = mapped_column(
        Float, Computed("end_pts - start_pts", persisted=True)
    )
    shot_size_class: Mapped[Optional[str]] = mapped_column(Text)
    motion_score: Mapped[Optional[float]] = mapped_column(Float)
    mean_brightness: Mapped[Optional[float]] = mapped_column(Float)
    mean_contrast: Mapped[Optional[float]] = mapped_column(Float)
    palette_hex: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text))

    asset: Mapped["Asset"] = relationship("Asset", back_populates="shots")
    scene: Mapped[Optional["Scene"]] = relationship("Scene", back_populates="shots")
    keyframes: Mapped[List["Keyframe"]] = relationship("Keyframe", back_populates="shot")

    __table_args__ = (
        UniqueConstraint("asset_id", "shot_index"),
        Index("idx_shot_asset", "asset_id"),
        Index("idx_shot_scene", "scene_id"),
    )


class Keyframe(TimestampMixin, Base):
    __tablename__ = "keyframe"

    shot_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("shot.id"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("asset.id"), nullable=False
    )
    pts: Mapped[float] = mapped_column(Float, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    phash: Mapped[Optional[str]] = mapped_column(Text)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    shot: Mapped["Shot"] = relationship("Shot", back_populates="keyframes")
    ocr_spans: Mapped[List["OcrSpan"]] = relationship("OcrSpan", back_populates="keyframe")

    __table_args__ = (
        Index("idx_keyframe_shot", "shot_id"),
    )


class TranscriptTurn(EvidenceMixin, Base):
    __tablename__ = "transcript_turn"

    asset_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("asset.id"), nullable=False
    )
    shot_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("shot.id")
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_pts: Mapped[float] = mapped_column(Float, nullable=False)
    end_pts: Mapped[float] = mapped_column(Float, nullable=False)
    speaker_label: Mapped[Optional[str]] = mapped_column(Text)
    character_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]] = mapped_column(Float)

    asset: Mapped["Asset"] = relationship("Asset", back_populates="transcript_turns")
    sentences: Mapped[List["Sentence"]] = relationship("Sentence", back_populates="turn")

    __table_args__ = (
        Index("idx_transcript_turn_asset", "asset_id"),
    )


class Sentence(EvidenceMixin, Base):
    __tablename__ = "sentence"

    turn_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("transcript_turn.id"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("asset.id"), nullable=False
    )
    sentence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_pts: Mapped[float] = mapped_column(Float, nullable=False)
    end_pts: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    turn: Mapped["TranscriptTurn"] = relationship("TranscriptTurn", back_populates="sentences")

    __table_args__ = (
        Index("idx_sentence_turn", "turn_id"),
    )


class OcrSpan(EvidenceMixin, Base):
    __tablename__ = "ocr_span"

    keyframe_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("keyframe.id"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("asset.id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    bbox: Mapped[Optional[dict]] = mapped_column(JSONB)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    language: Mapped[Optional[str]] = mapped_column(Text)

    keyframe: Mapped["Keyframe"] = relationship("Keyframe", back_populates="ocr_spans")

    __table_args__ = (
        Index("idx_ocr_keyframe", "keyframe_id"),
    )


class SyntheticQuery(EvidenceMixin, Base):
    __tablename__ = "synthetic_query"

    parent_type: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    asset_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("asset.id"), nullable=False
    )
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    qdrant_point_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False))


class EmbeddingModelVersion(Base):
    __tablename__ = "embedding_model_version"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    surface: Mapped[str] = mapped_column(Text, nullable=False)
    qdrant_collection: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("model_name", "model_version", "surface"),
    )
