"""Durable private coach and season-entry/public-team identities."""

import sqlalchemy as sa
from alembic import op

revision = "0005_identity"
down_revision = "0004_season"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "coach",
        sa.Column("coach_id", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("profile_notes", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "season_entry",
        sa.Column("season_entry_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("licence_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("season_id", "licence_key", name="uq_season_entry_licence"),
        sa.UniqueConstraint("season_entry_id", "season_id", name="uq_entry_id_season"),
    )
    op.create_index("ix_season_entry_season", "season_entry", ["season_id"])
    op.create_table(
        "season_entry_coach_history",
        sa.Column("assignment_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_entry_id",
            sa.Text(),
            sa.ForeignKey("season_entry.season_entry_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("coach_id", sa.Text(), sa.ForeignKey("coach.coach_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("ended_at", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_entry_assignment_interval"),
        sa.UniqueConstraint("season_entry_id", "started_at", name="uq_entry_assignment_start"),
    )
    op.create_index("ix_entry_coach", "season_entry_coach_history", ["coach_id"])
    op.create_index(
        "uq_entry_current_coach",
        "season_entry_coach_history",
        ["season_entry_id"],
        unique=True,
        sqlite_where=sa.text("ended_at IS NULL"),
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "season_entry_team_name_history",
        sa.Column("team_name_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_entry_id",
            sa.Text(),
            sa.ForeignKey("season_entry.season_entry_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("team_name", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("ended_at", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.CheckConstraint("length(trim(team_name)) > 0", name="ck_team_name_nonempty"),
        sa.CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_team_name_interval"),
        sa.UniqueConstraint("season_entry_id", "started_at", name="uq_team_name_start"),
    )
    op.create_index(
        "uq_entry_current_team_name",
        "season_entry_team_name_history",
        ["season_entry_id"],
        unique=True,
        sqlite_where=sa.text("ended_at IS NULL"),
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade():
    bind = op.get_bind()
    counts = sum(
        bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() for table in ("coach", "season_entry")
    )
    if counts:
        raise RuntimeError("0005 downgrade refused: coach/season-entry data cannot be represented by the prior schema")
    for table in ("season_entry_team_name_history", "season_entry_coach_history", "season_entry", "coach"):
        op.drop_table(table)
