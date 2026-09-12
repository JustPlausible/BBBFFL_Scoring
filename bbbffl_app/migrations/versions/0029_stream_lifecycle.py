"""Loosen the fixture-draw linkage in `bbbffl_round_lifecycle`/`bbbffl_matchup`
for non-ordinary streams (issue #197, a prerequisite for #170's finals/
SuperScore work found by Codex review of PR #196 -- see
docs/2026-finals-superscore-design.md's "A foundational schema fork").

## The problem

`bbbffl_round_lifecycle` (0010_competition_lifecycle.py) declared
`fixture_draw_id`, `fixture_draw_version` and `fixture_round_number` all
`nullable=False`, and `bbbffl_matchup` declared `fixture_matchup_id`
likewise `nullable=False` -- both foreign-keyed to the fixed, pre-drawn
round-robin fixture (`season_fixture_draw`/`season_fixture_matchup`). A
finals or SuperScore round has no fixture-draw row to reference at all
(pairings depend on results, not a pre-drawn fixture; SuperScore has no
pairing at all), so under the pre-#197 schema neither stream could ever
obtain a `bbbffl_round_lifecycle` row -- which blocks lineup submission
itself (`app.lineups.WeeklyLineupRepository._finalize_submission` reads
this table and raises if no row exists), not merely scoring.

## The chosen resolution: Path 1 (loosen the schema), not Path 2 (parallel storage)

Both paths were open per #197. Path 1 is chosen here because it is the
lower-complexity option that fully satisfies every acceptance criterion
without introducing a second lifecycle/matchup storage shape or a
dispatching lookup layer: once a finals/SuperScore round has a
`bbbffl_round_lifecycle` row (with a null fixture context) and, for
finals, a `bbbffl_matchup` row (with a null `fixture_matchup_id`), the
*existing* call sites in `app.lineups`, `app.lineup_adjudication`,
`app.calculations` and `app.round_review` all resolve correctly with zero
code changes -- none of them join against `season_fixture_draw`/
`season_fixture_matchup` at all; they only key off `bbbffl_round_id`/
`matchup_id`. The one place that *does* consult the frozen fixture
context, `CompetitionLifecycleRepository._validate_frozen_context`, is
made stream-aware alongside this migration (see app/competition_lifecycle.py)
rather than requiring a second storage/dispatch layer to reach the same
place. See the PR description and docs/2026-finals-superscore-design.md
for the full reasoning and call-site audit.

## What this migration does, and does not do

Purely additive/backwards-compatible:

- `bbbffl_round_lifecycle.fixture_draw_id`, `.fixture_draw_version` and
  `.fixture_round_number` become nullable. A new check constraint
  (`ck_lifecycle_fixture_context_all_or_none`) requires the three to be
  either all null or all non-null together, so a row can never end up in
  the inconsistent "some but not all populated" state the pre-existing
  application code never anticipated.
- `bbbffl_matchup.fixture_matchup_id` becomes nullable. The pre-existing
  `uq_round_fixture_matchup` unique constraint on `(bbbffl_round_id,
  fixture_matchup_id)` is unaffected: both SQLite and PostgreSQL already
  treat NULL as distinct from any other NULL in a unique constraint, so
  multiple finals matchups in the same round (each with a null
  `fixture_matchup_id`) were never going to collide there.
- No existing row's data changes, no existing constraint is *removed* for
  ordinary rounds (every ordinary row is written by
  `CompetitionLifecycleRepository.create_ordinary_round`, which is
  completely unchanged and still always populates all four columns), and
  no ordinary-round behaviour changes at all.
- This migration does not create any finals/SuperScore round, matchup,
  bracket, or scoring rule -- see `app.competition_lifecycle.
  CompetitionLifecycleRepository.create_non_ordinary_round`/
  `create_stream_matchup` (new in this issue) for the schema-level
  primitives #190/#192 will build finals bracket/SuperScore round
  creation on top of.
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_stream_lifecycle"
down_revision = "0028_finals_seeding"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # On SQLite, batch mode recreates each table (copy, DROP the original,
    # rename the copy into place); bbbffl_matchup/bbbffl_round_upstream_fact
    # hold foreign keys into bbbffl_round_lifecycle.bbbffl_round_id, and
    # bbbffl_matchup_calculation/bbbffl_official_result hold foreign keys
    # into bbbffl_matchup.matchup_id. On any real database that already has
    # persisted rounds/matchups (exactly the populated 2026 replay database
    # this migration exists to carry forward), the DROP step fails FK
    # enforcement unless it is suspended for these statements -- see the
    # matching comment in migrations/versions/0027_midseason_draft.py's
    # season_draft generalisation, and downgrade() below.
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("bbbffl_round_lifecycle") as batch:
        batch.alter_column("fixture_draw_id", nullable=True)
        batch.alter_column("fixture_draw_version", nullable=True)
        batch.alter_column("fixture_round_number", nullable=True)
        batch.create_check_constraint(
            "ck_lifecycle_fixture_context_all_or_none",
            "(fixture_draw_id IS NULL AND fixture_draw_version IS NULL AND fixture_round_number IS NULL) "
            "OR (fixture_draw_id IS NOT NULL AND fixture_draw_version IS NOT NULL AND fixture_round_number IS NOT NULL)",
        )
    with op.batch_alter_table("bbbffl_matchup") as batch:
        batch.alter_column("fixture_matchup_id", nullable=True)
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=ON")


def downgrade():
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM bbbffl_round_lifecycle WHERE fixture_draw_id IS NULL "
            "OR fixture_draw_version IS NULL OR fixture_round_number IS NULL"
        )
    ).scalar_one():
        raise RuntimeError(
            "0029 downgrade refused: one or more bbbffl_round_lifecycle rows have a null fixture context "
            "that the pre-#197 schema cannot represent"
        )
    if bind.execute(sa.text("SELECT COUNT(*) FROM bbbffl_matchup WHERE fixture_matchup_id IS NULL")).scalar_one():
        raise RuntimeError(
            "0029 downgrade refused: one or more bbbffl_matchup rows have a null fixture_matchup_id "
            "that the pre-#197 schema cannot represent"
        )
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("bbbffl_matchup") as batch:
        batch.alter_column("fixture_matchup_id", nullable=False)
    with op.batch_alter_table("bbbffl_round_lifecycle") as batch:
        batch.drop_constraint("ck_lifecycle_fixture_context_all_or_none", type_="check")
        batch.alter_column("fixture_round_number", nullable=False)
        batch.alter_column("fixture_draw_version", nullable=False)
        batch.alter_column("fixture_draw_id", nullable=False)
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=ON")
