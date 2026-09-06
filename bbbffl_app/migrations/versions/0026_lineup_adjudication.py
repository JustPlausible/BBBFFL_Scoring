"""Per-position draft-slot provenance and audited adjudication of a missed
initial weekly-lineup submission after lockout (issue #146).

## Per-position draft provenance

`weekly_lineup_draft_slot` previously carried no timing/actor evidence of
its own -- `save_draft` deleted and re-inserted every position on every
save, and the only timestamp available was the whole-draft
`weekly_lineup.updated_at`. That is not reliable evidence once a coach
legitimately edits a still-unlocked position *after* a selective lockout
has already activated: the whole-draft timestamp advances even though the
already-locked positions were not touched, and there was no way to prove
what those positions held, or when, before the lock instant.

This adds four columns to `weekly_lineup_draft_slot` -- `updated_at`,
`actor_type`, `actor_id`, `actor_role` -- populated per position, not
per whole draft. `app.lineups.WeeklyLineupRepository.save_draft` now
upserts each position individually and only advances a position's own
`updated_at`/actor columns when that position's *value* actually changes;
an untouched position keeps whatever timestamp/actor it already had,
tracing all the way back to when it was first set. This is the minimum
additional evidence the issue's inspection called for: a frozen,
per-position last-changed marker, not general-purpose full draft
versioning/history (no prior values are retained -- only "when/by whom was
the *current* value most recently set").

Existing rows have no such history to recover, so this migration backfills
`updated_at` from each draft's own `weekly_lineup.updated_at` (the closest
available truth for pre-existing drafts: correct whenever the draft was
never edited after a trigger activated, which is the common case, and
otherwise a conservative/safe value that never *understates* how recent a
position's value is) and leaves `actor_type`/`actor_id`/`actor_role` null
(no actor was ever attributed to a draft save before this change).

## Adjudicated missed-submission audit trail

`ck_submission_source` (0011, widened by 0025) is widened again to accept
two new, distinct `weekly_lineup_submission.source_type` values used only
by `app.lineup_adjudication` (issue #146):

- `'scorer_late_capture'` -- the round's first authoritative submission,
  built from a Scorer/Admin-approved pre-lockout private draft, after the
  coach never submitted before an activated trigger closed ordinary
  submission for one or more positions.
- `'scorer_adjudicated_carry_forward'` -- the round's first authoritative
  submission, sourced from the previous round's effective submitted lineup
  under the established BBBFFL carry-forward rules, created through this
  adjudicated path specifically because ordinary carry-forward would now
  collide with an activated lock. Deliberately distinct from the existing
  `'carry_forward'` source type so an adjudicated fallback is never
  indistinguishable from an ordinary, pre-lockout carry-forward.

`lineup_adjudication`/`lineup_adjudication_slot` are the immutable audit
record, structurally parallel to 0025's `weekly_lineup_correction`/
`weekly_lineup_correction_slot`: a header (decision type, resulting
version, actor/role, the required substantive reason recording the
externally-reached league decision, and either the source draft
revision/timestamp or the previous round/version this fallback carried
forward) plus one row per scoring position recording, for every position
that was already locked, the trigger/match/effective-lock evidence that
position's accepted value (or vacancy) traces back to. `to_version` is
always `1` -- this workflow only ever creates a lineup's *first*
authoritative submission (see `app.lineups.WeeklyLineupRepository.
submit_adjudicated_first_submission`); an existing submission is instead
corrected through issue #137's separate `weekly_lineup_correction` trail.
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_lineup_adjudication"
down_revision = "0025_lineup_correction"
branch_labels = None
depends_on = None

POSITIONS = "'F1','F2','F3','M1','M2','M3','Ruck','Tackler','Interchange'"
DECISION_TYPES = "'accept_evidenced_draft','apply_carry_forward'"


def upgrade():
    bind = op.get_bind()

    # -- Per-position draft-slot provenance ---------------------------------
    op.add_column("weekly_lineup_draft_slot", sa.Column("updated_at", sa.Text()))
    op.add_column("weekly_lineup_draft_slot", sa.Column("actor_type", sa.Text()))
    op.add_column("weekly_lineup_draft_slot", sa.Column("actor_id", sa.Text()))
    op.add_column("weekly_lineup_draft_slot", sa.Column("actor_role", sa.Text()))
    op.execute(
        "UPDATE weekly_lineup_draft_slot SET updated_at = "
        "(SELECT w.updated_at FROM weekly_lineup w WHERE w.lineup_id = weekly_lineup_draft_slot.lineup_id)"
    )
    with op.batch_alter_table("weekly_lineup_draft_slot") as batch:
        batch.alter_column("updated_at", existing_type=sa.Text(), nullable=False)

    # -- Widen the submission-source allowlist ------------------------------
    with op.batch_alter_table("weekly_lineup_submission") as batch:
        batch.drop_constraint("ck_submission_source", type_="check")
        batch.create_check_constraint(
            "ck_submission_source",
            "source_type IN ('coach','scorer_proxy','carry_forward','system_derived','scorer_correction',"
            "'scorer_late_capture','scorer_adjudicated_carry_forward')",
        )
    if bind.dialect.name == "sqlite":
        # See 0025's matching comment: SQLite's batch rebuild silently drops
        # 0011's hand-written immutability triggers, so they must be
        # recreated again here too.
        op.execute(
            "CREATE TRIGGER weekly_lineup_submission_no_update BEFORE UPDATE ON weekly_lineup_submission "
            "BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER weekly_lineup_submission_no_delete BEFORE DELETE ON weekly_lineup_submission "
            "BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
        )

    # -- Adjudication audit trail --------------------------------------------
    op.create_table(
        "lineup_adjudication",
        sa.Column("adjudication_id", sa.Text(), primary_key=True),
        sa.Column(
            "lineup_id", sa.Text(), sa.ForeignKey("weekly_lineup.lineup_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("decision_type", sa.Text(), nullable=False),
        sa.Column("submission_version", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("actor_role", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        # Resolution A (accept_evidenced_draft) evidence -- null for B.
        sa.Column("source_draft_revision", sa.Integer()),
        sa.Column("source_draft_saved_at", sa.Text()),
        # Resolution B (apply_carry_forward) evidence -- null for A.
        sa.Column("source_bbbffl_round_id", sa.Text()),
        sa.Column("source_lineup_id", sa.Text()),
        sa.Column("source_submission_version", sa.Integer()),
        sa.ForeignKeyConstraint(
            ["lineup_id", "submission_version"],
            ["weekly_lineup_submission.lineup_id", "weekly_lineup_submission.version"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("lineup_id", "submission_version", name="uq_adjudication_lineup_version"),
        sa.CheckConstraint(f"decision_type IN ({DECISION_TYPES})", name="ck_adjudication_decision_type"),
        sa.CheckConstraint("submission_version = 1", name="ck_adjudication_first_submission_only"),
    )
    op.create_index("ix_lineup_adjudication_lineup", "lineup_adjudication", ["lineup_id"])
    op.create_index("ix_lineup_adjudication_round", "lineup_adjudication", ["bbbffl_round_id"])

    op.create_table(
        "lineup_adjudication_slot",
        sa.Column(
            "adjudication_id",
            sa.Text(),
            sa.ForeignKey("lineup_adjudication.adjudication_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column(
            "season_player_id",
            sa.Text(),
            sa.ForeignKey("season_player_pool.season_player_id", ondelete="RESTRICT"),
        ),
        sa.Column("was_locked", sa.Integer(), nullable=False),
        sa.Column("evidence_status", sa.Text(), nullable=False),
        sa.Column("lock_reason", sa.Text()),
        sa.Column("afl_match_id", sa.Integer()),
        sa.Column("effective_lock_at", sa.Text()),
        sa.Column("observed_status", sa.Text()),
        sa.Column("evidence_saved_at", sa.Text()),
        sa.PrimaryKeyConstraint("adjudication_id", "position"),
        sa.CheckConstraint(f"position IN ({POSITIONS})", name="ck_adjudication_slot_position"),
    )

    if bind.dialect.name == "sqlite":
        for table in ("lineup_adjudication", "lineup_adjudication_slot"):
            op.execute(
                f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'lineup adjudication history is immutable'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'lineup adjudication history is immutable'); END"
            )
    elif bind.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_lineup_adjudication_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'lineup adjudication history is immutable'; END; $$ LANGUAGE plpgsql"
        )
        for table in ("lineup_adjudication", "lineup_adjudication_slot"):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION reject_lineup_adjudication_mutation()"
            )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM lineup_adjudication")).scalar_one():
        raise RuntimeError("0026 downgrade refused: audited lineup adjudication history would be lost")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_lineup_adjudication_mutation() CASCADE")
    op.drop_table("lineup_adjudication_slot")
    op.drop_table("lineup_adjudication")

    with op.batch_alter_table("weekly_lineup_submission") as batch:
        batch.drop_constraint("ck_submission_source", type_="check")
        batch.create_check_constraint(
            "ck_submission_source",
            "source_type IN ('coach','scorer_proxy','carry_forward','system_derived','scorer_correction')",
        )
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER weekly_lineup_submission_no_update BEFORE UPDATE ON weekly_lineup_submission "
            "BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER weekly_lineup_submission_no_delete BEFORE DELETE ON weekly_lineup_submission "
            "BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
        )

    with op.batch_alter_table("weekly_lineup_draft_slot") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("actor_type")
        batch.drop_column("actor_id")
        batch.drop_column("actor_role")
