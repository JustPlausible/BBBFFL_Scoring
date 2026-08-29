"""Season-scoped Opening Round deferred-selection configuration and nominations
(issue #69, follow-up to #31's exceptional round mapping).

Three AFL seasons (2024, 2025, 2026) used an "Opening Round" (AFL round
number 0) played by only a subset of clubs; each participating club later
received a compensating bye in a different, season-specific ordinary round.
BBBFFL allowed a coach/proxy to nominate an eligible Opening Round player into
a specific future BBBFFL lineup slot; when that later round was scored, that
one slot drew its statistics from the player's Opening Round match instead of
the round's ordinary mapped AFL round, while every other slot in the same
lineup continued to score normally (see docs/opening-round-deferred-
selection.md).

This is deliberately **not** a `season == 2024/2025/2026` special case: two
new tables model the *general* shape of the rule so any season could
configure it, and a season that configures nothing behaves exactly as before.

- `opening_round_rule` / `opening_round_rule_revision` is the season+club
  scoped configuration, versioned exactly like `round_afl_mapping`/
  `round_afl_mapping_revision` (migrations/versions/0008_round_mapping.py):
  the header carries only stable identity (`season_id`, `afl_club_id`) and a
  `current_revision` pointer; the revision carries the mutable
  `state` ('unresolved' | 'ambiguous' | 'accepted'), the AFL Opening Round
  identity, the club's compensating AFL bye round, the corresponding BBBFFL
  target round, and an evidence classification. Only an `accepted` revision
  is operational (see `app.round_mapping`'s "Acceptance is the only
  activation boundary" precedent).

- `opening_round_nomination` is the player-level decision: which specific
  owned player was nominated into which BBBFFL slot under an accepted rule,
  by whom, with what source AFL match. Unlike the rule table, a nomination is
  corrected in place (an authorised UPDATE, like
  `bbbffl_matchup_slot_ruling`/`interchange_assignment` in
  0019_round_review.py) rather than versioned as a separate revision history
  -- `app.audit`'s append-only event log already retains the required
  before/after/actor/reason trail for a correction (see
  `app.opening_round.OpeningRoundNominationRepository.correct`), so a second
  parallel history table is unnecessary.

Partial unique indexes enforce, for the *current* nomination row: at most one
nomination per (rule, season_entry) pair, at most one nomination per BBBFFL
target slot, and a player nominated at most once per target round/entry --
the same invariants `weekly_lineup_draft_slot`/`weekly_lineup_submission_slot`
already enforce for an ordinary lineup (0011_weekly_lineups.py).
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_opening_round_deferral"
down_revision = "0019_round_review"
branch_labels = None
depends_on = None

POSITIONS = "'F1','F2','F3','M1','M2','M3','Ruck','Tackler','Interchange'"
EVIDENCE_CLASSIFICATIONS = "'known_fact','reconstructable_behaviour','synthetic_scenario','unresolved_scorer_input'"


def upgrade():
    op.create_table(
        "opening_round_rule",
        sa.Column("rule_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("afl_club_id", sa.Integer(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("season_id", "afl_club_id", name="uq_opening_round_rule_season_club"),
        sa.CheckConstraint("current_revision >= 1", name="ck_opening_round_rule_revision_positive"),
    )
    op.create_table(
        "opening_round_rule_revision",
        sa.Column(
            "rule_id", sa.Text(), sa.ForeignKey("opening_round_rule.rule_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("afl_season_id", sa.Integer()),
        sa.Column("afl_opening_round_id", sa.Integer()),
        sa.Column("afl_bye_round_id", sa.Integer()),
        sa.Column("bbbffl_round_id", sa.Text(), sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT")),
        sa.Column("evidence_classification", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.PrimaryKeyConstraint("rule_id", "revision"),
        sa.CheckConstraint("state IN ('unresolved', 'ambiguous', 'accepted')", name="ck_opening_round_rule_state"),
        sa.CheckConstraint("revision >= 1", name="ck_opening_round_rule_history_revision_positive"),
        sa.CheckConstraint(
            "(state = 'accepted' AND afl_season_id IS NOT NULL AND afl_opening_round_id IS NOT NULL "
            "AND afl_bye_round_id IS NOT NULL AND bbbffl_round_id IS NOT NULL) OR state <> 'accepted'",
            name="ck_opening_round_rule_accepted_has_context",
        ),
        sa.CheckConstraint(
            f"evidence_classification IS NULL OR evidence_classification IN ({EVIDENCE_CLASSIFICATIONS})",
            name="ck_opening_round_rule_evidence_classification",
        ),
    )
    op.create_index(
        "ix_opening_round_rule_afl_context",
        "opening_round_rule_revision",
        ["afl_opening_round_id", "afl_bye_round_id"],
    )

    op.create_table(
        "opening_round_nomination",
        sa.Column("nomination_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "rule_id", sa.Text(), sa.ForeignKey("opening_round_rule.rule_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column(
            "season_player_id",
            sa.Text(),
            sa.ForeignKey("season_player_pool.season_player_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_afl_match_id", sa.Integer()),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("actor_role", sa.Text()),
        sa.Column("effective_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["season_entry_id", "season_id"],
            ["season_entry.season_entry_id", "season_entry.season_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"position IN ({POSITIONS})", name="ck_opening_round_nomination_position"),
        sa.UniqueConstraint("rule_id", "season_entry_id", name="uq_opening_round_nomination_rule_entry"),
        sa.UniqueConstraint("bbbffl_round_id", "season_entry_id", "position", name="uq_opening_round_nomination_slot"),
        sa.UniqueConstraint(
            "bbbffl_round_id", "season_entry_id", "season_player_id", name="uq_opening_round_nomination_player_once"
        ),
    )
    op.create_index(
        "ix_opening_round_nomination_round", "opening_round_nomination", ["bbbffl_round_id", "season_entry_id"]
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM opening_round_nomination")).scalar_one():
        raise RuntimeError("0020 downgrade refused: Opening Round nomination history would be lost")
    if bind.execute(sa.text("SELECT COUNT(*) FROM opening_round_rule")).scalar_one():
        raise RuntimeError("0020 downgrade refused: Opening Round rule configuration would be lost")
    op.drop_table("opening_round_nomination")
    op.drop_table("opening_round_rule_revision")
    op.drop_table("opening_round_rule")
