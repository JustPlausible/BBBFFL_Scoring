"""Audited Scorer/Admin correction of an authoritative weekly lineup after a
selective or main lockout has already activated (issue #137).

This is a narrowly-authorised competition correction, never an ordinary
lineup edit and never a numeric score override. It reuses the existing
immutable `weekly_lineup_submission`/`weekly_lineup_submission_slot` version
chain (0011) exactly as every other submission source does -- a correction
is simply one more submission, tagged `source_type='scorer_correction'`,
that `app.lineups.WeeklyLineupRepository.submit_correction` is permitted to
create for a round in `open`/`live`/`review` state (not just `open`), and
which is exempt from `app.lockouts`' ordinary lock rejection (an authorised
operator, not a coach/proxy, is doing this deliberately). Existing
`weekly_lineup_lock` (0012) evidence is never read for write purposes here --
it is only copied, read-only, into this migration's new tables so the
corrected effective lineup retains defensible provenance tied to the
already-active trigger/lock instant without mutating or reinterpreting the
original immutable evidence.

- `ck_submission_source` (0011) is widened, via `op.batch_alter_table` (the
  same idiom migration 0024 used for a constraint change that must work
  identically on SQLite's copy-rebuild and PostgreSQL's plain
  `ALTER TABLE`), to also accept `'scorer_correction'`.

- `weekly_lineup_correction` is the correction header: which submission
  version this correction superseded (`from_version`) and which new version
  became effective (`to_version`), the authenticated actor/role, the
  represented round/season-entry, the required substantive reason, and the
  timestamp. `to_version` is always `from_version + 1` -- a correction is
  never anything other than the very next submission for its lineup -- and
  `UNIQUE(lineup_id, to_version)` means at most one correction record
  explains any given submission version.

- `weekly_lineup_correction_slot` is the per-position provenance: for every
  position the correction actually changed, the previous and corrected
  player, and -- copied verbatim from `weekly_lineup_lock` at correction
  time, never recomputed later -- whether that position was locked, and if
  so, which trigger/match/instant locked it. This is what answers "which
  already-active trigger/lock instant does the corrected occupant's
  provenance trace back to" without fabricating a new, later lock event or
  touching `weekly_lineup_lock` itself.

Both new tables get the same immutable-history triggers as 0011/0012: once
written, a correction record can never be updated or deleted, only
superseded by a later correction that creates its own new row.
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_lineup_correction"
down_revision = "0024_opening_round_multi_player"
branch_labels = None
depends_on = None

POSITIONS = "'F1','F2','F3','M1','M2','M3','Ruck','Tackler','Interchange'"


def upgrade():
    bind = op.get_bind()
    with op.batch_alter_table("weekly_lineup_submission") as batch:
        batch.drop_constraint("ck_submission_source", type_="check")
        batch.create_check_constraint(
            "ck_submission_source",
            "source_type IN ('coach','scorer_proxy','carry_forward','system_derived','scorer_correction')",
        )
    if bind.dialect.name == "sqlite":
        # SQLite's batch mode rebuilds the table (copy-and-move) to change a
        # CHECK constraint -- there is no in-place ALTER TABLE ... DROP/ADD
        # CONSTRAINT on this backend. That rebuild only recreates what
        # SQLAlchemy's reflected metadata describes (columns, FKs, checks,
        # indexes); it does not know about 0011's hand-written
        # `weekly_lineup_submission_no_update`/`_no_delete` triggers, which
        # are silently dropped along with the old table. PostgreSQL's
        # `ALTER TABLE ... DROP/ADD CONSTRAINT` never rebuilds the table, so
        # its trigger is untouched and this recreation is SQLite-only.
        op.execute(
            "CREATE TRIGGER weekly_lineup_submission_no_update BEFORE UPDATE ON weekly_lineup_submission "
            "BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER weekly_lineup_submission_no_delete BEFORE DELETE ON weekly_lineup_submission "
            "BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
        )

    op.create_table(
        "weekly_lineup_correction",
        sa.Column("correction_id", sa.Text(), primary_key=True),
        sa.Column(
            "lineup_id", sa.Text(), sa.ForeignKey("weekly_lineup.lineup_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("actor_role", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["lineup_id", "from_version"],
            ["weekly_lineup_submission.lineup_id", "weekly_lineup_submission.version"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lineup_id", "to_version"],
            ["weekly_lineup_submission.lineup_id", "weekly_lineup_submission.version"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("lineup_id", "to_version", name="uq_correction_to_version"),
        sa.CheckConstraint("from_version >= 1", name="ck_correction_from_version"),
        sa.CheckConstraint("to_version = from_version + 1", name="ck_correction_sequential"),
    )
    op.create_index("ix_lineup_correction_lineup", "weekly_lineup_correction", ["lineup_id"])
    op.create_index("ix_lineup_correction_round", "weekly_lineup_correction", ["bbbffl_round_id"])

    op.create_table(
        "weekly_lineup_correction_slot",
        sa.Column(
            "correction_id",
            sa.Text(),
            sa.ForeignKey("weekly_lineup_correction.correction_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column(
            "previous_season_player_id",
            sa.Text(),
            sa.ForeignKey("season_player_pool.season_player_id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "corrected_season_player_id",
            sa.Text(),
            sa.ForeignKey("season_player_pool.season_player_id", ondelete="RESTRICT"),
        ),
        # Integer(0/1), not Boolean -- matches this codebase's existing
        # cross-dialect raw '?'-param convention (see 0019_round_review.py).
        sa.Column("was_locked", sa.Integer(), nullable=False),
        sa.Column("lock_reason", sa.Text()),
        sa.Column("afl_match_id", sa.Integer()),
        sa.Column("effective_lock_at", sa.Text()),
        sa.Column("observed_status", sa.Text()),
        sa.Column("locked_at", sa.Text()),
        sa.PrimaryKeyConstraint("correction_id", "position"),
        sa.CheckConstraint(f"position IN ({POSITIONS})", name="ck_correction_slot_position"),
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in ("weekly_lineup_correction", "weekly_lineup_correction_slot"):
            op.execute(
                f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'lineup correction history is immutable'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'lineup correction history is immutable'); END"
            )
    elif bind.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_lineup_correction_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'lineup correction history is immutable'; END; $$ LANGUAGE plpgsql"
        )
        for table in ("weekly_lineup_correction", "weekly_lineup_correction_slot"):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION reject_lineup_correction_mutation()"
            )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM weekly_lineup_correction")).scalar_one():
        raise RuntimeError("0025 downgrade refused: audited lineup correction history would be lost")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_lineup_correction_mutation() CASCADE")
    op.drop_table("weekly_lineup_correction_slot")
    op.drop_table("weekly_lineup_correction")
    with op.batch_alter_table("weekly_lineup_submission") as batch:
        batch.drop_constraint("ck_submission_source", type_="check")
        batch.create_check_constraint(
            "ck_submission_source", "source_type IN ('coach','scorer_proxy','carry_forward','system_derived')"
        )
    if bind.dialect.name == "sqlite":
        # See upgrade()'s matching comment: SQLite's batch rebuild drops
        # 0011's hand-written immutability triggers, so they must be
        # recreated again here too.
        op.execute(
            "CREATE TRIGGER weekly_lineup_submission_no_update BEFORE UPDATE ON weekly_lineup_submission "
            "BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER weekly_lineup_submission_no_delete BEFORE DELETE ON weekly_lineup_submission "
            "BEGIN SELECT RAISE(ABORT, 'submitted lineups are immutable'); END"
        )
