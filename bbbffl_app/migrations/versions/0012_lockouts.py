"""Persist irreversible player-level AFL-match lockout evidence."""

from alembic import op
import sqlalchemy as sa

revision = "0012_lockouts"
down_revision = "0011_lineups"
branch_labels = None
depends_on = None

POSITIONS = "'F1','F2','F3','M1','M2','M3','Ruck','Tackler','Interchange'"


def upgrade():
    op.create_table(
        "weekly_lineup_lock",
        sa.Column("lineup_id", sa.Text(), sa.ForeignKey("weekly_lineup.lineup_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("season_player_id", sa.Text(), sa.ForeignKey("season_player_pool.season_player_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("afl_match_id", sa.Integer(), nullable=False),
        sa.Column("observed_status", sa.Text(), nullable=False),
        sa.Column("effective_lock_at", sa.Text()),
        sa.Column("lock_reason", sa.Text(), nullable=False),
        sa.Column("locked_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("lineup_id", "position"),
        sa.CheckConstraint(f"position IN ({POSITIONS})", name="ck_lock_position"),
    )
    # Irreversible by design: once a position's commencement lock has been
    # observed and recorded, a later upstream schedule/status correction
    # must not be able to unlock it (see app/lockouts.py's module
    # docstring). Mirrors 0011's weekly_lineup_submission immutability.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("CREATE TRIGGER weekly_lineup_lock_no_update BEFORE UPDATE ON weekly_lineup_lock BEGIN SELECT RAISE(ABORT, 'lock evidence is immutable'); END")
        op.execute("CREATE TRIGGER weekly_lineup_lock_no_delete BEFORE DELETE ON weekly_lineup_lock BEGIN SELECT RAISE(ABORT, 'lock evidence is immutable'); END")
    elif bind.dialect.name == "postgresql":
        op.execute("CREATE FUNCTION reject_lineup_lock_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'lock evidence is immutable'; END; $$ LANGUAGE plpgsql")
        op.execute("CREATE TRIGGER weekly_lineup_lock_immutable BEFORE UPDATE OR DELETE ON weekly_lineup_lock FOR EACH ROW EXECUTE FUNCTION reject_lineup_lock_mutation()")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM weekly_lineup_lock")).scalar_one():
        raise RuntimeError("0012 downgrade refused: irreversible lock evidence would be lost")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_lineup_lock_mutation() CASCADE")
    op.drop_table("weekly_lineup_lock")
