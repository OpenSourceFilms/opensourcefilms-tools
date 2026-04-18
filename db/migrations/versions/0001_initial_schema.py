"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── extensions ────────────────────────────────────────────
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')

    # ── project ───────────────────────────────────────────────
    op.create_table(
        "project",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
    )

    # ── asset ─────────────────────────────────────────────────
    op.create_table(
        "asset",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("project_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("project.id"), nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("original_path", sa.Text, nullable=False),
        sa.Column("proxy_path", sa.Text),
        sa.Column("sha256", sa.Text, nullable=False, unique=True),
        sa.Column("duration_s", sa.Float),
        sa.Column("fps", sa.Float),
        sa.Column("width", sa.Integer),
        sa.Column("height", sa.Integer),
        sa.Column("codec", sa.Text),
        sa.Column("container", sa.Text),
        sa.Column("ingest_state", sa.Text, nullable=False, server_default="pending"),
        sa.Column("ingest_error", sa.Text),
        sa.Column("shot_count", sa.Integer),
    )
    op.create_index("idx_asset_project", "asset", ["project_id"])
    op.create_index("idx_asset_state", "asset", ["ingest_state"])

    # ── scene ─────────────────────────────────────────────────
    op.create_table(
        "scene",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("producer_model", sa.Text, nullable=False),
        sa.Column("producer_version", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=False)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("asset_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("asset.id"), nullable=False),
        sa.Column("scene_index", sa.Integer, nullable=False),
        sa.Column("start_pts", sa.Float, nullable=False),
        sa.Column("end_pts", sa.Float, nullable=False),
        sa.Column("label", sa.Text),
        sa.UniqueConstraint("asset_id", "scene_index"),
    )

    # ── shot ──────────────────────────────────────────────────
    op.create_table(
        "shot",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("producer_model", sa.Text, nullable=False),
        sa.Column("producer_version", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=False)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("asset_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("asset.id"), nullable=False),
        sa.Column("scene_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("scene.id")),
        sa.Column("shot_index", sa.Integer, nullable=False),
        sa.Column("start_pts", sa.Float, nullable=False),
        sa.Column("end_pts", sa.Float, nullable=False),
        sa.Column("duration_s", sa.Float,
                  sa.Computed("end_pts - start_pts", persisted=True)),
        sa.Column("shot_size_class", sa.Text),
        sa.Column("motion_score", sa.Float),
        sa.Column("mean_brightness", sa.Float),
        sa.Column("mean_contrast", sa.Float),
        sa.Column("palette_hex", postgresql.ARRAY(sa.Text)),
        sa.UniqueConstraint("asset_id", "shot_index"),
    )
    op.create_index("idx_shot_asset", "shot", ["asset_id"])
    op.create_index("idx_shot_scene", "shot", ["scene_id"])

    # ── keyframe ──────────────────────────────────────────────
    op.create_table(
        "keyframe",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("shot_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("shot.id"), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("asset.id"), nullable=False),
        sa.Column("pts", sa.Float, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("phash", sa.Text),
    )
    op.create_index("idx_keyframe_shot", "keyframe", ["shot_id"])

    # ── transcript_turn ───────────────────────────────────────
    op.create_table(
        "transcript_turn",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("producer_model", sa.Text, nullable=False),
        sa.Column("producer_version", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=False)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("asset_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("asset.id"), nullable=False),
        sa.Column("shot_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("shot.id")),
        sa.Column("turn_index", sa.Integer, nullable=False),
        sa.Column("start_pts", sa.Float, nullable=False),
        sa.Column("end_pts", sa.Float, nullable=False),
        sa.Column("speaker_label", sa.Text),
        sa.Column("character_id", postgresql.UUID(as_uuid=False)),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("language", sa.Text),
        sa.Column("confidence", sa.Float),
    )
    op.create_index("idx_transcript_turn_asset", "transcript_turn", ["asset_id"])
    op.execute(
        "CREATE INDEX idx_turn_text ON transcript_turn "
        "USING gin(to_tsvector('english', text))"
    )

    # ── sentence ──────────────────────────────────────────────
    op.create_table(
        "sentence",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("producer_model", sa.Text, nullable=False),
        sa.Column("producer_version", sa.Text, nullable=False),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=False)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("turn_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("transcript_turn.id"), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("asset.id"), nullable=False),
        sa.Column("sentence_index", sa.Integer, nullable=False),
        sa.Column("start_pts", sa.Float, nullable=False),
        sa.Column("end_pts", sa.Float, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
    )
    op.create_index("idx_sentence_turn", "sentence", ["turn_id"])
    op.execute(
        "CREATE INDEX idx_sentence_text ON sentence "
        "USING gin(to_tsvector('english', text))"
    )

    # ── ocr_span ──────────────────────────────────────────────
    op.create_table(
        "ocr_span",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("producer_model", sa.Text, nullable=False),
        sa.Column("producer_version", sa.Text, nullable=False),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=False)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("keyframe_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("keyframe.id"), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("asset.id"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("bbox", postgresql.JSONB),
        sa.Column("confidence", sa.Float),
        sa.Column("language", sa.Text),
    )
    op.create_index("idx_ocr_keyframe", "ocr_span", ["keyframe_id"])
    op.execute(
        "CREATE INDEX idx_ocr_text ON ocr_span "
        "USING gin(to_tsvector('english', text))"
    )

    # ── synthetic_query ───────────────────────────────────────
    op.create_table(
        "synthetic_query",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("producer_model", sa.Text, nullable=False),
        sa.Column("producer_version", sa.Text, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("parent_type", sa.Text, nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("asset.id"), nullable=False),
        sa.Column("query_text", sa.Text, nullable=False),
        sa.Column("qdrant_point_id", postgresql.UUID(as_uuid=False)),
    )

    # ── embedding_model_version ───────────────────────────────
    op.create_table(
        "embedding_model_version",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_name", sa.Text, nullable=False),
        sa.Column("model_version", sa.Text, nullable=False),
        sa.Column("surface", sa.Text, nullable=False),
        sa.Column("qdrant_collection", sa.Text, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("model_name", "model_version", "surface"),
    )

    # ── seed embedding_model_version ──────────────────────────
    op.execute("""
        INSERT INTO embedding_model_version
            (model_name, model_version, surface, qdrant_collection, active)
        VALUES
            ('Qwen3-VL-Embedding', '1.0', 'visual_keyframe',
             'visual_keyframe_qwen3vl', true),
            ('Qwen3-Embedding', '1.0', 'text_sentence',
             'text_sentence_qwen3', true),
            ('Qwen3-Embedding', '1.0', 'ocr_span',
             'ocr_span_qwen3', true)
    """)


def downgrade() -> None:
    for tbl in [
        "synthetic_query", "ocr_span", "sentence", "transcript_turn",
        "keyframe", "shot", "scene", "asset", "project",
        "embedding_model_version",
    ]:
        op.drop_table(tbl)
