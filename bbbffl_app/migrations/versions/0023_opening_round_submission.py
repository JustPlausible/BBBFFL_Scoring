"""Explicit per-season-entry Opening Round submission confirmation (issue #133).

The first authoritative browser replay following issue #131 exposed two
defects: the represented-entry nomination response duplicated persisted
nominations whenever several accepted rules shared one target BBBFFL round
(a read-path bug, fixed entirely in `app.opening_round` without any schema
change), and season/round readiness incorrectly inferred that owning an
eligible Opening Round club's player made a nomination for that club
*mandatory*. The historical BBBFFL process was always a partial submission:
a coach/proxy could nominate zero or more eligible owned players and
deliberately leave the rest vacant.

This migration adds the schema for the second fix: an explicit, persisted
per-`(season_id, season_entry_id)` **submission** boundary, independent of
how many nominations exist. `opening_round_submission` /
`opening_round_submission_revision` are a header+revision pair versioned
exactly like `opening_round_rule`/`opening_round_rule_revision`
(migrations/versions/0020_opening_round_deferral.py) and
`round_afl_mapping`/`round_afl_mapping_revision` before that: the header
carries only stable identity (`season_id`, `season_entry_id`) and a
`current_revision` pointer, and each revision carries the mutable `state`
(`'draft'` | `'confirmed'`), confirmation timestamp, actor provenance and an
optional reason. There is no row at all until an entry's submission is
first confirmed -- an entry that never confirms simply has no row, exactly
like a season/club that has never proposed an Opening Round rule.

A **confirmed** revision is authoritative completeness: `is_ready` season
readiness and the round-preflight dependency gate
(`app.round_preflight.build_round_preflight`) key off confirmed submissions,
never off nomination counts or eligible-club ownership. **Reopening** a
confirmed submission (`app.opening_round.OpeningRoundSubmissionRepository.
reopen`) writes a new `'draft'` revision with a mandatory reason, exactly
like `opening_round_rule`'s `correct()` requires one -- a confirmed
submission is never silently mutated back to draft, and its full revision
history (including every past confirmation/reopen) remains readable via
`history()`, never overwritten.
"""

import sqlalchemy as sa
from alembic import op

revision = "0023_opening_round_submission"
down_revision = "0022_acting_context"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "opening_round_submission",
        sa.Column("submission_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["season_entry_id", "season_id"],
            ["season_entry.season_entry_id", "season_entry.season_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("season_id", "season_entry_id", name="uq_opening_round_submission_entry"),
        sa.CheckConstraint("current_revision >= 1", name="ck_opening_round_submission_revision_positive"),
    )
    op.create_table(
        "opening_round_submission_revision",
        sa.Column(
            "submission_id",
            sa.Text(),
            sa.ForeignKey("opening_round_submission.submission_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.Text()),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("actor_role", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("submission_id", "revision"),
        sa.CheckConstraint("state IN ('draft', 'confirmed')", name="ck_opening_round_submission_state"),
        sa.CheckConstraint("revision >= 1", name="ck_opening_round_submission_history_revision_positive"),
        sa.CheckConstraint(
            "(state = 'confirmed' AND confirmed_at IS NOT NULL) OR state <> 'confirmed'",
            name="ck_opening_round_submission_confirmed_has_timestamp",
        ),
    )
    op.create_index(
        "ix_opening_round_submission_revision_confirmed_at",
        "opening_round_submission_revision",
        ["confirmed_at"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM opening_round_submission")).scalar_one():
        raise RuntimeError("0023 downgrade refused: Opening Round submission confirmation history would be lost")
    op.drop_index("ix_opening_round_submission_revision_confirmed_at", table_name="opening_round_submission_revision")
    op.drop_table("opening_round_submission_revision")
    op.drop_table("opening_round_submission")
