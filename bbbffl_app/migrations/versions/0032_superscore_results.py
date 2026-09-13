"""Entry calculations and append-only whole-leaderboard SuperScore results."""

import sqlalchemy as sa
from alembic import op

revision = "0032_superscore_results"
down_revision = "0031_superscore_round"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "superscore_entry_calculation",
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.Text(), nullable=False),
        sa.Column("computed_as_of_review_version", sa.Integer(), nullable=False),
        sa.Column("total_score", sa.Numeric(12, 3), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("bbbffl_round_id", "season_entry_id"),
        sa.CheckConstraint("revision >= 1", name="ck_superscore_calculation_revision"),
    )
    op.create_table(
        "superscore_leaderboard_revision",
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.Text(), nullable=False),
        sa.Column("published_by_type", sa.Text(), nullable=False),
        sa.Column("published_by", sa.Text()),
        sa.Column("published_by_role", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.PrimaryKeyConstraint("bbbffl_round_id", "version"),
    )
    op.create_table(
        "superscore_official_result",
        sa.Column("bbbffl_round_id", sa.Text(), nullable=False),
        sa.Column("leaderboard_version", sa.Integer(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("total_score", sa.Numeric(12, 3), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("is_joint_winner", sa.Integer(), nullable=False),
        sa.Column("input_snapshot", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bbbffl_round_id", "leaderboard_version"],
            ["superscore_leaderboard_revision.bbbffl_round_id", "superscore_leaderboard_revision.version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("bbbffl_round_id", "leaderboard_version", "season_entry_id"),
        sa.CheckConstraint("rank >= 1", name="ck_superscore_result_rank"),
        sa.CheckConstraint("is_joint_winner IN (0,1)", name="ck_superscore_joint_winner"),
    )
    bind = op.get_bind()
    publication_tables = ("superscore_leaderboard_revision", "superscore_official_result")
    if bind.dialect.name == "sqlite":
        for table in publication_tables:
            op.execute(
                f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} history is immutable'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} history is immutable'); END"
            )
    else:
        op.execute(
            "CREATE FUNCTION reject_immutable_superscore_result_change() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'SuperScore publication history is immutable'; END; $$ LANGUAGE plpgsql"
        )
        for table in publication_tables:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_immutable_superscore_result_change()"
            )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM superscore_leaderboard_revision")).scalar_one():
        raise RuntimeError("0032 downgrade refused: published SuperScore history would be lost")
    publication_tables = ("superscore_official_result", "superscore_leaderboard_revision")
    if bind.dialect.name == "sqlite":
        for table in publication_tables:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_update")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_delete")
    else:
        for table in publication_tables:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.execute("DROP FUNCTION reject_immutable_superscore_result_change() CASCADE")
    op.drop_table("superscore_official_result")
    op.drop_table("superscore_leaderboard_revision")
    op.drop_table("superscore_entry_calculation")
