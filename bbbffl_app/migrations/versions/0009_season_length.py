"""Season-scoped BBBFFL regular-season length."""

import sqlalchemy as sa
from alembic import op

revision = "0009_season_length"
down_revision = "0008_round_map"
branch_labels = None
depends_on = None


def _sqlite_matchup_triggers():
    for operation in ("update", "delete", "insert"):
        op.execute(f"DROP TRIGGER IF EXISTS season_fixture_matchup_frozen_{operation}")
        old_check = (
            "(SELECT state FROM season_fixture_draw WHERE fixture_draw_id=OLD.fixture_draw_id)='frozen'"
            if operation != "insert"
            else "0"
        )
        new_check = (
            "(SELECT state FROM season_fixture_draw WHERE fixture_draw_id=NEW.fixture_draw_id)='frozen'"
            if operation != "delete"
            else "0"
        )
        op.execute(f"""
        CREATE TRIGGER season_fixture_matchup_frozen_{operation}
        BEFORE {operation.upper()} ON season_fixture_matchup
        WHEN {old_check} OR {new_check}
        BEGIN SELECT RAISE(ABORT, 'frozen fixture draw is immutable'); END
        """)


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite can add this constrained, constant-default column in place.
        # Avoiding a batch table copy is important because season is the root
        # of the already-populated identity and fixture foreign-key graph.
        op.execute(
            "ALTER TABLE bbbffl_season ADD COLUMN regular_season_round_count "
            "INTEGER NOT NULL DEFAULT 20 CHECK (regular_season_round_count >= 1)"
        )
        with op.batch_alter_table("season_fixture_matchup") as batch:
            batch.drop_constraint("ck_fixture_round_range", type_="check")
            batch.create_check_constraint("ck_fixture_round_positive", "bbbffl_round_number >= 1")
        _sqlite_matchup_triggers()
        op.execute("""
        CREATE TRIGGER season_length_frozen_update
        BEFORE UPDATE OF regular_season_round_count ON bbbffl_season
        WHEN OLD.regular_season_round_count <> NEW.regular_season_round_count
          AND EXISTS (
            SELECT 1 FROM season_fixture_draw
            WHERE season_id=OLD.season_id AND state='frozen'
          )
        BEGIN SELECT RAISE(ABORT, 'frozen fixture draw fixes season length'); END
        """)
    else:
        op.add_column(
            "bbbffl_season",
            sa.Column("regular_season_round_count", sa.Integer(), nullable=False, server_default="20"),
        )
        op.create_check_constraint("ck_season_regular_round_count", "bbbffl_season", "regular_season_round_count >= 1")
        op.drop_constraint("ck_fixture_round_range", "season_fixture_matchup", type_="check")
        op.create_check_constraint("ck_fixture_round_positive", "season_fixture_matchup", "bbbffl_round_number >= 1")
        op.execute("""
        CREATE FUNCTION enforce_frozen_fixture_season_length() RETURNS trigger AS $$
        BEGIN
          IF OLD.regular_season_round_count <> NEW.regular_season_round_count
             AND EXISTS (
               SELECT 1 FROM season_fixture_draw
               WHERE season_id=OLD.season_id AND state='frozen'
             )
          THEN
            RAISE EXCEPTION 'frozen fixture draw fixes season length';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """)
        op.execute("""
        CREATE TRIGGER season_length_frozen_update
        BEFORE UPDATE OF regular_season_round_count ON bbbffl_season
        FOR EACH ROW EXECUTE FUNCTION enforce_frozen_fixture_season_length()
        """)


def downgrade():
    bind = op.get_bind()
    non_default_lengths = bind.execute(
        sa.text("SELECT COUNT(*) FROM bbbffl_season WHERE regular_season_round_count <> 20")
    ).scalar_one()
    if non_default_lengths:
        raise RuntimeError("0009 downgrade refused: non-default season length configuration cannot be discarded")
    too_long = bind.execute(
        sa.text("SELECT COUNT(*) FROM season_fixture_matchup WHERE bbbffl_round_number > 20")
    ).scalar_one()
    if too_long:
        raise RuntimeError("0009 downgrade refused: fixtures beyond round 20 cannot be represented")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("season_fixture_matchup") as batch:
            batch.drop_constraint("ck_fixture_round_positive", type_="check")
            batch.create_check_constraint("ck_fixture_round_range", "bbbffl_round_number BETWEEN 1 AND 20")
        _sqlite_matchup_triggers()
    else:
        op.drop_constraint("ck_fixture_round_positive", "season_fixture_matchup", type_="check")
        op.create_check_constraint(
            "ck_fixture_round_range", "season_fixture_matchup", "bbbffl_round_number BETWEEN 1 AND 20"
        )
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER season_length_frozen_update")
        # Supported SQLite versions can drop this leaf column in place. This
        # preserves the season table identity and its populated FK graph.
        op.execute("ALTER TABLE bbbffl_season DROP COLUMN regular_season_round_count")
    else:
        op.execute("DROP FUNCTION enforce_frozen_fixture_season_length() CASCADE")
        op.drop_constraint("ck_season_regular_round_count", "bbbffl_season", type_="check")
        op.drop_column("bbbffl_season", "regular_season_round_count")
