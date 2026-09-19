"""Private coach draft shortlist/planning list (issue #181).

A coach-private, ordered list of preferred players a team may maintain
before and during either draft type. Deliberately minimal: this table
records *preference order* only. It never reserves a player, never
changes ownership, and is never itself consulted by
`app.draft.DraftRepository`/`app.midseason_draft.MidseasonDraftRepository`
when validating or executing a selection -- those repositories remain the
sole source of truth for turn/eligibility/ownership/capacity, exactly as
issue #181 requires. `app.shortlist.ShortlistRepository` is the only
writer.

One row per `(season_entry_id, season_player_id)`; `rank` is a dense,
1-based ordering unique per `season_entry_id` so "the next still-available
preference" is a simple `ORDER BY rank` scan filtered against live
`player_ownership_period` state at read time -- there is no cached
availability column to go stale.

Scoped to `season_entry_id` (not to a specific draft/kind): the same
preference list is meaningful before/during the preseason draft and again
before/during a later mid-season draft in the same season -- see the
module docstring in `app.shortlist`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_coach_draft_shortlist"
down_revision = "0034_lockout_trigger_removal"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "coach_draft_shortlist",
        sa.Column("shortlist_item_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_entry_id",
            sa.Text(),
            sa.ForeignKey("season_entry.season_entry_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "season_player_id",
            sa.Text(),
            sa.ForeignKey("season_player_pool.season_player_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("season_entry_id", "season_player_id", name="uq_shortlist_entry_player"),
        sa.UniqueConstraint("season_entry_id", "rank", name="uq_shortlist_entry_rank"),
        sa.CheckConstraint("rank > 0", name="ck_shortlist_rank_positive"),
    )
    op.create_index("ix_shortlist_entry", "coach_draft_shortlist", ["season_entry_id"])


def downgrade():
    op.drop_index("ix_shortlist_entry", table_name="coach_draft_shortlist")
    op.drop_table("coach_draft_shortlist")
