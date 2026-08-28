"""Season player cache and history-preserving ownership ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0006_players"
down_revision = "0005_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "season_player_pool",
        sa.Column("season_player_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("canonical_player_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("afl_team_id", sa.Integer()),
        sa.Column("afl_team_name", sa.Text()),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_provider", sa.Text(), nullable=False),
        sa.Column("source_fetched_at", sa.Text(), nullable=False),
        sa.Column("source_updated_at", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("season_id", "canonical_player_id", name="uq_pool_season_canonical_player"),
        sa.UniqueConstraint("season_player_id", "season_id", name="uq_pool_id_season"),
        sa.CheckConstraint("canonical_player_id > 0", name="ck_pool_canonical_player_positive"),
        sa.CheckConstraint("length(trim(display_name)) > 0", name="ck_pool_display_name_nonempty"),
        sa.CheckConstraint("length(trim(source_provider)) > 0", name="ck_pool_provider_nonempty"),
    )
    op.create_index("ix_pool_season_selectable", "season_player_pool", ["season_id", "eligible"])
    op.create_table(
        "season_squad_configuration",
        sa.Column(
            "season_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("squad_limit", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("squad_limit > 0", name="ck_squad_limit_positive"),
    )
    op.create_table(
        "player_ownership_period",
        sa.Column("ownership_period_id", sa.Text(), primary_key=True),
        sa.Column("season_player_id", sa.Text(), nullable=False),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("acquired_at", sa.Text(), nullable=False),
        sa.Column("released_at", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["season_player_id", "season_id"],
            ["season_player_pool.season_player_id", "season_player_pool.season_id"],
            ondelete="RESTRICT",
            name="fk_ownership_player_same_season",
        ),
        sa.ForeignKeyConstraint(
            ["season_entry_id", "season_id"],
            ["season_entry.season_entry_id", "season_entry.season_id"],
            ondelete="RESTRICT",
            name="fk_ownership_entry_same_season",
        ),
        sa.CheckConstraint(
            "released_at IS NULL OR released_at > acquired_at",
            name="ck_ownership_interval",
        ),
        sa.UniqueConstraint("season_player_id", "acquired_at", name="uq_ownership_player_start"),
    )
    op.create_index(
        "ix_ownership_entry_history",
        "player_ownership_period",
        ["season_entry_id", "acquired_at"],
    )
    op.create_index(
        "ix_ownership_player_history",
        "player_ownership_period",
        ["season_player_id", "acquired_at"],
    )
    op.create_index(
        "uq_ownership_player_current",
        "player_ownership_period",
        ["season_player_id"],
        unique=True,
        sqlite_where=sa.text("released_at IS NULL"),
        postgresql_where=sa.text("released_at IS NULL"),
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("""
        CREATE TRIGGER ownership_no_overlap_insert BEFORE INSERT ON player_ownership_period
        BEGIN
          SELECT RAISE(ABORT, 'overlapping player ownership period')
          WHERE EXISTS (SELECT 1 FROM player_ownership_period p
            WHERE p.season_player_id = NEW.season_player_id
              AND (NEW.released_at IS NULL OR p.acquired_at < NEW.released_at)
              AND (p.released_at IS NULL OR NEW.acquired_at < p.released_at));
        END
        """)
        op.execute("""
        CREATE TRIGGER ownership_no_overlap_update BEFORE UPDATE OF acquired_at, released_at, season_player_id ON player_ownership_period
        BEGIN
          SELECT RAISE(ABORT, 'overlapping player ownership period')
          WHERE EXISTS (SELECT 1 FROM player_ownership_period p
            WHERE p.ownership_period_id <> NEW.ownership_period_id
              AND p.season_player_id = NEW.season_player_id
              AND (NEW.released_at IS NULL OR p.acquired_at < NEW.released_at)
              AND (p.released_at IS NULL OR NEW.acquired_at < p.released_at));
        END
        """)
    elif bind.dialect.name == "postgresql":
        op.execute("""
        CREATE FUNCTION enforce_ownership_no_overlap() RETURNS trigger AS $$
        BEGIN
          PERFORM 1 FROM season_player_pool WHERE season_player_id = NEW.season_player_id FOR UPDATE;
          IF EXISTS (SELECT 1 FROM player_ownership_period p
            WHERE p.ownership_period_id <> NEW.ownership_period_id
              AND p.season_player_id = NEW.season_player_id
              AND (NEW.released_at IS NULL OR p.acquired_at < NEW.released_at)
              AND (p.released_at IS NULL OR NEW.acquired_at < p.released_at)) THEN
            RAISE EXCEPTION 'overlapping player ownership period';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """)
        op.execute(
            "CREATE TRIGGER ownership_no_overlap BEFORE INSERT OR UPDATE OF acquired_at, released_at, season_player_id ON player_ownership_period FOR EACH ROW EXECUTE FUNCTION enforce_ownership_no_overlap()"
        )


def downgrade():
    bind = op.get_bind()
    counts = sum(
        bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in (
            "season_player_pool",
            "season_squad_configuration",
            "player_ownership_period",
        )
    )
    if counts:
        raise RuntimeError(
            "0006 downgrade refused: season player/ownership data cannot be represented by the prior schema"
        )
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION enforce_ownership_no_overlap() CASCADE")
    for table in (
        "player_ownership_period",
        "season_squad_configuration",
        "season_player_pool",
    ):
        op.drop_table(table)
