"""Scorer round-review, sign-off and correction workflow (roadmap package 28,
issue #58).

This migration adds exactly what the persisted ordinary-round lifecycle
(#32, `app/competition_lifecycle.py`) and generalised match scoring (#35,
`app/calculations.py`) do not yet have: a place to record attributable
scorer rulings/overrides against a *persisted* `bbbffl_matchup` (as opposed
to the Grand Final/SuperScore vertical's `competition_key`/`team_key`-scoped
`slot_dnp`/`interchange_assignment`/`score_override` tables in
`app/db.py`, which remain untouched and unrelated), plus enough columns on
the existing lifecycle tables to make sign-off atomic, stale-safe and
reproducible after the fact.

- `bbbffl_matchup.review_version` is a per-matchup optimistic-concurrency
  counter. Every ruling/override write against a matchup increments it;
  every ruling/override write and every sign-off/correction attempt must
  present the `review_version` it was read at, or fail closed as stale
  (see `app.round_review`). This is the same compare-and-swap idiom
  `weekly_lineup.draft_revision`/`effective_submission_version` already
  use in `app/lineups.py` -- a new column on the aggregate root being
  protected, not a new table.

- `bbbffl_official_result.input_snapshot` freezes the exact scoring inputs
  (rules version, lineup versions, calculated-result revision/fingerprint,
  DNP rulings, interchange rulings, overrides) a published/corrected
  official version was computed from, so that version's meaning never
  changes even if a lineup, rule, recommendation or calculated score is
  edited afterwards (issue #58 requirement 5). Nullable and additive: every
  existing row and every existing caller of `publish_results`/
  `correct_results` that does not pass a snapshot keeps working unchanged.

- `bbbffl_matchup_slot_ruling` / `bbbffl_matchup_interchange_ruling` /
  `bbbffl_matchup_override` are the ordinary-round equivalents of
  `slot_dnp` / `interchange_assignment` / `score_override`, keyed by
  `(matchup_id, season_entry_id, ...)` instead of `(competition_key,
  team_key, ...)`. `slot`/`position` use the weekly-lineup slot vocabulary
  (`app.lineups.POSITIONS`) -- the same vocabulary
  `app.calculations.MatchupCalculationService` already writes into each
  calculated snapshot's per-slot evidence -- rather than `app.scoring`'s
  internal `Forward1`/`Midfield1`/... names, so a ruling/override target
  always matches what the scorer sees in the calculated evidence.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_round_review"
down_revision = "0018_proxy_draft_source"
branch_labels = None
depends_on = None

SLOTS = "'F1','F2','F3','M1','M2','M3','Ruck','Tackler','Interchange'"
OVERRIDE_POSITIONS = "'F1','F2','F3','M1','M2','M3','Ruck','Tackler'"


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE bbbffl_matchup ADD COLUMN review_version INTEGER NOT NULL DEFAULT 1")
        op.execute("ALTER TABLE bbbffl_official_result ADD COLUMN input_snapshot TEXT")
    else:
        op.add_column("bbbffl_matchup", sa.Column("review_version", sa.Integer(), nullable=False, server_default="1"))
        op.create_check_constraint("ck_matchup_review_version", "bbbffl_matchup", "review_version >= 1")
        op.add_column("bbbffl_official_result", sa.Column("input_snapshot", sa.Text()))

    op.create_table(
        "bbbffl_matchup_slot_ruling",
        sa.Column(
            "matchup_id", sa.Text(), sa.ForeignKey("bbbffl_matchup.matchup_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("slot", sa.Text(), nullable=False),
        # Integer(0/1), not Boolean -- matches slot_dnp's existing
        # convention (see migrations/versions/0001_prototype_schema.py):
        # this codebase binds raw '?' parameters rather than going through
        # SQLAlchemy's Core type coercion, and PostgreSQL's boolean column
        # rejects a bound smallint outright where SQLite silently accepts
        # it (see app.round_review.RoundReviewRepository.record_dnp_ruling).
        sa.Column("dnp", sa.Integer(), nullable=False),
        sa.Column("decided_by_type", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_by_role", sa.Text()),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.PrimaryKeyConstraint("matchup_id", "season_entry_id", "slot"),
        sa.CheckConstraint(f"slot IN ({SLOTS})", name="ck_slot_ruling_slot"),
    )
    op.create_table(
        "bbbffl_matchup_interchange_ruling",
        sa.Column(
            "matchup_id", sa.Text(), sa.ForeignKey("bbbffl_matchup.matchup_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("target_position", sa.Text()),
        sa.Column("decided_by_type", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_by_role", sa.Text()),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.PrimaryKeyConstraint("matchup_id", "season_entry_id"),
        sa.CheckConstraint(
            f"target_position IS NULL OR target_position IN ({OVERRIDE_POSITIONS})", name="ck_interchange_ruling_target"
        ),
    )
    op.create_table(
        "bbbffl_matchup_override",
        sa.Column(
            "matchup_id", sa.Text(), sa.ForeignKey("bbbffl_matchup.matchup_id", ondelete="RESTRICT"), nullable=False
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
        sa.PrimaryKeyConstraint("matchup_id", "season_entry_id", "position"),
        sa.CheckConstraint(f"position IN ({OVERRIDE_POSITIONS})", name="ck_override_position"),
    )
    op.create_index("ix_matchup_override_matchup", "bbbffl_matchup_override", ["matchup_id"])
    op.create_index("ix_interchange_ruling_matchup", "bbbffl_matchup_interchange_ruling", ["matchup_id"])
    op.create_index("ix_slot_ruling_matchup", "bbbffl_matchup_slot_ruling", ["matchup_id"])


def downgrade():
    bind = op.get_bind()
    for table in (
        "bbbffl_matchup_override",
        "bbbffl_matchup_interchange_ruling",
        "bbbffl_matchup_slot_ruling",
    ):
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError(f"0019 downgrade refused: {table} scorer-review history would be lost")
    if bind.execute(
        sa.text("SELECT COUNT(*) FROM bbbffl_official_result WHERE input_snapshot IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("0019 downgrade refused: frozen official-result input snapshots would be lost")
    for table in ("bbbffl_matchup_override", "bbbffl_matchup_interchange_ruling", "bbbffl_matchup_slot_ruling"):
        op.drop_table(table)
    if bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE bbbffl_matchup DROP COLUMN review_version")
        op.execute("ALTER TABLE bbbffl_official_result DROP COLUMN input_snapshot")
    else:
        op.drop_constraint("ck_matchup_review_version", "bbbffl_matchup", type_="check")
        op.drop_column("bbbffl_matchup", "review_version")
        op.drop_column("bbbffl_official_result", "input_snapshot")
