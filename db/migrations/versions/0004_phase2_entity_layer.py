"""Phase 2: character, face_track, object_track tables; FK on transcript_turn.character_id

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── character ──────────────────────────────────────────────────────────
    op.create_table(
        "character",
        sa.Column("id", postgresql.UUID(as_uuid=False),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("cluster_label", sa.Text(), nullable=False),
        sa.Column("reference_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("confirmed", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_character_project", "character", ["project_id"])

    # ── face_track ─────────────────────────────────────────────────────────
    op.create_table(
        "face_track",
        sa.Column("id", postgresql.UUID(as_uuid=False),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("producer_model", sa.Text(), nullable=False),
        sa.Column("producer_version", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("shot_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("track_index", sa.Integer(), nullable=False),
        sa.Column("start_pts", sa.Float(), nullable=False),
        sa.Column("end_pts", sa.Float(), nullable=False),
        sa.Column("bbox_series", postgresql.JSONB(), nullable=True),
        # 512-dim embedding stored as float array (no pgvector required)
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("character_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("qdrant_point_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.ForeignKeyConstraint(["shot_id"], ["shot.id"]),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.id"]),
        sa.ForeignKeyConstraint(["character_id"], ["character.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_face_track_shot", "face_track", ["shot_id"])
    op.create_index("idx_face_track_asset", "face_track", ["asset_id"])
    op.create_index("idx_face_track_character", "face_track", ["character_id"])
    op.create_index("idx_face_track_qdrant", "face_track", ["qdrant_point_id"])

    # ── object_track ───────────────────────────────────────────────────────
    op.create_table(
        "object_track",
        sa.Column("id", postgresql.UUID(as_uuid=False),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("producer_model", sa.Text(), nullable=False),
        sa.Column("producer_version", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("shot_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("track_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("start_pts", sa.Float(), nullable=False),
        sa.Column("end_pts", sa.Float(), nullable=False),
        sa.Column("bbox_series", postgresql.JSONB(), nullable=True),
        sa.Column("crop_path", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["shot_id"], ["shot.id"]),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_object_track_shot", "object_track", ["shot_id"])
    op.create_index("idx_object_track_asset", "object_track", ["asset_id"])
    op.create_index("idx_object_track_label", "object_track", ["label"])

    # ── transcript_turn.character_id FK (column already exists, add constraint) ──
    op.create_foreign_key(
        "fk_transcript_turn_character",
        "transcript_turn", "character",
        ["character_id"], ["id"],
    )

    # ── asset: add enrich_done flag ────────────────────────────────────────
    op.add_column("asset", sa.Column(
        "enrich_done", sa.Boolean(), nullable=False, server_default=sa.text("false")
    ))


def downgrade() -> None:
    op.drop_column("asset", "enrich_done")
    op.drop_constraint("fk_transcript_turn_character", "transcript_turn", type_="foreignkey")
    op.drop_table("object_track")
    op.drop_table("face_track")
    op.drop_table("character")
