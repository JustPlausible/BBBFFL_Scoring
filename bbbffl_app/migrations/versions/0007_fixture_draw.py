"""Season fixture-number draw and persisted historical BBBFFL rotation."""

from alembic import op
import sqlalchemy as sa

revision = "0007_fixture"
down_revision = "0006_players"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "season_fixture_draw",
        sa.Column("fixture_draw_id", sa.Text(), primary_key=True),
        sa.Column("season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rotation_version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("frozen_at", sa.Text()),
        sa.UniqueConstraint("season_id", name="uq_fixture_draw_season"),
        sa.UniqueConstraint("fixture_draw_id", "season_id", name="uq_fixture_draw_id_season"),
        sa.CheckConstraint("state IN ('draft', 'frozen')", name="ck_fixture_draw_state"),
        sa.CheckConstraint("version >= 1", name="ck_fixture_draw_version"),
        sa.CheckConstraint("(state = 'draft' AND frozen_at IS NULL) OR (state = 'frozen' AND frozen_at IS NOT NULL)", name="ck_fixture_draw_frozen_at"),
    )
    op.create_table(
        "season_fixture_number",
        sa.Column("fixture_draw_id", sa.Text(), nullable=False),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("fixture_number", sa.Integer(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["fixture_draw_id", "season_id"], ["season_fixture_draw.fixture_draw_id", "season_fixture_draw.season_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["season_entry_id", "season_id"], ["season_entry.season_entry_id", "season_entry.season_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("fixture_draw_id", "fixture_number"),
        sa.UniqueConstraint("fixture_draw_id", "season_entry_id", name="uq_fixture_draw_entry"),
        sa.CheckConstraint("fixture_number BETWEEN 1 AND 10", name="ck_fixture_number_range"),
    )
    op.create_table(
        "season_fixture_matchup",
        sa.Column("fixture_matchup_id", sa.Text(), primary_key=True),
        sa.Column("fixture_draw_id", sa.Text(), nullable=False),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("bbbffl_round_number", sa.Integer(), nullable=False),
        sa.Column("matchup_order", sa.Integer(), nullable=False),
        sa.Column("home_season_entry_id", sa.Text(), nullable=False),
        sa.Column("away_season_entry_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["fixture_draw_id", "season_id"], ["season_fixture_draw.fixture_draw_id", "season_fixture_draw.season_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["home_season_entry_id", "season_id"], ["season_entry.season_entry_id", "season_entry.season_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["away_season_entry_id", "season_id"], ["season_entry.season_entry_id", "season_entry.season_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("fixture_draw_id", "bbbffl_round_number", "matchup_order", name="uq_fixture_matchup_slot"),
        sa.CheckConstraint("bbbffl_round_number BETWEEN 1 AND 20", name="ck_fixture_round_range"),
        sa.CheckConstraint("matchup_order BETWEEN 1 AND 5", name="ck_fixture_matchup_order"),
        sa.CheckConstraint("home_season_entry_id <> away_season_entry_id", name="ck_fixture_distinct_entries"),
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in ("season_fixture_number", "season_fixture_matchup"):
            op.execute(f"""
            CREATE TRIGGER {table}_frozen_update BEFORE UPDATE ON {table}
            WHEN (SELECT state FROM season_fixture_draw WHERE fixture_draw_id=OLD.fixture_draw_id)='frozen'
              OR (SELECT state FROM season_fixture_draw WHERE fixture_draw_id=NEW.fixture_draw_id)='frozen'
            BEGIN SELECT RAISE(ABORT, 'frozen fixture draw is immutable'); END
            """)
            op.execute(f"""
            CREATE TRIGGER {table}_frozen_delete BEFORE DELETE ON {table}
            WHEN (SELECT state FROM season_fixture_draw WHERE fixture_draw_id=OLD.fixture_draw_id)='frozen'
            BEGIN SELECT RAISE(ABORT, 'frozen fixture draw is immutable'); END
            """)
            op.execute(f"""
            CREATE TRIGGER {table}_frozen_insert BEFORE INSERT ON {table}
            WHEN (SELECT state FROM season_fixture_draw WHERE fixture_draw_id=NEW.fixture_draw_id)='frozen'
            BEGIN SELECT RAISE(ABORT, 'frozen fixture draw is immutable'); END
            """)
        op.execute("""
        CREATE TRIGGER fixture_draw_no_unfreeze BEFORE UPDATE ON season_fixture_draw
        WHEN OLD.state='frozen'
        BEGIN SELECT RAISE(ABORT, 'frozen fixture draw is immutable'); END
        """)
    elif bind.dialect.name == "postgresql":
        op.execute("""
        CREATE FUNCTION enforce_fixture_draw_mutability() RETURNS trigger AS $$
        DECLARE old_draw_state text;
        DECLARE new_draw_state text;
        BEGIN
          IF TG_OP IN ('UPDATE', 'DELETE') THEN
            SELECT state INTO old_draw_state FROM season_fixture_draw WHERE fixture_draw_id=OLD.fixture_draw_id FOR UPDATE;
          END IF;
          IF TG_OP IN ('INSERT', 'UPDATE') THEN
            SELECT state INTO new_draw_state FROM season_fixture_draw WHERE fixture_draw_id=NEW.fixture_draw_id FOR UPDATE;
          END IF;
          IF old_draw_state='frozen' OR new_draw_state='frozen' THEN
            RAISE EXCEPTION 'frozen fixture draw is immutable';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END; $$ LANGUAGE plpgsql
        """)
        for table in ("season_fixture_number", "season_fixture_matchup"):
            op.execute(f"CREATE TRIGGER {table}_mutable BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION enforce_fixture_draw_mutability()")
        op.execute("""
        CREATE FUNCTION enforce_fixture_draw_state() RETURNS trigger AS $$
        BEGIN
          IF OLD.state='frozen' THEN RAISE EXCEPTION 'frozen fixture draw is immutable'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """)
        op.execute("CREATE TRIGGER fixture_draw_no_unfreeze BEFORE UPDATE ON season_fixture_draw FOR EACH ROW EXECUTE FUNCTION enforce_fixture_draw_state()")


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM season_fixture_draw")).scalar_one():
        raise RuntimeError("0007 downgrade refused: fixture history cannot be represented by the prior schema")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION enforce_fixture_draw_mutability() CASCADE")
        op.execute("DROP FUNCTION enforce_fixture_draw_state() CASCADE")
    for table in ("season_fixture_matchup", "season_fixture_number", "season_fixture_draw"):
        op.drop_table(table)
