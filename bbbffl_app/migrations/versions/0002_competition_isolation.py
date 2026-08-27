"""Scope existing scorer state by competition key."""

import sqlalchemy as sa
from alembic import op

revision = "0002_competition"
down_revision = "0001_prototype"
branch_labels = None
depends_on = None

TABLES = ("slot_dnp", "interchange_assignment", "score_override", "matchup_state")


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Some deployed pre-baseline databases predate frozen snapshots.
    if "finalized_snapshot" not in {c["name"] for c in inspector.get_columns("matchup_state")}:
        op.add_column("matchup_state", sa.Column("finalized_snapshot", sa.Text()))
    op.rename_table("slot_dnp", "slot_dnp_legacy")
    op.create_table(
        "slot_dnp",
        sa.Column("competition_key", sa.Text(), nullable=False, server_default="grand_final"),
        sa.Column("team_key", sa.Text(), nullable=False),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("dnp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("competition_key", "team_key", "slot"),
    )
    op.execute("INSERT INTO slot_dnp SELECT 'grand_final', team_key, slot, dnp, updated_at FROM slot_dnp_legacy")
    op.drop_table("slot_dnp_legacy")
    op.rename_table("interchange_assignment", "interchange_assignment_legacy")
    op.create_table(
        "interchange_assignment",
        sa.Column("competition_key", sa.Text(), nullable=False, server_default="grand_final"),
        sa.Column("team_key", sa.Text(), nullable=False),
        sa.Column("target_position", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("competition_key", "team_key"),
    )
    op.execute(
        "INSERT INTO interchange_assignment SELECT 'grand_final', team_key, target_position, updated_at FROM interchange_assignment_legacy"
    )
    op.drop_table("interchange_assignment_legacy")
    op.rename_table("score_override", "score_override_legacy")
    op.create_table(
        "score_override",
        sa.Column("competition_key", sa.Text(), nullable=False, server_default="grand_final"),
        sa.Column("team_key", sa.Text(), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("override_score", sa.Float()),
        sa.Column("reason", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("competition_key", "team_key", "position"),
    )
    op.execute(
        "INSERT INTO score_override SELECT 'grand_final', team_key, position, override_score, reason, updated_at FROM score_override_legacy"
    )
    op.drop_table("score_override_legacy")
    op.rename_table("matchup_state", "matchup_state_legacy")
    op.create_table(
        "matchup_state",
        sa.Column("competition_key", sa.Text(), nullable=False),
        sa.Column("finalized", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finalized_at", sa.Text()),
        sa.Column("finalized_note", sa.Text()),
        sa.Column("finalized_snapshot", sa.Text()),
        sa.PrimaryKeyConstraint("competition_key"),
    )
    op.execute(
        "INSERT INTO matchup_state SELECT 'grand_final', finalized, finalized_at, finalized_note, finalized_snapshot FROM matchup_state_legacy WHERE id = 1"
    )
    op.drop_table("matchup_state_legacy")


def downgrade():
    bind = op.get_bind()
    for table in TABLES:
        other = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE competition_key <> 'grand_final'")
        ).scalar_one()
        if other:
            raise RuntimeError(
                "0002 downgrade refused: non-grand-final data cannot be represented by the prototype schema"
            )
    # Recreate 0001 and copy only the representable grand_final state.
    for table in TABLES:
        op.rename_table(table, table + "_current")
    upgrade_sql = {
        "slot_dnp": (
            "team_key TEXT NOT NULL, slot TEXT NOT NULL, dnp INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY (team_key, slot)",
            "team_key, slot, dnp, updated_at",
        ),
        "interchange_assignment": (
            "team_key TEXT PRIMARY KEY, target_position TEXT, updated_at TEXT NOT NULL",
            "team_key, target_position, updated_at",
        ),
        "score_override": (
            "team_key TEXT NOT NULL, position TEXT NOT NULL, override_score FLOAT, reason TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (team_key, position)",
            "team_key, position, override_score, reason, updated_at",
        ),
        "matchup_state": (
            "id INTEGER PRIMARY KEY, finalized INTEGER NOT NULL DEFAULT 0, finalized_at TEXT, finalized_note TEXT, finalized_snapshot TEXT",
            "finalized, finalized_at, finalized_note, finalized_snapshot",
        ),
    }
    for table, (ddl, cols) in upgrade_sql.items():
        op.execute(f"CREATE TABLE {table} ({ddl})")
        if table == "matchup_state":
            op.execute(
                f"INSERT INTO {table} (id, {cols}) SELECT 1, {cols} FROM {table}_current WHERE competition_key='grand_final'"
            )
        else:
            op.execute(
                f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}_current WHERE competition_key='grand_final'"
            )
        op.drop_table(table + "_current")
