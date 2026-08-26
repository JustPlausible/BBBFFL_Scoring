"""Append-only audit-event boundary (see app/audit.py).

Adds one domain-neutral `audit_event` table plus, as defence in depth beyond
the application-level guarantee, a database trigger on both supported
dialects that rejects UPDATE/DELETE against it. Application code (see
app/audit.py) never issues those statements; the trigger exists in case
something outside this codebase (an ad-hoc script, a future ORM misuse) ever
tries to.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_audit"
down_revision = "0002_competition"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_event",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("actor_role", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("entity_version", sa.Text()),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("before_state", sa.Text()),
        sa.Column("after_state", sa.Text()),
        sa.Column("payload", sa.Text()),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("event_id", name="uq_audit_event_event_id"),
    )
    op.create_index("ix_audit_event_entity", "audit_event", ["entity_type", "entity_id"])
    op.create_index("ix_audit_event_action", "audit_event", ["action"])
    op.create_index("ix_audit_event_correlation", "audit_event", ["correlation_id"])

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION audit_event_immutable() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'audit_event is append-only: % not permitted', TG_OP
                    USING ERRCODE = 'integrity_constraint_violation';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            "CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit_event "
            "FOR EACH ROW EXECUTE FUNCTION audit_event_immutable()"
        )
        op.execute(
            "CREATE TRIGGER audit_event_no_delete BEFORE DELETE ON audit_event "
            "FOR EACH ROW EXECUTE FUNCTION audit_event_immutable()"
        )
    else:
        op.execute(
            "CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit_event "
            "BEGIN SELECT RAISE(ABORT, 'audit_event is append-only: UPDATE not permitted'); END"
        )
        op.execute(
            "CREATE TRIGGER audit_event_no_delete BEFORE DELETE ON audit_event "
            "BEGIN SELECT RAISE(ABORT, 'audit_event is append-only: DELETE not permitted'); END"
        )


def downgrade():
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT COUNT(*) FROM audit_event")).scalar_one()
    if count:
        raise RuntimeError(
            "0003 downgrade refused: audit_event holds history that the prior schema cannot "
            "represent; back up and archive it manually before downgrading"
        )
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_event_no_update ON audit_event")
        op.execute("DROP TRIGGER IF EXISTS audit_event_no_delete ON audit_event")
        op.execute("DROP FUNCTION IF EXISTS audit_event_immutable()")
    else:
        op.execute("DROP TRIGGER IF EXISTS audit_event_no_update")
        op.execute("DROP TRIGGER IF EXISTS audit_event_no_delete")
    op.drop_index("ix_audit_event_correlation", table_name="audit_event")
    op.drop_index("ix_audit_event_action", table_name="audit_event")
    op.drop_index("ix_audit_event_entity", table_name="audit_event")
    op.drop_table("audit_event")
