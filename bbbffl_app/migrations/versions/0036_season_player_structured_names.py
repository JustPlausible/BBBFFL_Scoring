"""Preserve afl-api's structured player-name facts on `season_player_pool`
(issue #248).

afl-api's season-scoped canonical player collection (`GET /api/v1/seasons/
{season_id}/players`) now supplies `given_name`/`family_name` alongside
`display_name` (afl-api commit `d21d15a`). These are additive, nullable,
cached AFL facts -- the same authority boundary as the existing
`display_name`/`afl_team_id`/`afl_team_name` columns (see
`docs/player-pool-ownership.md`): BBBFFL is a consumer/cache of afl-api's
player identity, never an independent source of truth for a player's name.

Both columns are nullable with no default and no backfill. Existing rows
predate structured names and remain valid with `NULL` in both columns --
BBBFFL never reconstructs `given_name`/`family_name` by splitting
`display_name`. A later player-pool refresh (`PlayerPoolRepository.
refresh_season_pool`) fills them in from a fresh afl-api response the same
way it already refreshes `display_name` and club facts.

Downgrade drops both columns outright: they carry no BBBFFL-authored state
(cached AFL facts only), so there is nothing here that needs a
data-loss refusal, unlike this codebase's submitted-history migrations.
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_season_player_structured_names"
down_revision = "0035_coach_draft_shortlist"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("season_player_pool", sa.Column("given_name", sa.Text(), nullable=True))
    op.add_column("season_player_pool", sa.Column("family_name", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("season_player_pool", "family_name")
    op.drop_column("season_player_pool", "given_name")
