"""Issue #195: persisted, versioned premiership/wooden-spoon award records.

`docs/2026-finals-superscore-design.md`'s "Grand Final/season winner
recording and end-of-season completion" section is explicit that these are
**persisted, versioned records**, not merely the bare `finals.premier.
recorded`/`finals.wooden_spoon.recorded` audit events issue #191 already
appends on Grand Final publication/correction (`app/finals_review.py`):
"New: explicit `season.premiership.recorded` and `season.wooden_spoon.
recorded` ... audit provenance plus persisted, versioned records."

## Why this shape

Mirrors `finals_bracket_pairing`/`finals_bracket_elimination`'s established
"one active row at a time, a correction supersedes rather than overwrites"
pattern (migration `0030_finals_bracket.py`): a later relevant correction
must never silently mutate the existing frozen record (issue #195's
explicit requirement) -- it must instead append a new `status='active'` row
referencing the newly-effective source versions and flip the previous row
to `status='superseded'`, linked by `superseded_by_award_id`. The partial
unique index below (`status='active'` only) is what lets "supersede" and
"the current one" coexist safely: at most one *active* row per
`(season_id, award_type)` at a time, but the full history remains
queryable for audit. Exactly like `finals_bracket_pairing`/`finals_bracket_
elimination`, this table is deliberately **not** given a database-level
immutable-history trigger (unlike `finals_bracket_seed`/`finals_bracket_
result_reference`, which really are frozen once written): the `status`/
`superseded_by_award_id` columns must remain updatable for the supersede
step itself, so the append-only guarantee comes from the domain code
(`app.season_awards.SeasonAwardRepository`) only ever superseding, never
rewriting a row's award-describing fields, plus every prior version
staying permanently queryable.

`provenance` is a JSON blob rather than a fixed set of columns because the
two award types reference genuinely different source shapes: the
premiership references the effective Grand Final result (`bracket_id`,
`grand_final_matchup_id`, `official_version`); the wooden spoon references
the live mathematical Round 20 ladder (`ordinary_competition_id`,
`through_round`, `latest_included_round`, `result_references`). Both are
frozen at the moment the record is written, exactly like `finals_bracket`'s
own `result_references` freeze -- never re-derived by a later reader.
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_season_award"
down_revision = "0032_superscore_results"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "season_award",
        sa.Column("award_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("award_type", sa.Text(), nullable=False),
        sa.Column(
            "season_entry_id",
            sa.Text(),
            sa.ForeignKey("season_entry.season_entry_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "runner_up_season_entry_id",
            sa.Text(),
            sa.ForeignKey("season_entry.season_entry_id", ondelete="RESTRICT"),
        ),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("superseded_by_award_id", sa.Text(), sa.ForeignKey("season_award.award_id", ondelete="RESTRICT")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.CheckConstraint("award_type IN ('premiership', 'wooden_spoon')", name="ck_season_award_type"),
        sa.CheckConstraint("status IN ('active', 'superseded')", name="ck_season_award_status"),
        sa.CheckConstraint(
            "(award_type = 'premiership' AND runner_up_season_entry_id IS NOT NULL) OR "
            "(award_type = 'wooden_spoon' AND runner_up_season_entry_id IS NULL)",
            name="ck_season_award_runner_up_shape",
        ),
    )
    op.create_index(
        "uq_season_award_active_type",
        "season_award",
        ["season_id", "award_type"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_season_award_season", "season_award", ["season_id"])


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM season_award")).scalar_one():
        raise RuntimeError("0033 downgrade refused: season award history would be lost")
    op.drop_table("season_award")
