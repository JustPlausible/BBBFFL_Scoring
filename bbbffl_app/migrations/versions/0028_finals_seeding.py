"""Replay-only 2026 finals-seeding snapshot (issue #187).

Builds on, rather than duplicating:

- the ladder read model (`app.ladder`) -- never persisted or made
  authoritative by this migration. `finals_seeding_snapshot_mathematical_row`
  freezes an independent, immutable *copy* of the calculated Round 20 ladder
  at the moment the snapshot is created, exactly as `0027_midseason_draft.
  py`'s `midseason_ladder_snapshot_row` already does for the mid-season
  draft-order basis. The live ladder calculation is completely untouched and
  remains the sole mathematical authority for competition standings.
- the audit boundary (`0003_audit_event.py`) for actor/reason/before-after
  history on the one snapshot-creation event.

## New tables

- `finals_seeding_snapshot` -- at most one row per season (enforced by
  `uq_finals_seeding_snapshot_season`), naming the ordinary competition and
  the `through_round` (always 20) the mathematical side of the snapshot was
  taken against.
- `finals_seeding_snapshot_mathematical_row` -- the frozen "before": one row
  per season entry, copied from `app.ladder.calculate_ladder`'s Round 20
  output at snapshot time. Never the live ladder itself.
- `finals_seeding_snapshot_reference` -- the frozen mathematical ladder's own
  official-result provenance (matchup id/version), matching
  `midseason_ladder_snapshot_reference`'s convention.
- `finals_seeding_snapshot_seed_row` -- the frozen "after": the fixed
  historical finals-seeding order (`app.finals_seeding.
  HISTORICAL_FINALS_SEED_TEAM_NAMES`), one row per seed position 1-10.

Unlike `midseason_draft_order`, there is deliberately no mutable "current
order" table and no override mechanism here: the historical seed is a fixed,
hard-coded historical fact (see `app.finals_seeding`'s module docstring),
never an arbitrary operator-supplied order, so every one of these four
tables is immutable -- there is nothing here for an audited correction to
ever legitimately rewrite.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_finals_seeding"
down_revision = "0027_midseason_draft"
branch_labels = None
depends_on = None

_IMMUTABLE_TABLES = (
    "finals_seeding_snapshot",
    "finals_seeding_snapshot_mathematical_row",
    "finals_seeding_snapshot_reference",
    "finals_seeding_snapshot_seed_row",
)


def upgrade():
    bind = op.get_bind()

    op.create_table(
        "finals_seeding_snapshot",
        sa.Column("snapshot_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "competition_id",
            sa.Text(),
            sa.ForeignKey("competition_stream.competition_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("through_round", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("season_id", name="uq_finals_seeding_snapshot_season"),
        sa.CheckConstraint("through_round > 0", name="ck_finals_seeding_snapshot_through_round_positive"),
    )

    op.create_table(
        "finals_seeding_snapshot_mathematical_row",
        sa.Column("row_id", sa.Text(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Text(),
            sa.ForeignKey("finals_seeding_snapshot.snapshot_id", ondelete="RESTRICT"),
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
        sa.UniqueConstraint("snapshot_id", "season_entry_id", name="uq_finals_seeding_math_row_entry"),
    )

    op.create_table(
        "finals_seeding_snapshot_reference",
        sa.Column("reference_id", sa.Text(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Text(),
            sa.ForeignKey("finals_seeding_snapshot.snapshot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("matchup_id", sa.Text(), nullable=False),
        sa.Column("official_version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_finals_seeding_reference_snapshot", "finals_seeding_snapshot_reference", ["snapshot_id"])

    op.create_table(
        "finals_seeding_snapshot_seed_row",
        sa.Column("row_id", sa.Text(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Text(),
            sa.ForeignKey("finals_seeding_snapshot.snapshot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("seed_position", sa.Integer(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "seed_position", name="uq_finals_seeding_seed_position"),
        sa.UniqueConstraint("snapshot_id", "season_entry_id", name="uq_finals_seeding_seed_entry"),
        # Bounded 1-10, not just positive: the historical 2026 finals seed
        # (`app.finals_seeding.HISTORICAL_FINALS_SEED_TEAM_NAMES`) always has
        # exactly ten positions. Combined with the two UNIQUE constraints
        # above (every position and every season_entry_id already taken once
        # `apply()` inserts its ten rows) and the UPDATE/DELETE immutability
        # triggers below, this closes the one remaining gap those leave open
        # -- an INSERT of an eleventh row (position 11, an otherwise-unused
        # season_entry_id) issued directly against the database after
        # creation, which no UPDATE/DELETE trigger or existing constraint
        # would reject (Codex review, PR #188).
        sa.CheckConstraint("seed_position BETWEEN 1 AND 10", name="ck_finals_seeding_seed_position_range"),
    )

    if bind.dialect.name == "sqlite":
        for table in _IMMUTABLE_TABLES:
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
        CREATE FUNCTION reject_immutable_finals_seeding_change() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'finals-seeding snapshot history is immutable'; END; $$ LANGUAGE plpgsql
        """)
        for table in _IMMUTABLE_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_immutable_finals_seeding_change()"
            )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM finals_seeding_snapshot")).scalar_one():
        raise RuntimeError("0028 downgrade refused: finals-seeding snapshot history would be lost")

    if bind.dialect.name == "sqlite":
        for table in _IMMUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_update")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_delete")
    else:
        for table in _IMMUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.execute("DROP FUNCTION reject_immutable_finals_seeding_change() CASCADE")

    for table in (
        "finals_seeding_snapshot_seed_row",
        "finals_seeding_snapshot_reference",
        "finals_seeding_snapshot_mathematical_row",
        "finals_seeding_snapshot",
    ):
        op.drop_table(table)
