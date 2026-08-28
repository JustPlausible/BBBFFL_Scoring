"""Authoritative season draft order, stable picks and transfer history."""

import sqlalchemy as sa
from alembic import op

revision = "0015_draft"
down_revision = "0014_scoring"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "season_draft",
        sa.Column("draft_id", sa.Text(), primary_key=True),
        sa.Column("season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("target_squad_size", sa.Integer(), nullable=False),
        sa.Column("accepted_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("season_id", name="uq_draft_season"),
        sa.CheckConstraint("target_squad_size > 0", name="ck_draft_target_positive"),
    )
    op.create_table(
        "draft_order_position",
        sa.Column("draft_id", sa.Text(), sa.ForeignKey("season_draft.draft_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("draft_id", "position"),
        sa.UniqueConstraint("draft_id", "season_entry_id", name="uq_draft_order_entry"),
        sa.ForeignKeyConstraint(["season_entry_id", "season_id"], ["season_entry.season_entry_id", "season_entry.season_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("position > 0", name="ck_draft_order_position_positive"),
    )
    op.create_table(
        "draft_pick",
        sa.Column("draft_pick_id", sa.Text(), primary_key=True),
        sa.Column("draft_id", sa.Text(), sa.ForeignKey("season_draft.draft_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("overall_number", sa.Integer(), nullable=False),
        sa.Column("draft_round", sa.Integer(), nullable=False),
        sa.Column("round_position", sa.Integer(), nullable=False),
        sa.Column("original_season_entry_id", sa.Text(), nullable=False),
        sa.Column("current_season_entry_id", sa.Text(), nullable=False),
        sa.Column("selected_season_player_id", sa.Text()),
        sa.Column("completed_at", sa.Text()),
        sa.UniqueConstraint("draft_id", "overall_number", name="uq_draft_pick_sequence"),
        sa.ForeignKeyConstraint(["original_season_entry_id", "season_id"], ["season_entry.season_entry_id", "season_entry.season_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["current_season_entry_id", "season_id"], ["season_entry.season_entry_id", "season_entry.season_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["selected_season_player_id", "season_id"], ["season_player_pool.season_player_id", "season_player_pool.season_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("overall_number > 0 AND draft_round > 0 AND round_position > 0", name="ck_draft_pick_numbers_positive"),
        sa.CheckConstraint("(selected_season_player_id IS NULL) = (completed_at IS NULL)", name="ck_draft_pick_completion"),
    )
    op.create_table(
        "draft_pick_transfer",
        sa.Column("transfer_id", sa.Text(), primary_key=True),
        sa.Column("draft_pick_id", sa.Text(), sa.ForeignKey("draft_pick.draft_pick_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("from_season_entry_id", sa.Text(), sa.ForeignKey("season_entry.season_entry_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("to_season_entry_id", sa.Text(), sa.ForeignKey("season_entry.season_entry_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("transferred_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("audit_event_id", sa.Text(), sa.ForeignKey("audit_event.event_id", ondelete="RESTRICT"), nullable=False),
        sa.CheckConstraint("from_season_entry_id <> to_season_entry_id", name="ck_pick_transfer_changes_owner"),
    )
    op.create_index("ix_draft_pick_next", "draft_pick", ["draft_id", "completed_at", "overall_number"])

    # Accepted order/allocation and completed results have no ordinary rewrite path.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("CREATE TRIGGER draft_order_immutable_update BEFORE UPDATE ON draft_order_position BEGIN SELECT RAISE(ABORT, 'accepted draft order is immutable'); END")
        op.execute("CREATE TRIGGER draft_order_immutable_delete BEFORE DELETE ON draft_order_position BEGIN SELECT RAISE(ABORT, 'accepted draft order is immutable'); END")
        op.execute("CREATE TRIGGER completed_pick_immutable BEFORE UPDATE OF selected_season_player_id, completed_at ON draft_pick WHEN OLD.completed_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'completed draft pick is immutable'); END")
    else:
        op.execute("""
        CREATE FUNCTION reject_immutable_draft_change() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'accepted/completed draft history is immutable'; END; $$ LANGUAGE plpgsql
        """)
        op.execute("CREATE TRIGGER draft_order_immutable_update BEFORE UPDATE OR DELETE ON draft_order_position FOR EACH ROW EXECUTE FUNCTION reject_immutable_draft_change()")
        op.execute("CREATE TRIGGER completed_pick_immutable BEFORE UPDATE OF selected_season_player_id, completed_at ON draft_pick FOR EACH ROW WHEN (OLD.completed_at IS NOT NULL) EXECUTE FUNCTION reject_immutable_draft_change()")


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM season_draft")).scalar_one():
        raise RuntimeError("0015 downgrade refused: draft history would be lost")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_immutable_draft_change() CASCADE")
    for table in ("draft_pick_transfer", "draft_pick", "draft_order_position", "season_draft"):
        op.drop_table(table)
