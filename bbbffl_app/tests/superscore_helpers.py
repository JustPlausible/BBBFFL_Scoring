"""Shared setup for tests/test_superscore*.py: a completed 20-round ordinary
season (tests.finals_seeding_helpers.build_2026_replay_season) plus a
`superscore`-typed competition stream with SS1-SS4 fully set up
(app.superscore_round.setup_round) -- issue #192.

docs/round-afl-mapping.md's "2026 evidence" establishes that the 2026
finals series occupies AFL rounds 21-24 (BBBFFL finals weeks 1-4). The
confirmed SuperScore rule (docs/2026-finals-superscore-design.md's
"SuperScore design") is that SS1-SS4 run across the *identical* four AFL
rounds, concurrent with finals -- so this module derives SS1-SS4's AFL
mapping from that same evidence, never a fresh/independent assumption."""

from app.round_mapping import RoundMappingRepository
from app.superscore_round import confirm_afl_mapping, ensure_round, ensure_stream, setup_round
from tests.finals_helpers import KnownRound
from tests.finals_seeding_helpers import build_2026_replay_season

# The 2026 finals series occupies AFL rounds 21-24 (docs/round-afl-mapping.md).
FINALS_AFL_ROUNDS = {1: 21, 2: 22, 3: 23, 4: 24}


def build_superscore_ready_season(**kwargs):
    """A fully-finalised 20-round 2026-shaped ordinary season plus a
    sibling `superscore`-typed `competition_stream` with SS1-SS4 rounds --
    AFL mapping confirmed (against the same evidence finals weeks use) and
    round-lifecycle/review-state setup already run (`setup_round`), so a
    round only needs `app.superscore_round.open_round` to accept lineups."""
    built = build_2026_replay_season(**kwargs)
    database, season = built["database"], built["season"]
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()
    ordinary_competition_id = built["competition"].competition_id
    stream = ensure_stream(database, season.season_id, rules_row["rules_version_id"], ordinary_competition_id)
    built["superscore_stream"] = stream
    built["ordinary_competition_id"] = ordinary_competition_id

    validator = KnownRound({(season.year, afl_round_id) for afl_round_id in FINALS_AFL_ROUNDS.values()})
    superscore_rounds = {}
    for number in range(1, 5):
        round_id = ensure_round(database, stream.competition_id, number, number)
        afl_round_id = FINALS_AFL_ROUNDS[number]
        confirm_afl_mapping(
            database,
            validator,
            round_id,
            season.year,
            afl_round_id,
            reason=f"SS{number} concurrent with 2026 finals week {number} (docs/round-afl-mapping.md)",
        )
        setup_round(database, round_id, reason=f"SS{number} round setup")
        superscore_rounds[number] = round_id
    built["superscore_rounds"] = superscore_rounds
    built["superscore_afl_rounds"] = dict(FINALS_AFL_ROUNDS)
    return built


def open_superscore_round(database, bbbffl_round_id, *, reason="open SuperScore round"):
    from app.superscore_round import open_round

    return open_round(database, bbbffl_round_id, reason=reason)


def open_superscore_round_live(database, bbbffl_round_id, *, reason="open SuperScore round"):
    from app.competition_lifecycle import CompetitionLifecycleRepository

    open_superscore_round(database, bbbffl_round_id, reason=reason)
    CompetitionLifecycleRepository(database).transition(bbbffl_round_id, "live", reason=reason)


def accept_finals_week_mapping(database, bbbffl_round_id, *, year, week_number):
    """The finals-week counterpart used by cross-stream-fallback tests that
    need both a finals week and a SuperScore round mapped to the identical
    AFL round evidence -- mirrors `tests.finals_helpers.accept_week_mapping`
    exactly, kept here only to make the shared `FINALS_AFL_ROUNDS` table the
    single source for both streams' evidence in one test module."""
    RoundMappingRepository(database).accept(
        bbbffl_round_id,
        year,
        FINALS_AFL_ROUNDS[week_number],
        KnownRound({(year, FINALS_AFL_ROUNDS[week_number])}),
    )
