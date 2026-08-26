"""Season-aware BBBFFL parent identities.

Legacy scorer tables deliberately remain keyed by ``competition_key``.  They
are a compatibility boundary, not an implicit backfill into these identities.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_season"
down_revision = "0003_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bbbffl_season",
        sa.Column("season_id", sa.Text(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("year", name="uq_bbbffl_season_year"),
        sa.CheckConstraint("lifecycle_state IN ('setup', 'active', 'completed')", name="ck_bbbffl_season_lifecycle"),
        sa.CheckConstraint("version >= 1", name="ck_bbbffl_season_version"),
    )
    op.create_table(
        "season_rules_version",
        sa.Column("rules_version_id", sa.Text(), primary_key=True),
        sa.Column("season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rules_key", sa.Text(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text()),
        sa.UniqueConstraint("season_id", "rules_key", "version_number", name="uq_rules_season_key_version"),
        sa.UniqueConstraint("rules_version_id", "season_id", name="uq_rules_id_season"),
        sa.CheckConstraint("version_number >= 1", name="ck_rules_version_positive"),
    )
    op.create_index("ix_rules_season", "season_rules_version", ["season_id"])
    op.create_table(
        "competition_stream",
        sa.Column("competition_id", sa.Text(), primary_key=True),
        sa.Column("season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rules_version_id", sa.Text(), nullable=False),
        sa.Column("stream_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("stream_type", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("season_id", "stream_key", name="uq_competition_stream_season_key"),
        sa.ForeignKeyConstraint(["rules_version_id", "season_id"], ["season_rules_version.rules_version_id", "season_rules_version.season_id"], ondelete="RESTRICT", name="fk_competition_rules_same_season"),
        sa.CheckConstraint("stream_type IN ('ordinary', 'finals', 'superscore', 'replay', 'test')", name="ck_competition_stream_type"),
    )
    op.create_index("ix_competition_stream_season", "competition_stream", ["season_id"])
    op.create_table(
        "bbbffl_round",
        sa.Column("bbbffl_round_id", sa.Text(), primary_key=True),
        sa.Column("competition_id", sa.Text(), sa.ForeignKey("competition_stream.competition_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("round_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("competition_id", "round_key", name="uq_bbbffl_round_competition_key"),
        sa.UniqueConstraint("competition_id", "sequence", name="uq_bbbffl_round_competition_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_bbbffl_round_sequence"),
    )
    op.create_index("ix_bbbffl_round_competition", "bbbffl_round", ["competition_id"])
    op.create_table(
        "bbbffl_round_afl_reference",
        sa.Column("mapping_id", sa.Text(), primary_key=True),
        sa.Column("bbbffl_round_id", sa.Text(), sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default="afl-api-v1"),
        sa.Column("afl_season_id", sa.Integer(), nullable=False),
        sa.Column("afl_round_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("bbbffl_round_id", "provider", "afl_season_id", "afl_round_id", name="uq_round_afl_reference"),
    )
    op.create_index("ix_round_afl_lookup", "bbbffl_round_afl_reference", ["provider", "afl_season_id", "afl_round_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM bbbffl_season")).scalar_one():
        raise RuntimeError("0004 downgrade refused: season domain data cannot be represented by the prior schema")
    for table in ("bbbffl_round_afl_reference", "bbbffl_round", "competition_stream", "season_rules_version", "bbbffl_season"):
        op.drop_table(table)
