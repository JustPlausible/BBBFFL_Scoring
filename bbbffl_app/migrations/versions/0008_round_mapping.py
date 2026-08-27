"""Versioned, fail-closed BBBFFL round to AFL context mappings."""

from alembic import op
import sqlalchemy as sa

revision = "0008_round_map"
down_revision = "0007_fixture"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "round_afl_mapping",
        sa.Column("mapping_id", sa.Text(), primary_key=True),
        sa.Column("bbbffl_round_id", sa.Text(), sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("bbbffl_round_id", name="uq_round_afl_mapping_round"),
        sa.CheckConstraint("current_revision >= 1", name="ck_mapping_revision_positive"),
    )
    op.create_table(
        "round_afl_mapping_revision",
        sa.Column("mapping_id", sa.Text(), sa.ForeignKey("round_afl_mapping.mapping_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default="afl-api-v1"),
        sa.Column("afl_season_id", sa.Integer()),
        sa.Column("afl_round_id", sa.Integer()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.PrimaryKeyConstraint("mapping_id", "revision"),
        sa.CheckConstraint("state IN ('unresolved', 'ambiguous', 'accepted')", name="ck_mapping_revision_state"),
        sa.CheckConstraint("revision >= 1", name="ck_mapping_history_revision_positive"),
        sa.CheckConstraint("(state = 'accepted' AND afl_season_id IS NOT NULL AND afl_round_id IS NOT NULL) OR state <> 'accepted'", name="ck_accepted_mapping_has_context"),
    )
    op.create_index("ix_mapping_afl_context", "round_afl_mapping_revision", ["provider", "afl_season_id", "afl_round_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM round_afl_mapping")).scalar_one():
        raise RuntimeError("0008 downgrade refused: mapping history cannot be represented by the prior schema")
    op.drop_table("round_afl_mapping_revision")
    op.drop_table("round_afl_mapping")
