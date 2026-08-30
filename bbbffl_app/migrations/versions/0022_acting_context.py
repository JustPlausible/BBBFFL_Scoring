"""Role grants and active operating context (issue #107).

Introduces the shared multi-role / acting-context model: one authenticated
coach identity (`coach.coach_id`, roadmap package 19/issue #74) may hold
zero or more *additional* authority roles -- Scorer, Secretary/League
Manager, Administrator, Replay Operator -- on top of the "Coach" authority
they already have implicitly through `season_entry_coach_history`. See
docs/acting-context.md.

Two changes, both additive and both scoped to the existing coach identity
(no second user/identity model, matching every other package in this
codebase):

- **`role_grant`** -- one row per granted role. `coach_id` is the
  authenticated person the role is granted to. `role` is one of
  `GRANTABLE_ROLES` (`app.auth`): `scorer`, `secretary`, `admin`,
  `replay_operator` -- deliberately never `coach` or `spectator`, which are
  not grantable (Coach authority comes from season-entry assignment,
  spectator from having no session at all). `season_id`, when set, scopes
  the grant to season entries within that one season only -- this is how a
  Replay Operator grant is restricted to the ten 2026 replay season
  entries without ever reaching a live/current season; a `NULL` season_id
  grants the role across every season (the ordinary case for Scorer/
  Secretary/Admin). `revoked_at` supports revocation without deleting the
  row (matching `coach_session.revoked_at`'s convention), so a revoked
  grant's audit history remains inspectable.
- **`coach_session.active_role`/`coach_session.represented_season_entry_id`**
  -- server-authoritative active-context state, added to the same session
  row roadmap package 19 already made the one trusted per-request boundary.
  Both are nullable: `active_role IS NULL` means "Coach" (the pre-existing,
  always-available default -- see `app.authorization.resolve_principal`),
  and `represented_season_entry_id` is only meaningful once a delegated
  role is active. Neither column is trusted at face value on every
  request: `app.authorization.resolve_principal` re-validates both against
  `role_grant` on each resolution, so a grant revoked mid-session cannot
  keep conferring authority merely because the session row still names it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_acting_context"
down_revision = "0021_coach_authentication"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "role_grant",
        sa.Column("grant_id", sa.Text(), primary_key=True),
        sa.Column("coach_id", sa.Text(), sa.ForeignKey("coach.coach_id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="CASCADE")),
        sa.Column("granted_at", sa.Text(), nullable=False),
        sa.Column("granted_by_actor_type", sa.Text(), nullable=False),
        sa.Column("granted_by_actor_id", sa.Text()),
        sa.Column("revoked_at", sa.Text()),
        sa.Column("reason", sa.Text()),
    )
    op.create_index("ix_role_grant_coach", "role_grant", ["coach_id"])
    op.create_index("ix_role_grant_season", "role_grant", ["season_id"])

    op.add_column("coach_session", sa.Column("active_role", sa.Text()))
    # SQLite cannot ALTER a table to add a column carrying a foreign-key
    # constraint directly (it has no ALTER TABLE ... ADD CONSTRAINT); batch
    # mode does the copy-and-move Alembic needs for that dialect, the same
    # pattern migration 0016 already uses for `draft_pick.
    # superseded_by_draft_pick_id`.
    with op.batch_alter_table("coach_session") as batch:
        batch.add_column(
            sa.Column(
                "represented_season_entry_id",
                sa.Text(),
                sa.ForeignKey(
                    "season_entry.season_entry_id",
                    ondelete="SET NULL",
                    name="fk_coach_session_represented_entry",
                ),
            )
        )


def downgrade():
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT COUNT(*) FROM role_grant")).scalar_one()
    if count:
        raise RuntimeError("0022 downgrade refused: role_grant holds data the prior schema cannot represent")
    op.drop_index("ix_role_grant_season", table_name="role_grant")
    op.drop_index("ix_role_grant_coach", table_name="role_grant")
    op.drop_table("role_grant")
    with op.batch_alter_table("coach_session") as batch:
        batch.drop_column("represented_season_entry_id")
        batch.drop_column("active_role")
