"""SuperScore round lifecycle: stream header, durable per-entry review-state
rows and the entry-scoped DNP/Interchange/override ruling boundary (issue
#192, the third of six #170 follow-ups; depends on #197's
`create_non_ordinary_round` primitive, merged in 0029/0030).

## Why this shape

`docs/2026-finals-superscore-design.md`'s "SuperScore design" section
(especially "A genuine SuperScore-specific abstraction: leaderboard results,
not matchups") and issue #192's body are the authoritative source:

- SuperScore has no head-to-head pairing at all -- ten independent entries
  ranked by total score. `app.round_review`'s matchup-keyed
  `bbbffl_matchup_slot_ruling`/`bbbffl_matchup_interchange_ruling`/
  `bbbffl_matchup_override` cannot be reused (there is no `matchup_id` to
  key against), so this migration adds an **entry-scoped** counterpart of
  each, keyed by `(bbbffl_round_id, season_entry_id, ...)` instead --
  mirroring `app.round_review`'s validation/CAS/audit shape exactly (see
  migrations/versions/0019_round_review.py), only the key shape differs.
- `superscore_entry_review_state` is the durable serialization/CAS point
  for the lifetime of a SuperScore round -- **not** a counter riding on the
  entry-scoped calculation row #193 will add (which is optional/derived and
  may not exist yet when the first ruling or lineup mutation happens).
  Round setup creates one row per eligible entry (`review_version=0`)
  *before* any lineup, ruling or calculation exists; the round must not
  open until the complete set exists (see `app.superscore_round`). Every
  lineup write that changes an entry's effective submission (initial
  submission, unlocked resubmission, post-lockout correction -- see
  `app.lineups.WeeklyLineupRepository._finalize_submission`) and every
  entry-scoped ruling (see `app.superscore_review`) locks and advances this
  row's `review_version` in the same transaction as its own write.
  Calculation persistence (#193) only ever locks/compares/records against
  it -- it must never advance it.
- `superscore_stream` is a one-row-per-competition header recording which
  ordinary competition stream this SuperScore stream's SS1 cross-stream
  carry-forward fallback resolves against (`app.finals_participation.
  resolve_cross_stream_fallback_source`, shared verbatim with #191's
  identical finals Week 1/seed-1-Week-2 case) -- the SuperScore-stream
  counterpart of `finals_bracket.ordinary_competition_id`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_superscore_round"
down_revision = "0030_finals_bracket"
branch_labels = None
depends_on = None

SLOTS = "'F1','F2','F3','M1','M2','M3','Ruck','Tackler','Interchange'"
OVERRIDE_POSITIONS = "'F1','F2','F3','M1','M2','M3','Ruck','Tackler'"


def upgrade():
    op.create_table(
        "superscore_stream",
        sa.Column(
            "competition_id",
            sa.Text(),
            sa.ForeignKey("competition_stream.competition_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "ordinary_competition_id",
            sa.Text(),
            sa.ForeignKey("competition_stream.competition_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("season_id", "competition_id", name="uq_superscore_stream_season_competition"),
    )

    op.create_table(
        "superscore_entry_review_state",
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("bbbffl_round_id", "season_entry_id"),
        sa.CheckConstraint("review_version >= 0", name="ck_superscore_review_state_version"),
    )
    op.create_index("ix_superscore_review_state_round", "superscore_entry_review_state", ["bbbffl_round_id"])

    op.create_table(
        "superscore_entry_slot_ruling",
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("slot", sa.Text(), nullable=False),
        # Integer(0/1), matching bbbffl_matchup_slot_ruling's own convention
        # (see 0019_round_review.py) rather than a native Boolean column.
        sa.Column("dnp", sa.Integer(), nullable=False),
        sa.Column("decided_by_type", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_by_role", sa.Text()),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.PrimaryKeyConstraint("bbbffl_round_id", "season_entry_id", "slot"),
        sa.CheckConstraint(f"slot IN ({SLOTS})", name="ck_superscore_slot_ruling_slot"),
    )
    op.create_index("ix_superscore_slot_ruling_round", "superscore_entry_slot_ruling", ["bbbffl_round_id"])

    op.create_table(
        "superscore_entry_interchange_ruling",
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("target_position", sa.Text()),
        sa.Column("decided_by_type", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_by_role", sa.Text()),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.PrimaryKeyConstraint("bbbffl_round_id", "season_entry_id"),
        sa.CheckConstraint(
            f"target_position IS NULL OR target_position IN ({OVERRIDE_POSITIONS})",
            name="ck_superscore_interchange_ruling_target",
        ),
    )
    op.create_index(
        "ix_superscore_interchange_ruling_round", "superscore_entry_interchange_ruling", ["bbbffl_round_id"]
    )

    op.create_table(
        "superscore_entry_override",
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("override_score", sa.Numeric(12, 3), nullable=False),
        sa.Column("calculated_score", sa.Numeric(12, 3)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by_type", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_by_role", sa.Text()),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("bbbffl_round_id", "season_entry_id", "position"),
        sa.CheckConstraint(f"position IN ({OVERRIDE_POSITIONS})", name="ck_superscore_override_position"),
    )
    op.create_index("ix_superscore_override_round", "superscore_entry_override", ["bbbffl_round_id"])


def downgrade():
    bind = op.get_bind()
    for table in (
        "superscore_entry_override",
        "superscore_entry_interchange_ruling",
        "superscore_entry_slot_ruling",
        "superscore_entry_review_state",
        "superscore_stream",
    ):
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError(f"0031 downgrade refused: {table} SuperScore history would be lost")
    for table in (
        "superscore_entry_override",
        "superscore_entry_interchange_ruling",
        "superscore_entry_slot_ruling",
        "superscore_entry_review_state",
        "superscore_stream",
    ):
        op.drop_table(table)
