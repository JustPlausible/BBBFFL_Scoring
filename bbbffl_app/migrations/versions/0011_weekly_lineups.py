"""Persist private weekly drafts and immutable submitted lineup versions."""

import sqlalchemy as sa
from alembic import op

revision = "0011_lineups"
down_revision = "0010_lifecycle"
branch_labels = None
depends_on = None

POSITIONS = "'F1','F2','F3','M1','M2','M3','Ruck','Tackler','Interchange'"


def upgrade():
    op.create_table(
        "weekly_lineup",
        sa.Column("lineup_id", sa.Text(), primary_key=True),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("competition_id", sa.Text(), nullable=False),
        sa.Column("bbbffl_round_id", sa.Text(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("effective_submission_version", sa.Integer()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["season_id"], ["bbbffl_season.season_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["competition_id"], ["competition_stream.competition_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["bbbffl_round_id"], ["bbbffl_round.bbbffl_round_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["season_entry_id", "season_id"],
            ["season_entry.season_entry_id", "season_entry.season_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "season_id", "competition_id", "bbbffl_round_id", "season_entry_id", name="uq_weekly_lineup_scope"
        ),
        sa.UniqueConstraint("lineup_id", "season_id", name="uq_weekly_lineup_id_season"),
        sa.CheckConstraint("draft_revision >= 1", name="ck_lineup_draft_revision"),
        sa.CheckConstraint(
            "effective_submission_version IS NULL OR effective_submission_version >= 1",
            name="ck_lineup_effective_version",
        ),
    )
    op.create_table(
        "weekly_lineup_draft_slot",
        sa.Column("lineup_id", sa.Text(), sa.ForeignKey("weekly_lineup.lineup_id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column(
            "season_player_id", sa.Text(), sa.ForeignKey("season_player_pool.season_player_id", ondelete="RESTRICT")
        ),
        sa.PrimaryKeyConstraint("lineup_id", "position"),
        sa.CheckConstraint(f"position IN ({POSITIONS})", name="ck_draft_slot_position"),
        sa.UniqueConstraint("lineup_id", "season_player_id", name="uq_draft_player_once"),
    )
    op.create_table(
        "weekly_lineup_submission",
        sa.Column("lineup_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("based_on_draft_revision", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("actor_role", sa.Text()),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_detail", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.ForeignKeyConstraint(["lineup_id"], ["weekly_lineup.lineup_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("lineup_id", "version"),
        sa.CheckConstraint("version >= 1", name="ck_submission_version"),
        sa.CheckConstraint("based_on_draft_revision >= 1", name="ck_submission_draft_revision"),
        sa.CheckConstraint(
            "source_type IN ('coach','scorer_proxy','carry_forward','system_derived')", name="ck_submission_source"
        ),
    )
    op.create_table(
        "weekly_lineup_submission_slot",
        sa.Column("lineup_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column(
            "season_player_id", sa.Text(), sa.ForeignKey("season_player_pool.season_player_id", ondelete="RESTRICT")
        ),
        sa.ForeignKeyConstraint(
            ["lineup_id", "version"],
            ["weekly_lineup_submission.lineup_id", "weekly_lineup_submission.version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("lineup_id", "version", "position"),
        sa.CheckConstraint(f"position IN ({POSITIONS})", name="ck_submission_slot_position"),
        sa.UniqueConstraint("lineup_id", "version", "season_player_id", name="uq_submission_player_once"),
    )
    # The player FK is deliberately indirect through the repository's season
    # validation: submitted rows retain the stable player identity after ownership ends.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in ("weekly_lineup_submission", "weekly_lineup_submission_slot"):
            op.execute(
                f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
            )
    elif bind.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_submitted_lineup_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'submitted lineups are immutable'; END; $$ LANGUAGE plpgsql"
        )
        for table in ("weekly_lineup_submission", "weekly_lineup_submission_slot"):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_submitted_lineup_mutation()"
            )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM weekly_lineup")).scalar_one():
        raise RuntimeError("0011 downgrade refused: weekly lineup history would be lost")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_submitted_lineup_mutation() CASCADE")
    for table in (
        "weekly_lineup_submission_slot",
        "weekly_lineup_submission",
        "weekly_lineup_draft_slot",
        "weekly_lineup",
    ):
        op.drop_table(table)
