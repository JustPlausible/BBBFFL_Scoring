"""Remove the invalid one-nomination-per-(rule, entry) cardinality limit
(issue #135).

Migration 0020 added `uq_opening_round_nomination_rule_entry` --
`UNIQUE(rule_id, season_entry_id)` -- on the mistaken assumption that an
accepted Opening Round rule could ever nominate only one player per BBBFFL
season entry. An accepted rule describes how one AFL club's Opening Round
maps to that club's compensating-bye BBBFFL target round; it says nothing
about how many of that club's *owned* players a coach may nominate into
that target round's distinct lineup slots. The historical issue #135
regression case is a real submission naming two Collingwood players (Jamie
Elliott, Josh Daicos) in distinct Round 2 slots under one rule, and two Gold
Coast players (Noah Anderson, Touk Miller) in distinct Round 3 slots under
another -- both structurally valid and both rejected by the removed
constraint.

The two remaining partial-unique invariants on `opening_round_nomination`
already express the actual domain rules a target-round lineup must satisfy
(mirroring `weekly_lineup_draft_slot`'s "a player cannot occupy multiple
scoring positions" -- see docs/opening-round-deferred-selection.md):

- `uq_opening_round_nomination_slot`
  (`bbbffl_round_id, season_entry_id, position`) -- at most one player per
  target-round slot.
- `uq_opening_round_nomination_player_once`
  (`bbbffl_round_id, season_entry_id, season_player_id`) -- a player
  occupies at most one slot in that entry's target-round lineup.

Neither of those is touched here. This migration removes only the invalid
`uq_opening_round_nomination_rule_entry` constraint, preserving every
existing nomination, rule and confirmation row untouched. `app.opening_round.
OpeningRoundNominationRepository.nominate`'s matching application-layer
pre-check (issue #135) is removed in the same change so the schema and
application layers agree.

`op.batch_alter_table` (not a dialect branch) is used for the same reason
migration 0016 used it for `uq_draft_pick_sequence`: it performs a plain
`ALTER TABLE ... DROP CONSTRAINT` on PostgreSQL and SQLite's copy-and-move
batch rebuild transparently, from one call, preserving every other column,
foreign key, check, remaining unique constraint and index reflected off the
existing table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_opening_round_multi_player"
down_revision = "0023_opening_round_submission"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("opening_round_nomination") as batch:
        batch.drop_constraint("uq_opening_round_nomination_rule_entry", type_="unique")


def downgrade():
    bind = op.get_bind()
    # A downgrade that blindly recreates UNIQUE(rule_id, season_entry_id)
    # would either fail outright (PostgreSQL: ALTER TABLE ... ADD
    # CONSTRAINT against violating rows) or silently succeed while leaving
    # the database unable to represent state the application layer wrote
    # while this constraint was absent (SQLite's batch rebuild would simply
    # raise on the same violating rows). Refusing outright when any such
    # multi-nomination-per-rule data exists follows this repository's
    # existing irreversible-history convention (see e.g. 0009, 0019, 0022,
    # 0023's downgrade refusals) rather than writing a downgrade that is
    # syntactically reversible but unsafe.
    duplicates = bind.execute(
        sa.text("SELECT 1 FROM opening_round_nomination GROUP BY rule_id, season_entry_id HAVING COUNT(*) > 1")
    ).fetchone()
    if duplicates:
        raise RuntimeError(
            "0024 downgrade refused: opening_round_nomination has multiple nominations sharing one "
            "(rule_id, season_entry_id) pair (valid data under issue #135) that "
            "uq_opening_round_nomination_rule_entry cannot represent"
        )
    with op.batch_alter_table("opening_round_nomination") as batch:
        batch.create_unique_constraint("uq_opening_round_nomination_rule_entry", ["rule_id", "season_entry_id"])
