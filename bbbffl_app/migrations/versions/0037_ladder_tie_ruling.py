"""Issue #241: an audited, persisted manual resolution for an exact
mathematical ladder tie that the configured ladder criteria (competition
points, percentage, points for -- `app.ladder`'s module docstring) cannot
separate.

## Why this exists

`app.ladder.calculate_ladder` deliberately never invents a fourth
tiebreaking criterion: an exact tie is reported as `LadderRow.tied` with a
`tie_group`, and `season_entry_id` ordering inside that group exists only
for repeatable *serialization*, never as a real decision
(`app.finals_seeding.UnresolvedLadderTieError`/`app.season_awards.
UnresolvedWoodenSpoonTieError` both fail closed on it today, with no
recorded-resolution path). This table is that path: a Scorer/Administrator
records the decided relative order for one exact tie group, with a
mandatory reason, and the ruling is reused by every consumer that needs a
deterministic order over that same tie -- Finals seeding
(`app.finals`/`app.finals_seeding`) and season awards
(`app.season_awards`'s wooden spoon) alike -- rather than each maintaining
its own ad hoc override.

## Why this shape

Mirrors `season_award` (migration `0033_season_award.py`): one active row
per governed fact at a time (`uq_ladder_tie_ruling_active_key`, a partial
unique index on `status='active'`), a later ruling superseding rather than
overwriting the previous one (`superseded_by_ruling_id`), full history
always queryable. Unlike `season_award`, the "governed fact" here is not a
fixed `(season_id, award_type)` pair -- it is one exact tie group, which can
recur at any rank band and is identified by `tie_group_key` (a canonical,
sorted, comma-joined `season_entry_id` list) scoped to
`(season_id, competition_id, through_round)`. `decided_order` (JSON) is the
mandatory decision itself: `tie_group`'s members, reordered best-to-worst,
matching the ladder's own overall best-to-worst convention -- so consuming
code can substitute it directly into the ladder's own row order.

`result_references` (JSON, the exact `(matchup_id, official_version)` set
`app.ladder.LadderSnapshot.result_references` reports for the ladder this
ruling was recorded against) is this table's staleness fingerprint --
exactly the same "freeze the exact result-version set, compare it verbatim
later" technique `finals_bracket_result_reference`/
`finals_seeding_snapshot_reference` already use (migrations
`0030_finals_bracket.py`/`0028_finals_seeding.py`). Any correction to any
official result feeding that ladder changes this set, so a resolver comparing
it verbatim can detect a stale ruling without a second bespoke mechanism.
`tie_group_key`'s own exact-match lookup already handles the narrower case
where a correction changes *which* entries are tied (a different key simply
finds no ruling); the reference-set comparison additionally catches a
correction that leaves the same entries tied but on now-different underlying
facts, which the requesting issue (#241) calls out as needing its own
detection.

Never a general-purpose ladder editor: `app.ladder_tie_ruling.record_ruling`
requires the ladder it just recomputed under lock to actually report a tied
group whose member set equals the caller's `decided_order` -- there is no
caller-supplied `tie_group` column to spoof, and no code path to record a
ruling for entries that are not, right now, exactly tied.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_ladder_tie_ruling"
down_revision = "0036_player_structured_names"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ladder_tie_ruling",
        sa.Column("ruling_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "competition_id",
            sa.Text(),
            sa.ForeignKey("competition_stream.competition_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("through_round", sa.Integer(), nullable=False),
        sa.Column("tie_group_key", sa.Text(), nullable=False),
        sa.Column("tie_group", sa.Text(), nullable=False),
        sa.Column("decided_order", sa.Text(), nullable=False),
        sa.Column("result_references", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "superseded_by_ruling_id",
            sa.Text(),
            sa.ForeignKey("ladder_tie_ruling.ruling_id", ondelete="RESTRICT"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'superseded')", name="ck_ladder_tie_ruling_status"),
        sa.CheckConstraint("through_round > 0", name="ck_ladder_tie_ruling_through_round_positive"),
    )
    op.create_index(
        "uq_ladder_tie_ruling_active_key",
        "ladder_tie_ruling",
        ["season_id", "competition_id", "through_round", "tie_group_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_ladder_tie_ruling_season", "ladder_tie_ruling", ["season_id"])


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM ladder_tie_ruling")).scalar_one():
        raise RuntimeError("0037 downgrade refused: ladder tie ruling history would be lost")
    op.drop_table("ladder_tie_ruling")
