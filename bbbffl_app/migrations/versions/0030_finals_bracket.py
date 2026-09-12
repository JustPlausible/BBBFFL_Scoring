"""Finals bracket persistence: one bracket per `(season_id, competition_id)`,
its frozen seed order/provenance, per-week pairings and elimination history
(issue #190, the first of six #170 follow-ups; depends on #197's
`create_non_ordinary_round`/`create_stream_matchup` primitives).

## Why this shape

`docs/2026-finals-superscore-design.md`'s "Match generation/representation"
section and issue #190's body/Steve-confirmed policy comment are the
authoritative source for the rules this schema exists to support:

- The seed order is resolved **once**, at bracket creation, from either an
  existing `finals_seeding_snapshot` or a single locked-and-recompared
  `LadderRepository.snapshot` read -- never re-resolved afterwards. Every
  later decision (Week 1 pairing, a tie-break, an audit payload) reads the
  frozen `finals_bracket_seed`/`finals_bracket_result_reference` rows this
  migration adds, never a fresh call into `app.ladder`/`app.finals_seeding`.
- A finals week has a *variable* match count (0 matches + a bye for seed 1
  in Week 1; two matches in Weeks 1-2; one match in Weeks 3-4) -- the exact
  reason a fixed five-matchup fixture (`season_fixture_matchup`) cannot
  represent it. `finals_bracket_pairing` therefore has no fixed row count
  per week, and `matchup_id` is nullable (null only for the Week 1 bye,
  which is not a match at all).
- **History is append-only, never destructively overwritten** (Steve's
  confirmed correction/rewind policy): a correction to a prerequisite
  finals result that changes an already-derived downstream pairing or
  elimination never updates the old row in place. It inserts a new
  `status='active'` row and flips the old row to `status='superseded'`,
  linked by `superseded_by_pairing_id`/`superseded_by_elimination_id`. The
  partial unique indexes below (`status='active'` only) are what let
  "supersede" and "the current one" coexist safely: at most one *active*
  row per `(bracket_id, week_number, slot)` or `(bracket_id,
  season_entry_id)` at a time, but the full history remains queryable.
- `source_matchup_id_1`/`source_matchup_id_2` (each paired with its own
  captured `source_official_version`) record provenance for a derived
  pairing. Two columns, not one, because a derived pairing can depend on
  two different prerequisite matches at once (e.g. the First Semi-Final is
  Qualifying-Final-loser vs Elimination-Final-winner -- both Week 1
  matches feed it) -- see `app.finals.advance_bracket`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_finals_bracket"
down_revision = "0029_stream_lifecycle"
branch_labels = None
depends_on = None

_APPEND_ONLY_TABLES = (
    "finals_bracket_seed",
    "finals_bracket_result_reference",
    "finals_bracket_week",
)


def upgrade():
    bind = op.get_bind()

    op.create_table(
        "finals_bracket",
        sa.Column("bracket_id", sa.Text(), primary_key=True),
        sa.Column(
            "season_id", sa.Text(), sa.ForeignKey("bbbffl_season.season_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "competition_id",
            sa.Text(),
            sa.ForeignKey("competition_stream.competition_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "ordinary_competition_id",
            sa.Text(),
            sa.ForeignKey("competition_stream.competition_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("seed_source", sa.Text(), nullable=False),
        sa.Column(
            "finals_seeding_snapshot_id",
            sa.Text(),
            sa.ForeignKey("finals_seeding_snapshot.snapshot_id", ondelete="RESTRICT"),
        ),
        sa.Column("through_round", sa.Integer()),
        sa.Column("latest_included_round", sa.Integer()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("season_id", "competition_id", name="uq_finals_bracket_season_competition"),
        sa.CheckConstraint("seed_source IN ('snapshot', 'ladder')", name="ck_finals_bracket_seed_source"),
        sa.CheckConstraint(
            "(seed_source = 'snapshot' AND finals_seeding_snapshot_id IS NOT NULL AND through_round IS NULL) OR "
            "(seed_source = 'ladder' AND finals_seeding_snapshot_id IS NULL AND through_round IS NOT NULL)",
            name="ck_finals_bracket_seed_provenance_shape",
        ),
    )

    op.create_table(
        "finals_bracket_seed",
        sa.Column(
            "bracket_id", sa.Text(), sa.ForeignKey("finals_bracket.bracket_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("seed_position", sa.Integer(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column("qualified", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("bracket_id", "seed_position"),
        sa.UniqueConstraint("bracket_id", "season_entry_id", name="uq_finals_bracket_seed_entry"),
        sa.CheckConstraint("seed_position BETWEEN 1 AND 10", name="ck_finals_bracket_seed_position_range"),
    )

    op.create_table(
        "finals_bracket_result_reference",
        sa.Column("reference_id", sa.Text(), primary_key=True),
        sa.Column(
            "bracket_id", sa.Text(), sa.ForeignKey("finals_bracket.bracket_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("matchup_id", sa.Text(), nullable=False),
        sa.Column("official_version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_finals_bracket_result_reference_bracket", "finals_bracket_result_reference", ["bracket_id"])

    op.create_table(
        "finals_bracket_week",
        sa.Column(
            "bracket_id", sa.Text(), sa.ForeignKey("finals_bracket.bracket_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("week_number", sa.Integer(), nullable=False),
        sa.Column(
            "bbbffl_round_id",
            sa.Text(),
            sa.ForeignKey("bbbffl_round.bbbffl_round_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("label", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("bracket_id", "week_number"),
        sa.UniqueConstraint("bbbffl_round_id", name="uq_finals_bracket_week_round"),
        sa.CheckConstraint("week_number BETWEEN 1 AND 4", name="ck_finals_bracket_week_number_range"),
    )

    op.create_table(
        "finals_bracket_pairing",
        sa.Column("pairing_id", sa.Text(), primary_key=True),
        sa.Column(
            "bracket_id", sa.Text(), sa.ForeignKey("finals_bracket.bracket_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("week_number", sa.Integer(), nullable=False),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("matchup_id", sa.Text(), sa.ForeignKey("bbbffl_matchup.matchup_id", ondelete="RESTRICT")),
        sa.Column("home_season_entry_id", sa.Text(), nullable=False),
        sa.Column("away_season_entry_id", sa.Text()),
        sa.Column("source_matchup_id_1", sa.Text()),
        sa.Column("source_official_version_1", sa.Integer()),
        sa.Column("source_matchup_id_2", sa.Text()),
        sa.Column("source_official_version_2", sa.Integer()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "superseded_by_pairing_id",
            sa.Text(),
            sa.ForeignKey("finals_bracket_pairing.pairing_id", ondelete="RESTRICT"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.UniqueConstraint("matchup_id", name="uq_finals_bracket_pairing_matchup"),
        sa.CheckConstraint("week_number BETWEEN 1 AND 4", name="ck_finals_bracket_pairing_week_range"),
        sa.CheckConstraint(
            "slot IN ('bye', 'qf', 'ef', 'second_semi', 'first_semi', 'preliminary', 'grand_final')",
            name="ck_finals_bracket_pairing_slot",
        ),
        sa.CheckConstraint("status IN ('active', 'superseded')", name="ck_finals_bracket_pairing_status"),
        sa.CheckConstraint(
            "(slot = 'bye' AND matchup_id IS NULL AND away_season_entry_id IS NULL) OR "
            "(slot != 'bye' AND away_season_entry_id IS NOT NULL)",
            name="ck_finals_bracket_pairing_bye_shape",
        ),
    )
    op.create_index(
        "uq_finals_bracket_pairing_active_slot",
        "finals_bracket_pairing",
        ["bracket_id", "week_number", "slot"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_finals_bracket_pairing_bracket", "finals_bracket_pairing", ["bracket_id"])

    op.create_table(
        "finals_bracket_elimination",
        sa.Column("elimination_id", sa.Text(), primary_key=True),
        sa.Column(
            "bracket_id", sa.Text(), sa.ForeignKey("finals_bracket.bracket_id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("season_entry_id", sa.Text(), nullable=False),
        sa.Column(
            "source_pairing_id", sa.Text(), sa.ForeignKey("finals_bracket_pairing.pairing_id", ondelete="RESTRICT")
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "superseded_by_elimination_id",
            sa.Text(),
            sa.ForeignKey("finals_bracket_elimination.elimination_id", ondelete="RESTRICT"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.CheckConstraint(
            "stage IN ('pre_finals', 'week1_elimination_final', 'week2_first_semi_final', 'week3_preliminary_final')",
            name="ck_finals_bracket_elimination_stage",
        ),
        sa.CheckConstraint("status IN ('active', 'superseded')", name="ck_finals_bracket_elimination_status"),
        sa.CheckConstraint(
            "(stage = 'pre_finals' AND source_pairing_id IS NULL) OR "
            "(stage != 'pre_finals' AND source_pairing_id IS NOT NULL)",
            name="ck_finals_bracket_elimination_source_shape",
        ),
    )
    op.create_index(
        "uq_finals_bracket_elimination_active_entry",
        "finals_bracket_elimination",
        ["bracket_id", "season_entry_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_finals_bracket_elimination_bracket", "finals_bracket_elimination", ["bracket_id"])

    # `finals_bracket_seed`/`finals_bracket_result_reference`/`finals_bracket_week`
    # are frozen at creation time and never revised in place -- exactly
    # `finals_seeding_snapshot`'s own immutability rationale (0028's module
    # docstring). `finals_bracket_pairing`/`finals_bracket_elimination`
    # deliberately are NOT in this list: Steve's confirmed correction/rewind
    # policy supersedes a row's `status` column in place (active ->
    # superseded) rather than ever mutating its winner/loser-describing
    # fields -- see `app.finals.rewind_bracket`. Locking `status` itself
    # down here would make that supersede path impossible; the append-only
    # guarantee for pairings/eliminations instead comes from the domain
    # code only ever superseding (never rewriting a fact) plus every prior
    # version staying permanently queryable.
    if bind.dialect.name == "sqlite":
        for table in _APPEND_ONLY_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} history is immutable'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} history is immutable'); END"
            )
    else:
        op.execute("""
        CREATE FUNCTION reject_immutable_finals_bracket_change() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'finals bracket history is immutable'; END; $$ LANGUAGE plpgsql
        """)
        for table in _APPEND_ONLY_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_immutable_finals_bracket_change()"
            )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM finals_bracket")).scalar_one():
        raise RuntimeError("0030 downgrade refused: finals bracket history would be lost")

    if bind.dialect.name == "sqlite":
        for table in _APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_update")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_delete")
    else:
        for table in _APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.execute("DROP FUNCTION reject_immutable_finals_bracket_change() CASCADE")

    for table in (
        "finals_bracket_elimination",
        "finals_bracket_pairing",
        "finals_bracket_week",
        "finals_bracket_result_reference",
        "finals_bracket_seed",
        "finals_bracket",
    ):
        op.drop_table(table)
