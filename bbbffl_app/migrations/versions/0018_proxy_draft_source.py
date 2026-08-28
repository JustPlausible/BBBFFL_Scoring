"""Track whether a weekly lineup's current private draft was last written
by an ordinary coach edit or an explicit scorer/admin proxy operation
(roadmap package 22 / issue #55 follow-up).

`LineupProxyService.create_or_amend` (app/lineup_proxy.py) is a privileged
proxy operation over otherwise-anonymous, unattributed private draft
content (`weekly_lineup_draft_slot` carries no per-edit authorship of its
own -- see app/lineups.py's/app/lineup_proxy.py's module docstrings).
Without this column, that intervention could silently disappear from
authoritative history: if the coach subsequently submitted that same
draft content through the ordinary coach `submit()` path, the resulting
submission would be recorded as `source_type="coach"` with no trace that
a proxy actor produced its content.

`weekly_lineup.draft_source` tracks only the *current* draft revision's
origin ('coach' or 'scorer_proxy') -- coarse, whole-draft, not per-position
or per-edit -- deliberately not a general audit trail for every coach
draft edit (issue #55's review asked for the narrowest fix, not a parallel
draft-history mechanism). `WeeklyLineupRepository.submit` (the coach path)
now refuses to submit a draft whose current `draft_source` is
'scorer_proxy' unless submitting with `source_type="scorer_proxy"` -- see
app/lineups.py's `submit`. A coach's own subsequent `save_draft` call
resets `draft_source` back to 'coach', since the coach has then reviewed/
rewritten the draft themselves.

Downgrade needs no refusal-if-populated guard, unlike this codebase's
immutable-submission-history migrations: `draft_source` describes only
current, mutable, pre-submission draft state, never authoritative
submitted history (which remains fully intact via `weekly_lineup_
submission.source_type`/`source_detail` regardless of this column).
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_proxy_draft_source"
down_revision = "0017_preseason"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite can add this constrained, constant-default column in
        # place (same technique as 0009's regular_season_round_count).
        op.execute(
            "ALTER TABLE weekly_lineup ADD COLUMN draft_source TEXT NOT NULL DEFAULT 'coach' "
            "CHECK (draft_source IN ('coach','scorer_proxy'))"
        )
    else:
        op.add_column("weekly_lineup", sa.Column("draft_source", sa.Text(), nullable=False, server_default="coach"))
        op.create_check_constraint(
            "ck_weekly_lineup_draft_source", "weekly_lineup", "draft_source IN ('coach','scorer_proxy')"
        )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE weekly_lineup DROP COLUMN draft_source")
    else:
        op.drop_constraint("ck_weekly_lineup_draft_source", "weekly_lineup", type_="check")
        op.drop_column("weekly_lineup", "draft_source")
