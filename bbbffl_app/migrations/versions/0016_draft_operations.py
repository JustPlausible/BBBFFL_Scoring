"""Scorer-operated draft workflow: pause/resume, finalisation, correction.

Adds the small domain primitives roadmap package 13 deliberately deferred
(see docs/draft-ledger.md): persisted pause state, explicit finalisation
distinct from mere completion, and a narrowly-scoped correction mechanism
for the most recently completed pick that never rewrites or deletes the
original completed `draft_pick` row.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_draft_ops"
down_revision = "0015_draft"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("season_draft", sa.Column("paused_at", sa.Text()))
    op.add_column("season_draft", sa.Column("paused_reason", sa.Text()))
    op.add_column("season_draft", sa.Column("finalized_at", sa.Text()))
    op.add_column("season_draft", sa.Column("finalized_note", sa.Text()))

    # A correction never rewrites or deletes the original completed row
    # (the immutability triggers from 0015 continue to protect
    # selected_season_player_id/completed_at unconditionally). Instead a
    # correction points the original pick at a fresh replacement row for
    # the same slot -- so exactly one row per (draft_id, overall_number)
    # is ever "active" (superseded_by_draft_pick_id IS NULL) at a time,
    # while every earlier attempt for that slot remains in the table
    # forever as evidence of what was originally selected and by when.
    with op.batch_alter_table("draft_pick") as batch:
        batch.add_column(
            sa.Column(
                "superseded_by_draft_pick_id",
                sa.Text(),
                sa.ForeignKey(
                    "draft_pick.draft_pick_id",
                    ondelete="RESTRICT",
                    name="fk_draft_pick_superseded_by",
                    # A correction updates the original row's pointer and
                    # inserts its replacement row in the same transaction
                    # (app.draft.DraftRepository.correct_pick), and the
                    # partial-unique "one active row per slot" index (below)
                    # requires the original be marked superseded *before*
                    # the replacement exists to take its place -- so this FK
                    # must not be checked until commit. PostgreSQL supports
                    # this natively; SQLite requires the correction
                    # transaction to also issue `PRAGMA defer_foreign_keys =
                    # ON` (see app/draft.py) for this DDL-level deferral to
                    # actually take effect.
                    deferrable=True,
                    initially="DEFERRED",
                ),
            )
        )
        batch.drop_constraint("uq_draft_pick_sequence", type_="unique")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite's batch (copy-and-move) ALTER above recreated `draft_pick`
        # under the hood, which drops any trigger attached to the old table
        # object -- including 0015's completed-pick immutability triggers.
        # Postgres's ALTER TABLE is in-place, so its triggers are unaffected
        # and must not be recreated here (CREATE TRIGGER would conflict).
        op.execute(
            "CREATE TRIGGER completed_pick_immutable BEFORE UPDATE OF selected_season_player_id, completed_at ON draft_pick WHEN OLD.completed_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'completed draft pick is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER completed_pick_delete_immutable BEFORE DELETE ON draft_pick WHEN OLD.completed_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'completed draft pick is immutable'); END"
        )
    op.create_index(
        "uq_draft_pick_active_sequence",
        "draft_pick",
        ["draft_id", "overall_number"],
        unique=True,
        postgresql_where=sa.text("superseded_by_draft_pick_id IS NULL"),
        sqlite_where=sa.text("superseded_by_draft_pick_id IS NULL"),
    )

    op.create_table(
        "draft_pick_correction",
        sa.Column("correction_id", sa.Text(), primary_key=True),
        sa.Column(
            "original_draft_pick_id",
            sa.Text(),
            sa.ForeignKey("draft_pick.draft_pick_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "replacement_draft_pick_id",
            sa.Text(),
            sa.ForeignKey("draft_pick.draft_pick_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("corrected_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "audit_event_id", sa.Text(), sa.ForeignKey("audit_event.event_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.CheckConstraint("original_draft_pick_id <> replacement_draft_pick_id", name="ck_correction_distinct_picks"),
    )
    op.create_index("ix_draft_pick_correction_original", "draft_pick_correction", ["original_draft_pick_id"])

    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER draft_pick_correction_immutable_update BEFORE UPDATE ON draft_pick_correction "
            "BEGIN SELECT RAISE(ABORT, 'draft pick correction history is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER draft_pick_correction_immutable_delete BEFORE DELETE ON draft_pick_correction "
            "BEGIN SELECT RAISE(ABORT, 'draft pick correction history is immutable'); END"
        )
    else:
        op.execute(
            "CREATE TRIGGER draft_pick_correction_immutable BEFORE UPDATE OR DELETE ON draft_pick_correction "
            "FOR EACH ROW EXECUTE FUNCTION reject_immutable_draft_change()"
        )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM draft_pick_correction")).scalar_one():
        raise RuntimeError("0016 downgrade refused: draft correction history would be lost")
    if bind.execute(sa.text("SELECT COUNT(*) FROM season_draft WHERE finalized_at IS NOT NULL")).scalar_one():
        raise RuntimeError("0016 downgrade refused: draft finalisation state would be lost")

    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS draft_pick_correction_immutable_delete")
        op.execute("DROP TRIGGER IF EXISTS draft_pick_correction_immutable_update")
    else:
        op.execute("DROP TRIGGER IF EXISTS draft_pick_correction_immutable ON draft_pick_correction")
    op.drop_table("draft_pick_correction")
    op.drop_index("uq_draft_pick_active_sequence", table_name="draft_pick")
    with op.batch_alter_table("draft_pick") as batch:
        batch.drop_column("superseded_by_draft_pick_id")
        batch.create_unique_constraint("uq_draft_pick_sequence", ["draft_id", "overall_number"])
    if bind.dialect.name == "sqlite":
        # Restore 0015's completed-pick immutability triggers -- SQLite's
        # batch (copy-and-move) ALTER above recreated `draft_pick` again,
        # dropping whatever triggers (including the ones this revision's
        # upgrade() re-created) were attached to the prior table object.
        op.execute(
            "CREATE TRIGGER completed_pick_immutable BEFORE UPDATE OF selected_season_player_id, completed_at ON draft_pick WHEN OLD.completed_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'completed draft pick is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER completed_pick_delete_immutable BEFORE DELETE ON draft_pick WHEN OLD.completed_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'completed draft pick is immutable'); END"
        )
    op.drop_column("season_draft", "finalized_note")
    op.drop_column("season_draft", "finalized_at")
    op.drop_column("season_draft", "paused_reason")
    op.drop_column("season_draft", "paused_at")
