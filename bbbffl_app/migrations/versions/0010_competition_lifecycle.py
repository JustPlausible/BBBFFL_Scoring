"""Persist ordinary-round, matchup and versioned official-result lifecycle."""

from alembic import op
import sqlalchemy as sa

revision = "0010_lifecycle"
down_revision = "0009_season_length"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bbbffl_round_lifecycle",
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "competition_id",
            sa.Text(),
            sa.ForeignKey("competition_stream.competition_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "season_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("fixture_draw_id", sa.Text(), nullable=False),
        sa.Column("fixture_draw_version", sa.Integer(), nullable=False),
        sa.Column("fixture_round_number", sa.Integer(), nullable=False),
        sa.Column("mapping_id", sa.Text(), nullable=False),
        sa.Column("mapping_revision", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("afl_season_id", sa.Integer(), nullable=False),
        sa.Column("afl_round_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["fixture_draw_id", "season_id"],
            ["season_fixture_draw.fixture_draw_id", "season_fixture_draw.season_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_id", "mapping_revision"],
            [
                "round_afl_mapping_revision.mapping_id",
                "round_afl_mapping_revision.revision",
            ],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "bbbffl_round_id", "competition_id", name="uq_lifecycle_round_competition"
        ),
        sa.CheckConstraint(
            "state IN ('upcoming','open','live','review','final')",
            name="ck_round_lifecycle_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_round_lifecycle_version"),
    )
    op.create_table(
        "bbbffl_matchup",
        sa.Column("matchup_id", sa.Text(), primary_key=True),
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey(
                "bbbffl_round_lifecycle.bbbffl_round_id", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column(
            "fixture_matchup_id",
            sa.Text(),
            sa.ForeignKey(
                "season_fixture_matchup.fixture_matchup_id", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column("matchup_order", sa.Integer(), nullable=False),
        sa.Column("home_season_entry_id", sa.Text(), nullable=False),
        sa.Column("away_season_entry_id", sa.Text(), nullable=False),
        sa.Column("effective_official_version", sa.Integer()),
        sa.UniqueConstraint(
            "bbbffl_round_id", "fixture_matchup_id", name="uq_round_fixture_matchup"
        ),
        sa.UniqueConstraint(
            "bbbffl_round_id", "matchup_order", name="uq_round_matchup_order"
        ),
        sa.CheckConstraint(
            "matchup_order BETWEEN 1 AND 5", name="ck_lifecycle_matchup_order"
        ),
        sa.CheckConstraint(
            "home_season_entry_id <> away_season_entry_id",
            name="ck_lifecycle_distinct_entries",
        ),
    )
    op.create_table(
        "bbbffl_matchup_calculation",
        sa.Column(
            "matchup_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_matchup.matchup_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_calculation_revision"),
    )
    op.create_table(
        "bbbffl_official_result",
        sa.Column(
            "matchup_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_matchup.matchup_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("home_score", sa.Numeric(12, 3), nullable=False),
        sa.Column("away_score", sa.Numeric(12, 3), nullable=False),
        sa.Column("published_at", sa.Text(), nullable=False),
        sa.Column("published_by", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.PrimaryKeyConstraint("matchup_id", "version"),
        sa.CheckConstraint("version >= 1", name="ck_official_result_version"),
    )
    op.create_table(
        "bbbffl_round_upstream_fact",
        sa.Column("fact_id", sa.Text(), primary_key=True),
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey(
                "bbbffl_round_lifecycle.bbbffl_round_id", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column("provider_status", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text()),
        sa.Column("observed_at", sa.Text(), nullable=False),
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER official_result_no_update BEFORE UPDATE ON bbbffl_official_result BEGIN SELECT RAISE(ABORT, 'official results are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER official_result_no_delete BEFORE DELETE ON bbbffl_official_result BEGIN SELECT RAISE(ABORT, 'official results are immutable'); END"
        )
    elif bind.dialect.name == "postgresql":
        op.execute(
            """CREATE FUNCTION reject_official_result_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'official results are immutable'; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            "CREATE TRIGGER official_result_immutable BEFORE UPDATE OR DELETE ON bbbffl_official_result FOR EACH ROW EXECUTE FUNCTION reject_official_result_mutation()"
        )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM bbbffl_round_lifecycle"))
        .scalar_one()
    ):
        raise RuntimeError(
            "0010 downgrade refused: competition lifecycle history would be lost"
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_official_result_mutation() CASCADE")
    for table in (
        "bbbffl_round_upstream_fact",
        "bbbffl_official_result",
        "bbbffl_matchup_calculation",
        "bbbffl_matchup",
        "bbbffl_round_lifecycle",
    ):
        op.drop_table(table)
