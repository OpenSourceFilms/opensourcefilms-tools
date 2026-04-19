"""add qdrant_point_ids, asr_done/ocr_done, embed state

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Asset: parallel completion flags
    op.add_column("asset", sa.Column("asr_done", sa.Boolean(), nullable=False,
                                     server_default=sa.text("false")))
    op.add_column("asset", sa.Column("ocr_done", sa.Boolean(), nullable=False,
                                     server_default=sa.text("false")))

    # Qdrant point IDs
    op.add_column("keyframe", sa.Column("qdrant_point_id",
                                        postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("sentence", sa.Column("qdrant_point_id",
                                        postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("transcript_turn", sa.Column("qdrant_point_id",
                                               postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("ocr_span", sa.Column("qdrant_point_id",
                                        postgresql.UUID(as_uuid=False), nullable=True))

    # Indexes for fast lookup by qdrant_point_id
    op.create_index("idx_keyframe_qdrant_point", "keyframe", ["qdrant_point_id"])
    op.create_index("idx_sentence_qdrant_point", "sentence", ["qdrant_point_id"])
    op.create_index("idx_turn_qdrant_point", "transcript_turn", ["qdrant_point_id"])
    op.create_index("idx_ocr_qdrant_point", "ocr_span", ["qdrant_point_id"])


def downgrade() -> None:
    op.drop_index("idx_keyframe_qdrant_point", "keyframe")
    op.drop_index("idx_sentence_qdrant_point", "sentence")
    op.drop_index("idx_turn_qdrant_point", "transcript_turn")
    op.drop_index("idx_ocr_qdrant_point", "ocr_span")

    op.drop_column("keyframe", "qdrant_point_id")
    op.drop_column("sentence", "qdrant_point_id")
    op.drop_column("transcript_turn", "qdrant_point_id")
    op.drop_column("ocr_span", "qdrant_point_id")
    op.drop_column("asset", "asr_done")
    op.drop_column("asset", "ocr_done")
