"""Issue #219: a safe pre-activation removal primitive for a lockout trigger.

`LockoutTriggerRepository` previously had `create`/`replace`/`configure` --
every one of them a create-or-revise operation -- but no way to drop a
trigger key from a round's plan entirely. A Scorer who created an
unnecessary selective trigger (e.g. a mistaken `early-1`) before activation
had no supported way to remove it and leave a valid main-only plan; the
only documented workaround was repointing it at the same match as `main`,
which misrepresents the actual competition decision in the persisted
configuration/audit history (see `docs/2026-finals-superscore-design.md`
and issue #219 itself).

This adds three nullable columns to the existing trigger header table --
mirroring how `bbbffl_round_lockout_trigger_activation` already records
activation as a *fact about* a trigger, separate from its revision history,
rather than a new revision or a destructive delete. A removed trigger's
`bbbffl_round_lockout_trigger_revision`/`..._match` history is untouched and
remains fully readable -- removal is a header-level fact, exactly like
activation, never a rewrite of revision history. Additive only: every
existing row gets `removed_at IS NULL`, unchanged from every existing
reader's point of view before this migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "0034_lockout_trigger_removal"
down_revision = "0033_season_award"
branch_labels = None
depends_on = None


def upgrade():
    # `op.batch_alter_table` (SQLite's copy-and-move strategy) rather than
    # plain `op.add_column`/`op.create_check_constraint`, matching this
    # migration series' established convention (e.g. 0025, 0029) --
    # SQLite's ALTER TABLE cannot add a CHECK constraint to an existing
    # table directly.
    with op.batch_alter_table("bbbffl_round_lockout_trigger") as batch:
        batch.add_column(sa.Column("removed_at", sa.Text(), nullable=True))
        batch.add_column(sa.Column("removed_by", sa.Text(), nullable=True))
        batch.add_column(sa.Column("removed_reason", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_lockout_trigger_removal_all_or_none",
            "(removed_at IS NULL AND removed_by IS NULL AND removed_reason IS NULL) OR "
            "(removed_at IS NOT NULL AND removed_reason IS NOT NULL)",
        )


def downgrade():
    with op.batch_alter_table("bbbffl_round_lockout_trigger") as batch:
        batch.drop_constraint("ck_lockout_trigger_removal_all_or_none", type_="check")
        batch.drop_column("removed_reason")
        batch.drop_column("removed_by")
        batch.drop_column("removed_at")
