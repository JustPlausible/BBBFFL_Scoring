"""Original single-competition prototype schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001_prototype"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "slot_dnp",
        sa.Column("team_key", sa.Text(), nullable=False),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("dnp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("team_key", "slot"),
    )
    op.create_table(
        "interchange_assignment",
        sa.Column("team_key", sa.Text(), nullable=False),
        sa.Column("target_position", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("team_key"),
    )
    op.create_table(
        "score_override",
        sa.Column("team_key", sa.Text(), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("override_score", sa.Float()),
        sa.Column("reason", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("team_key", "position"),
    )
    op.create_table(
        "matchup_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("finalized", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finalized_at", sa.Text()),
        sa.Column("finalized_note", sa.Text()),
        sa.Column("finalized_snapshot", sa.Text()),
        sa.CheckConstraint("id = 1"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("matchup_state")
    op.drop_table("score_override")
    op.drop_table("interchange_assignment")
    op.drop_table("slot_dnp")
