"""Mid-season draft lifecycle: season trigger-round configuration, a
generalised (multi-kind) draft ledger, a frozen ladder-order snapshot,
delistings and player/pick trades (issue #164).

Builds on, rather than duplicating:

- the existing preseason draft ledger (`0015_draft_ledger.py`/
  `0016_draft_operations.py`) -- `season_draft` gains a `draft_kind`
  discriminator (`'preseason'` default, `'midseason'` new) so a season can
  have at most one draft of each kind (`uq_draft_season_kind` replaces the
  old one-draft-per-season `uq_draft_season`). `draft_order_position`,
  `draft_pick`, `draft_pick_transfer` and `draft_pick_correction` need no
  schema change: they key off `draft_id`, which stays globally unique.
- the ladder read model (`app.ladder`) -- never persisted or made
  authoritative by this migration. `midseason_ladder_snapshot(_row)`
  freezes an independent, immutable *copy* of one `LadderSnapshot` at the
  moment the Scorer confirms the mid-season draft-order basis; the live
  ladder calculation is completely untouched and remains the sole
  authority for competition standings.
- the audit boundary (`0003_audit_event.py`) for every lifecycle
  transition, delisting and trade decision.

## New tables

- `midseason_draft` -- one row per season (like `season_draft`/
  `season_preseason_window`), holding the explicit lifecycle state
  (`ladder_confirmed -> delisting_open -> delistings_locked -> draft_open
  -> draft_complete -> complete`).
- `midseason_ladder_snapshot(_row)` -- the frozen ladder-order basis,
  append-only (immutable triggers, matching `preseason_opening_snapshot`'s
  treatment): a correction to an underlying result must go through the
  existing audited match/result correction pathways and produce a *new*
  mid-season draft's snapshot, never a rewrite of this one.
- `midseason_draft_order` -- the *current* confirmed team order (seeded
  1:1 from the reverse of the frozen ladder snapshot, entry-id tie-break).
  Deliberately mutable (no immutability trigger): an audited Scorer
  override replaces its rows entirely, recorded via `append_event`'s
  before/after state -- the override never touches the frozen ladder
  snapshot above.
- `midseason_delisting` -- formal delistings, amendable (via withdrawal +
  resubmission) until the Scorer locks them; lock state is enforced by
  `app.midseason_draft`'s transactional lifecycle checks, the same
  pattern `app.preseason`'s `season_preseason_window.closed_at` uses --
  not a DB trigger, since it is ordinary mutable domain state gated by
  the parent `midseason_draft.state`, not a historical fact.
- `midseason_trade`/`midseason_trade_leg` -- proposed/approved/rejected
  player and round-based pick trades. A `status` transition
  (`pending -> approved|rejected`) is the only mutation ever made to a
  `midseason_trade` row; legs are never mutated after insert.
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_midseason_draft"
down_revision = "0026_lineup_adjudication"
branch_labels = None
depends_on = None

_LADDER_SNAPSHOT_TABLES = (
    "midseason_ladder_snapshot",
    "midseason_ladder_snapshot_row",
    "midseason_ladder_snapshot_reference",
)


def upgrade():
    bind = op.get_bind()

    # -- Season-level configuration: the BBBFFL round after which the
    # mid-season draft occurs. Nullable: most seasons (any pre-2026 replay
    # data, or a season that never runs a mid-season draft) simply never set
    # it, matching `regular_season_round_count`'s "add a dedicated column"
    # precedent (0009) but without that column's NOT NULL default, since
    # there is no sensible universal default trigger round.
    if bind.dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE bbbffl_season ADD COLUMN midseason_draft_trigger_round INTEGER "
            "CHECK (midseason_draft_trigger_round IS NULL OR midseason_draft_trigger_round > 0)"
        )
    else:
        op.add_column("bbbffl_season", sa.Column("midseason_draft_trigger_round", sa.Integer()))
        op.create_check_constraint(
            "ck_season_midseason_trigger_positive",
            "bbbffl_season",
            "midseason_draft_trigger_round IS NULL OR midseason_draft_trigger_round > 0",
        )

    # -- Generalise season_draft to carry more than one draft per season.
    with op.batch_alter_table("season_draft") as batch:
        batch.add_column(sa.Column("draft_kind", sa.Text(), nullable=False, server_default="preseason"))
        batch.drop_constraint("uq_draft_season", type_="unique")
        batch.create_unique_constraint("uq_draft_season_kind", ["season_id", "draft_kind"])
        batch.create_check_constraint("ck_draft_kind_valid", "draft_kind IN ('preseason', 'midseason')")

    # -- Mid-season draft lifecycle.
    op.create_table(
        "midseason_draft",
        sa.Column("midseason_draft_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "competition_id",
            sa.Text(),
            sa.ForeignKey("competition_stream.competition_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trigger_round_sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("ladder_confirmed_at", sa.Text(), nullable=False),
        sa.Column("delisting_opened_at", sa.Text()),
        sa.Column("delistings_locked_at", sa.Text()),
        sa.Column("draft_completed_at", sa.Text()),
        sa.Column("completed_at", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("season_id", name="uq_midseason_draft_season"),
        sa.CheckConstraint("trigger_round_sequence > 0", name="ck_midseason_draft_trigger_positive"),
        sa.CheckConstraint(
            "state IN ('ladder_confirmed', 'delisting_open', 'delistings_locked', 'draft_open', "
            "'draft_complete', 'complete')",
            name="ck_midseason_draft_state_valid",
        ),
    )

    op.create_table(
        "midseason_ladder_snapshot",
        sa.Column("snapshot_id", sa.Text(), primary_key=True),
        sa.Column(
            "midseason_draft_id",
            sa.Text(),
            sa.ForeignKey("midseason_draft.midseason_draft_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("through_round", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("midseason_draft_id", name="uq_midseason_ladder_snapshot_draft"),
    )

    op.create_table(
        "midseason_ladder_snapshot_row",
        sa.Column("row_id", sa.Text(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Text(),
            sa.ForeignKey("midseason_ladder_snapshot.snapshot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("tied", sa.Boolean(), nullable=False),
        sa.Column("played", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("draws", sa.Integer(), nullable=False),
        sa.Column("losses", sa.Integer(), nullable=False),
        sa.Column("points_for", sa.Text(), nullable=False),
        sa.Column("points_against", sa.Text(), nullable=False),
        sa.Column("percentage", sa.Text(), nullable=False),
        sa.Column("competition_points", sa.Integer(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "season_entry_id", name="uq_midseason_ladder_row_entry"),
    )

    op.create_table(
        "midseason_ladder_snapshot_reference",
        sa.Column("reference_id", sa.Text(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Text(),
            sa.ForeignKey("midseason_ladder_snapshot.snapshot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("matchup_id", sa.Text(), nullable=False),
        sa.Column("official_version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_midseason_ladder_reference_snapshot", "midseason_ladder_snapshot_reference", ["snapshot_id"])

    op.create_table(
        "midseason_draft_order",
        sa.Column(
            "midseason_draft_id",
            sa.Text(),
            sa.ForeignKey("midseason_draft.midseason_draft_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("midseason_draft_id", "position"),
        sa.UniqueConstraint("midseason_draft_id", "season_entry_id", name="uq_midseason_order_entry"),
        sa.CheckConstraint("position > 0", name="ck_midseason_order_position_positive"),
        sa.CheckConstraint("source IN ('ladder', 'override')", name="ck_midseason_order_source_valid"),
    )

    op.create_table(
        "midseason_delisting",
        sa.Column("delisting_id", sa.Text(), primary_key=True),
        sa.Column(
            "midseason_draft_id",
            sa.Text(),
            sa.ForeignKey("midseason_draft.midseason_draft_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("season_player_id", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.Text(), nullable=False),
        sa.Column("withdrawn_at", sa.Text()),
        sa.Column("locked_at", sa.Text()),
        sa.Column("reason", sa.Text()),
    )
    op.create_index(
        "uq_midseason_delisting_active_player",
        "midseason_delisting",
        ["midseason_draft_id", "season_player_id"],
        unique=True,
        postgresql_where=sa.text("withdrawn_at IS NULL"),
        sqlite_where=sa.text("withdrawn_at IS NULL"),
    )

    op.create_table(
        "midseason_trade",
        sa.Column("trade_id", sa.Text(), primary_key=True),
        sa.Column(
            "midseason_draft_id",
            sa.Text(),
            sa.ForeignKey("midseason_draft.midseason_draft_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("proposed_at", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Text()),
        sa.Column("decision_reason", sa.Text()),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column(
            "audit_event_id", sa.Text(), sa.ForeignKey("audit_event.event_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_midseason_trade_status_valid"),
    )
    op.create_index("ix_midseason_trade_draft", "midseason_trade", ["midseason_draft_id", "status"])

    op.create_table(
        "midseason_trade_leg",
        sa.Column("leg_id", sa.Text(), primary_key=True),
        sa.Column(
            "trade_id", sa.Text(), sa.ForeignKey("midseason_trade.trade_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("leg_type", sa.Text(), nullable=False),
        sa.Column("from_season_entry_id", sa.Text(), nullable=False),
        sa.Column("to_season_entry_id", sa.Text(), nullable=False),
        sa.Column("season_player_id", sa.Text()),
        sa.Column("draft_round", sa.Integer()),
        sa.CheckConstraint("leg_type IN ('player', 'pick')", name="ck_midseason_leg_type_valid"),
        sa.CheckConstraint(
            "(leg_type = 'player') = (season_player_id IS NOT NULL)", name="ck_midseason_leg_player_shape"
        ),
        sa.CheckConstraint("(leg_type = 'pick') = (draft_round IS NOT NULL)", name="ck_midseason_leg_pick_shape"),
        sa.CheckConstraint("from_season_entry_id <> to_season_entry_id", name="ck_midseason_leg_changes_owner"),
    )
    op.create_index("ix_midseason_trade_leg_trade", "midseason_trade_leg", ["trade_id"])

    # -- Immutability: only the frozen ladder-order snapshot is a true
    # historical fact this migration protects at the database level (see
    # module docstring for why the other new tables are deliberately not
    # trigger-protected).
    if bind.dialect.name == "sqlite":
        for table in _LADDER_SNAPSHOT_TABLES:
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
        CREATE FUNCTION reject_immutable_midseason_ladder_change() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'mid-season ladder snapshot history is immutable'; END; $$ LANGUAGE plpgsql
        """)
        for table in _LADDER_SNAPSHOT_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_immutable_midseason_ladder_change()"
            )


def downgrade():
    bind = op.get_bind()
    for table in ("midseason_draft", "midseason_delisting", "midseason_trade"):
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError(f"0027 downgrade refused: {table} history would be lost")
    if bind.execute(sa.text("SELECT COUNT(*) FROM season_draft WHERE draft_kind <> 'preseason'")).scalar_one():
        raise RuntimeError("0027 downgrade refused: non-preseason draft history would be lost")

    if bind.dialect.name == "sqlite":
        for table in _LADDER_SNAPSHOT_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_update")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_delete")
    else:
        for table in _LADDER_SNAPSHOT_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.execute("DROP FUNCTION reject_immutable_midseason_ladder_change() CASCADE")

    for table in (
        "midseason_trade_leg",
        "midseason_trade",
        "midseason_delisting",
        "midseason_draft_order",
        "midseason_ladder_snapshot_reference",
        "midseason_ladder_snapshot_row",
        "midseason_ladder_snapshot",
        "midseason_draft",
    ):
        op.drop_table(table)

    # draft_order_position/draft_pick/season_preseason_window all hold
    # foreign keys into season_draft.draft_id -- with a guaranteed-empty
    # midseason draft_kind (checked above) there is at most an ordinary
    # preseason draft's own rows, which the batch recreate below preserves
    # unchanged (same draft_id values, copied then renamed back), but
    # SQLite's FK enforcement still refuses the transient DROP of the old
    # table underneath them unless checking is suspended for this operation.
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("season_draft") as batch:
        batch.drop_constraint("ck_draft_kind_valid", type_="check")
        batch.drop_constraint("uq_draft_season_kind", type_="unique")
        batch.create_unique_constraint("uq_draft_season", ["season_id"])
        batch.drop_column("draft_kind")
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=ON")

    if bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE bbbffl_season DROP COLUMN midseason_draft_trigger_round")
    else:
        op.drop_constraint("ck_season_midseason_trigger_positive", "bbbffl_season", type_="check")
        op.drop_column("bbbffl_season", "midseason_draft_trigger_round")
