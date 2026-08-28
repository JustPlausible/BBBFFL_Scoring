"""Preseason transaction/finalisation window, audited trades and the frozen
opening-ownership boundary (roadmap package 15, issue #54).

Builds on 0015/0016's draft ledger and 0006's ownership ledger without
rewriting either: a season's preseason window opens only once its draft is
finalized, every trade leg is still validated and applied through the
existing `player_ownership_period` ledger (this migration adds no parallel
ownership table), and the opening snapshot freezes *references* into that
ledger (`ownership_period_id`) rather than copying player/ownership facts.

Three small table groups:

- `season_preseason_window` -- one row per season (like `season_draft`),
  `closed_at IS NULL` while open.
- `preseason_trade` / `preseason_trade_leg` -- one atomic trade with one or
  more player legs; each leg records both the released and the newly
  acquired `player_ownership_period` row it produced, so a trade's
  provenance is always traceable into the authoritative ledger.
- `preseason_opening_snapshot` / `preseason_opening_snapshot_entry` -- a
  versioned, append-only freeze of "who owned which player when the season
  opened". Version 1 is written atomically by closing the window; a later
  authorised correction (app.preseason.PreseasonRepository.correct_opening_snapshot)
  appends version 2, 3, ... and never rewrites an earlier version's rows.

Every one of these six tables gets the same immutability triggers 0015/0016
already established for draft history: ordinary UPDATE/DELETE is rejected at
the database level, so a correction must always take the shape of a new,
attributable row/version rather than a silent edit.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_preseason"
down_revision = "0016_draft_ops"
branch_labels = None
depends_on = None

_APPEND_ONLY_TABLES = (
    "preseason_trade",
    "preseason_trade_leg",
    "preseason_opening_snapshot",
    "preseason_opening_snapshot_entry",
)


def upgrade():
    op.create_table(
        "season_preseason_window",
        sa.Column("window_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("draft_id", sa.Text(), sa.ForeignKey("season_draft.draft_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("opened_at", sa.Text(), nullable=False),
        sa.Column("closed_at", sa.Text()),
        sa.Column("closed_note", sa.Text()),
        sa.UniqueConstraint("season_id", name="uq_preseason_window_season"),
    )

    op.create_table(
        "preseason_trade",
        sa.Column("trade_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "window_id",
            sa.Text(),
            sa.ForeignKey("season_preseason_window.window_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column(
            "audit_event_id", sa.Text(), sa.ForeignKey("audit_event.event_id", ondelete="RESTRICT"), nullable=False
        ),
    )
    op.create_index("ix_preseason_trade_season", "preseason_trade", ["season_id", "applied_at"])

    op.create_table(
        "preseason_trade_leg",
        sa.Column("leg_id", sa.Text(), primary_key=True),
        sa.Column(
            "trade_id", sa.Text(), sa.ForeignKey("preseason_trade.trade_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("season_player_id", sa.Text(), nullable=False),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("from_season_entry_id", sa.Text(), nullable=False),
        sa.Column("to_season_entry_id", sa.Text(), nullable=False),
        sa.Column(
            "released_ownership_period_id",
            sa.Text(),
            sa.ForeignKey("player_ownership_period.ownership_period_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "acquired_ownership_period_id",
            sa.Text(),
            sa.ForeignKey("player_ownership_period.ownership_period_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["season_player_id", "season_id"],
            ["season_player_pool.season_player_id", "season_player_pool.season_id"],
            ondelete="RESTRICT",
            name="fk_trade_leg_player_same_season",
        ),
        sa.ForeignKeyConstraint(
            ["from_season_entry_id", "season_id"],
            ["season_entry.season_entry_id", "season_entry.season_id"],
            ondelete="RESTRICT",
            name="fk_trade_leg_from_entry_same_season",
        ),
        sa.ForeignKeyConstraint(
            ["to_season_entry_id", "season_id"],
            ["season_entry.season_entry_id", "season_entry.season_id"],
            ondelete="RESTRICT",
            name="fk_trade_leg_to_entry_same_season",
        ),
        sa.CheckConstraint("from_season_entry_id <> to_season_entry_id", name="ck_trade_leg_changes_owner"),
    )
    op.create_index("ix_preseason_trade_leg_trade", "preseason_trade_leg", ["trade_id"])

    op.create_table(
        "preseason_opening_snapshot",
        sa.Column("snapshot_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "window_id",
            sa.Text(),
            sa.ForeignKey("season_preseason_window.window_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "supersedes_snapshot_id",
            sa.Text(),
            sa.ForeignKey("preseason_opening_snapshot.snapshot_id", ondelete="RESTRICT"),
        ),
        sa.UniqueConstraint("season_id", "version", name="uq_opening_snapshot_season_version"),
        sa.CheckConstraint("version > 0", name="ck_opening_snapshot_version_positive"),
    )

    op.create_table(
        "preseason_opening_snapshot_entry",
        sa.Column("entry_row_id", sa.Text(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Text(),
            sa.ForeignKey("preseason_opening_snapshot.snapshot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("season_player_id", sa.Text(), nullable=False),
        sa.Column(
            "ownership_period_id",
            sa.Text(),
            sa.ForeignKey("player_ownership_period.ownership_period_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["season_entry_id", "season_id"],
            ["season_entry.season_entry_id", "season_entry.season_id"],
            ondelete="RESTRICT",
            name="fk_opening_snapshot_entry_same_season",
        ),
        sa.ForeignKeyConstraint(
            ["season_player_id", "season_id"],
            ["season_player_pool.season_player_id", "season_player_pool.season_id"],
            ondelete="RESTRICT",
            name="fk_opening_snapshot_player_same_season",
        ),
        # One row per player per snapshot *version* -- the same invariant
        # the live ownership ledger enforces (0006's partial unique index),
        # frozen at a point in time: no version of a snapshot can claim a
        # player is owned by two entries at once.
        sa.UniqueConstraint("snapshot_id", "season_player_id", name="uq_opening_snapshot_player_once"),
    )
    op.create_index(
        "ix_opening_snapshot_entry_entry", "preseason_opening_snapshot_entry", ["season_entry_id", "snapshot_id"]
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in _APPEND_ONLY_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} history is immutable'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} history is immutable'); END"
            )
    else:
        op.execute("""
        CREATE FUNCTION reject_immutable_preseason_change() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'preseason trade/opening-squad history is immutable'; END; $$ LANGUAGE plpgsql
        """)
        for table in _APPEND_ONLY_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_immutable_preseason_change()"
            )


def downgrade():
    bind = op.get_bind()
    for table in ("preseason_opening_snapshot", "preseason_trade", "season_preseason_window"):
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError(f"0017 downgrade refused: {table} history would be lost")

    if bind.dialect.name == "sqlite":
        for table in _APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_update")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_delete")
    else:
        for table in _APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.execute("DROP FUNCTION reject_immutable_preseason_change() CASCADE")

    for table in (
        "preseason_opening_snapshot_entry",
        "preseason_opening_snapshot",
        "preseason_trade_leg",
        "preseason_trade",
        "season_preseason_window",
    ):
        op.drop_table(table)
