"""Provisional player creation and canonical afl-api reconciliation (issue #242).

## Why this exists

A legitimate AFL player sometimes needs to participate in the BBBFFL draft
before `afl-api` represents them (a genuine rookie/mid-season recruit whose
upstream identity has not yet been published). Before this revision,
`season_player_pool.canonical_player_id` was `NOT NULL` and constrained
positive (migration `0006_player_pool_ownership`), so BBBFFL had no way to
represent such a player without inventing a fake canonical id -- exactly the
gap `docs/2027-live-season-readiness.md` records as outstanding item 9.

## What changes

`season_player_id` (this table's existing primary key -- see
`docs/player-pool-ownership.md`) was already BBBFFL's stable internal player
identity, distinct from `canonical_player_id`, the cached afl-api
association. This revision does not introduce a second identity concept; it
only lets that existing association be temporarily absent:

- `canonical_player_id` becomes nullable, with the positive-or-null check
  `ck_pool_canonical_player_positive_or_null` replacing the old
  `ck_pool_canonical_player_positive`. `uq_pool_season_canonical_player`
  (season_id, canonical_player_id) is unchanged and, by ordinary SQL NULL
  semantics on both SQLite and PostgreSQL, does not treat two provisional
  rows' NULLs as a duplicate.
- `was_provisional` (never reset once set) and `provisional_note` (the
  reason/source recorded at creation, retained permanently -- "do not
  destroy the fact that the player previously existed provisionally") make
  a player's provisional origin visible even after reconciliation attaches
  a canonical id. `provisional_reconciled_at` records when that happened.
  A player is *currently* provisional exactly when `canonical_player_id IS
  NULL` -- no separate "is provisional" flag exists, so there is nothing
  that can drift out of sync with it.

Two new tables support the surrounding workflow, both scoped to
`app.provisional_players` (never read back to compute current player-pool
state -- domain tables, here `season_player_pool` itself, remain that):

- `player_nomination` -- a Coach's report that a legitimate AFL player is
  missing from the pool (issue #242's role boundary: a Coach may nominate,
  never create the provisional identity directly). `resulting_season_player_id`
  links a nomination to the provisional player a Scorer/Administrator later
  created from it.
- `provisional_match_candidate` -- a plausible canonical match detected the
  next time the season's player pool is refreshed from afl-api (see
  `app.player_pool.PlayerPoolRepository.refresh_season_pool`), never an
  automatic reconciliation. Its composite FK to
  `season_player_pool(season_id, canonical_player_id)` cascades: when
  reconciliation retires the duplicate canonical pool row it was suggesting
  (see `app.provisional_players.reconcile`), every candidate row naming that
  canonical id is removed with it, for every provisional player that may
  have been offered it -- no stale suggestion can outlive the pool row it
  described.
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_provisional_players"
down_revision = "0037_ladder_tie_ruling"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # On SQLite, batch mode recreates the table (copy, DROP the original,
    # rename the copy into place); `season_player_pool` is the parent of a
    # great many foreign keys (ownership, draft picks, weekly lineup slots,
    # lockouts, shortlists, ...). On any real database that already has
    # persisted players/ownership/selections, the DROP step fails FK
    # enforcement unless it is suspended for these statements -- see the
    # identical pattern in migrations/versions/0029_stream_lifecycle.py.
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("season_player_pool") as batch:
        batch.drop_constraint("ck_pool_canonical_player_positive", type_="check")
        batch.alter_column("canonical_player_id", existing_type=sa.Integer(), nullable=True)
        batch.create_check_constraint(
            "ck_pool_canonical_player_positive_or_null",
            "canonical_player_id IS NULL OR canonical_player_id > 0",
        )
        batch.add_column(sa.Column("was_provisional", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("provisional_note", sa.Text(), nullable=True))
        batch.add_column(sa.Column("provisional_reconciled_at", sa.Text(), nullable=True))
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=ON")

    op.create_table(
        "player_nomination",
        sa.Column("nomination_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("player_name", sa.Text(), nullable=False),
        sa.Column("afl_club_note", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.Text()),
        sa.Column("resulting_season_player_id", sa.Text()),
        sa.ForeignKeyConstraint(
            ["season_entry_id", "season_id"],
            ["season_entry.season_entry_id", "season_entry.season_id"],
            ondelete="RESTRICT",
            name="fk_nomination_entry_same_season",
        ),
        sa.ForeignKeyConstraint(
            ["resulting_season_player_id", "season_id"],
            ["season_player_pool.season_player_id", "season_player_pool.season_id"],
            ondelete="RESTRICT",
            name="fk_nomination_result_same_season",
        ),
        sa.CheckConstraint("status IN ('pending', 'created', 'dismissed')", name="ck_nomination_status"),
        sa.CheckConstraint("length(trim(player_name)) > 0", name="ck_nomination_player_name_nonempty"),
    )
    op.create_index("ix_nomination_season_status", "player_nomination", ["season_id", "status"])
    op.create_index("ix_nomination_entry", "player_nomination", ["season_entry_id"])

    op.create_table(
        "provisional_match_candidate",
        sa.Column("candidate_id", sa.Text(), primary_key=True),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("season_player_id", sa.Text(), nullable=False),
        sa.Column("canonical_player_id", sa.Integer(), nullable=False),
        sa.Column("match_basis", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("detected_at", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Text()),
        sa.ForeignKeyConstraint(
            ["season_player_id", "season_id"],
            ["season_player_pool.season_player_id", "season_player_pool.season_id"],
            ondelete="RESTRICT",
            name="fk_candidate_provisional_same_season",
        ),
        sa.ForeignKeyConstraint(
            ["season_id", "canonical_player_id"],
            ["season_player_pool.season_id", "season_player_pool.canonical_player_id"],
            ondelete="CASCADE",
            name="fk_candidate_target_same_season",
        ),
        sa.CheckConstraint("status IN ('pending', 'rejected')", name="ck_candidate_status"),
        sa.CheckConstraint("canonical_player_id > 0", name="ck_candidate_canonical_positive"),
        sa.UniqueConstraint("season_player_id", "canonical_player_id", name="uq_candidate_provisional_canonical"),
    )
    op.create_index("ix_candidate_season_status", "provisional_match_candidate", ["season_id", "status"])


def downgrade():
    bind = op.get_bind()
    for table in ("provisional_match_candidate", "player_nomination"):
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError(f"0038 downgrade refused: {table} history would be lost")
        op.drop_table(table)

    provisional_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM season_player_pool WHERE canonical_player_id IS NULL OR was_provisional")
    ).scalar_one()
    if provisional_count:
        raise RuntimeError(
            "0038 downgrade refused: provisional player data cannot be represented by the prior schema "
            "(a positive, non-null canonical_player_id was required)"
        )
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("season_player_pool") as batch:
        batch.drop_column("provisional_reconciled_at")
        batch.drop_column("provisional_note")
        batch.drop_column("was_provisional")
        batch.drop_constraint("ck_pool_canonical_player_positive_or_null", type_="check")
        batch.alter_column("canonical_player_id", existing_type=sa.Integer(), nullable=False)
        batch.create_check_constraint("ck_pool_canonical_player_positive", "canonical_player_id > 0")
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=ON")
