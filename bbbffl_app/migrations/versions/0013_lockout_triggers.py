"""Persist the BBBFFL round lockout-trigger plan and its activation evidence.

Which AFL matches constitute a round's selective (early) and main lockout
stages is a BBBFFL commissioner/scorer decision, not something inferred from
AFL scheduling -- see app/lockouts.py's module docstring. This establishes
the persisted configuration (revisable before it fires) and a separate,
round-scoped, immutable record of when each configured trigger actually
activated.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_lockout_triggers"
down_revision = "0012_lockouts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bbbffl_round_lockout_trigger",
        sa.Column("trigger_id", sa.Text(), primary_key=True),
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trigger_key", sa.Text(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("bbbffl_round_id", "trigger_key", name="uq_lockout_trigger_key"),
        sa.CheckConstraint("current_revision >= 1", name="ck_lockout_trigger_revision_positive"),
    )
    op.create_table(
        "bbbffl_round_lockout_trigger_revision",
        sa.Column(
            "trigger_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round_lockout_trigger.trigger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.PrimaryKeyConstraint("trigger_id", "revision"),
        sa.CheckConstraint("trigger_type IN ('selective','main')", name="ck_lockout_trigger_type"),
        sa.CheckConstraint("revision >= 1", name="ck_lockout_trigger_revision_history_positive"),
    )
    op.create_table(
        "bbbffl_round_lockout_trigger_match",
        sa.Column("trigger_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("afl_match_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trigger_id", "revision"],
            ["bbbffl_round_lockout_trigger_revision.trigger_id", "bbbffl_round_lockout_trigger_revision.revision"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("trigger_id", "revision", "afl_match_id"),
    )
    op.create_table(
        "bbbffl_round_lockout_trigger_activation",
        sa.Column(
            "trigger_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round_lockout_trigger.trigger_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("afl_match_id", sa.Integer(), nullable=False),
        sa.Column("observed_status", sa.Text(), nullable=False),
        sa.Column("effective_lock_at", sa.Text()),
        sa.Column("activation_reason", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    # Irreversible by design, same principle as 0012's weekly_lineup_lock:
    # once a trigger has activated, that fact -- and therefore the trigger's
    # frozen configuration (see LockoutTriggerRepository.replace) -- must
    # never be silently rewritten by a later upstream correction.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER lockout_trigger_activation_no_update BEFORE UPDATE ON bbbffl_round_lockout_trigger_activation BEGIN SELECT RAISE(ABORT, 'trigger activation evidence is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER lockout_trigger_activation_no_delete BEFORE DELETE ON bbbffl_round_lockout_trigger_activation BEGIN SELECT RAISE(ABORT, 'trigger activation evidence is immutable'); END"
        )
    elif bind.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_lockout_trigger_activation_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'trigger activation evidence is immutable'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER lockout_trigger_activation_immutable BEFORE UPDATE OR DELETE ON bbbffl_round_lockout_trigger_activation FOR EACH ROW EXECUTE FUNCTION reject_lockout_trigger_activation_mutation()"
        )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM bbbffl_round_lockout_trigger_activation")).scalar_one():
        raise RuntimeError("0013 downgrade refused: irreversible trigger activation evidence would be lost")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_lockout_trigger_activation_mutation() CASCADE")
    for table in (
        "bbbffl_round_lockout_trigger_activation",
        "bbbffl_round_lockout_trigger_match",
        "bbbffl_round_lockout_trigger_revision",
        "bbbffl_round_lockout_trigger",
    ):
        op.drop_table(table)
