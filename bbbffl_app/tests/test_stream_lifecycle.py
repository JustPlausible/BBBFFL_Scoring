"""Issue #197: generalize `bbbffl_round_lifecycle`/`bbbffl_matchup` so a
finals/superscore-typed round can obtain lifecycle state, transition
`upcoming -> open`, and accept a lineup submission -- without changing any
ordinary-round behaviour.

Path 1 was chosen (loosen the fixture-draw linkage) over Path 2 (parallel
lifecycle/matchup storage with a dispatching lookup) -- see the module
docstring of `app.competition_lifecycle` and
`docs/2026-finals-superscore-design.md`'s "A foundational schema fork" for
the full reasoning. Because Path 1 makes the *existing* tables/call sites
work unchanged once a lifecycle/matchup row with a null fixture context
exists, most of the acceptance criteria below exercise `app.lineups`/
`app.lineup_adjudication`/`app.calculations`/`app.round_review` completely
unmodified against a finals/superscore round -- proving the schema
generalization, not a parallel code path.
"""

import pytest
from sqlalchemy import text

from app.afl_client import Match, PlayerStatLine, Team
from app.audit import ActorContext
from app.calculations import MatchupCalculationService
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.identity import IdentityRepository
from app.lineup_adjudication import LineupAdjudicationService
from app.lineups import POSITIONS, WeeklyLineupRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.round_mapping import RoundMappingRepository
from app.round_review import RoundReviewRepository, build_matchup_review
from app.season import SeasonRepository, _now
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import KnownRound, operational
from tests.test_lockouts import FakeMatchFacts


class Facts:
    """Same shape as tests.test_calculations.Facts -- a minimal afl_client
    double for MatchupCalculationService, reused here for a finals matchup."""

    def __init__(self, stats):
        self.stats = stats
        self.match = Match(700, Team(1, "A"), Team(2, "B"), "LIVE")

    def get_matches(self, round_id):
        return [self.match]

    def get_match_player_stats(self, match_id):
        return self.stats


def _non_ordinary_round(stream_type, *, year=2050, afl_round=None):
    """A single `stream_type`-typed ('finals' or 'superscore') competition
    stream with one BBBFFL round and an accepted AFL mapping -- deliberately
    built without ever touching `app.fixtures` at all, unlike
    tests.test_competition_lifecycle.configured()/operational()."""
    afl_round = afl_round or year
    db = migrated_connection()
    seasons = SeasonRepository(db)
    season = seasons.create_season(year, f"{year} {stream_type}")
    rules = seasons.create_rules_version(season.season_id, "canonical", 1, "Rules")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, stream_type, stream_type.title(), stream_type
    )
    round_ = seasons.create_round(competition.competition_id, f"{stream_type}-1", f"{stream_type} 1", 1)
    RoundMappingRepository(db).accept(round_.bbbffl_round_id, year, afl_round, KnownRound(year, afl_round))
    lifecycle = CompetitionLifecycleRepository(db)
    lifecycle.create_non_ordinary_round(round_.bbbffl_round_id)
    identities = IdentityRepository(db)
    entries = [
        identities.create_entry(
            season.season_id,
            f"{stream_type}-{n}",
            identities.create_coach(f"{stream_type} coach {n}").coach_id,
            f"{stream_type} team {n}",
        )
        for n in range(2)
    ]
    return db, lifecycle, season, round_, entries


# -- Lifecycle row and transition -------------------------------------------


def test_finals_round_gets_lifecycle_row_with_null_fixture_context():
    _db, lifecycle, _season, round_, _entries = _non_ordinary_round("finals")
    persisted = lifecycle.get_round(round_.bbbffl_round_id)
    assert persisted.state == "upcoming"
    assert persisted.fixture_draw_id is None
    assert persisted.fixture_draw_version is None
    assert persisted.fixture_round_number is None
    assert lifecycle.list_matchups(round_.bbbffl_round_id) == []


def test_superscore_round_gets_lifecycle_row_with_null_fixture_context():
    _db, lifecycle, _season, round_, _entries = _non_ordinary_round("superscore")
    persisted = lifecycle.get_round(round_.bbbffl_round_id)
    assert persisted.state == "upcoming"
    assert persisted.fixture_draw_id is None


def test_finals_round_transitions_from_upcoming_to_open():
    _db, lifecycle, _season, round_, _entries = _non_ordinary_round("finals")
    opened = lifecycle.transition(round_.bbbffl_round_id, "open")
    assert opened.state == "open"


def test_superscore_round_transitions_from_upcoming_to_open():
    _db, lifecycle, _season, round_, _entries = _non_ordinary_round("superscore")
    opened = lifecycle.transition(round_.bbbffl_round_id, "open")
    assert opened.state == "open"


def test_ordinary_round_still_requires_fixture_context_unchanged():
    """Regression guard: an ordinary round created via the pre-existing,
    completely unmodified `create_ordinary_round` still always populates
    the fixture-draw context -- Path 1 never weakens this for ordinary
    rounds, only skips it for a round that never had one."""
    database = migrated_connection()
    _lifecycle, round_, _entries = operational(database)
    lifecycle = CompetitionLifecycleRepository(database)
    persisted = lifecycle.get_round(round_.bbbffl_round_id)
    assert persisted.fixture_draw_id is not None
    assert persisted.fixture_draw_version is not None
    assert persisted.fixture_round_number is not None


def test_create_non_ordinary_round_rejects_an_ordinary_stream():
    database = migrated_connection()
    seasons = SeasonRepository(database)
    season = seasons.create_season(2051, "2051")
    rules = seasons.create_rules_version(season.season_id, "r", 1, "Rules")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "Ordinary", "ordinary"
    )
    round_ = seasons.create_round(competition.competition_id, "r1", "R1", 1)
    RoundMappingRepository(database).accept(round_.bbbffl_round_id, 2051, 2051, KnownRound(2051, 2051))

    with pytest.raises(ValueError, match="non-ordinary"):
        CompetitionLifecycleRepository(database).create_non_ordinary_round(round_.bbbffl_round_id)


def test_create_ordinary_round_still_rejects_a_finals_stream():
    """Unchanged pre-existing guard in `create_ordinary_round` -- Path 1
    does not touch it."""
    _db, lifecycle, _season, round_, _entries = _non_ordinary_round("finals", year=2052)
    with pytest.raises(ValueError, match="ordinary competition stream"):
        # A fresh lifecycle already exists from `_non_ordinary_round`, so this
        # also exercises the "already exists" guard first on a re-attempt of
        # the wrong method; the stream-type guard is checked first.
        lifecycle.create_ordinary_round(round_.bbbffl_round_id)


def test_create_stream_matchup_rejects_an_ordinary_round():
    database = migrated_connection()
    lifecycle, round_, entries = operational(database)
    with pytest.raises(ValueError, match="finals competition stream"):
        lifecycle.create_stream_matchup(
            round_.bbbffl_round_id, 1, entries[0].season_entry_id, entries[1].season_entry_id
        )


def test_create_stream_matchup_rejects_a_superscore_round():
    """SuperScore is matchup-free by design (docs/2026-finals-superscore-
    design.md) -- `create_stream_matchup` is a finals-only primitive."""
    db, lifecycle, _season, round_, entries = _non_ordinary_round("superscore", year=2062)
    with pytest.raises(ValueError, match="finals competition stream"):
        lifecycle.create_stream_matchup(
            round_.bbbffl_round_id, 1, entries[0].season_entry_id, entries[1].season_entry_id
        )


def test_create_stream_matchup_rejects_an_entry_from_a_different_season():
    db, lifecycle, season, round_, entries = _non_ordinary_round("finals", year=2063)
    other_identities = IdentityRepository(db)
    other_season = SeasonRepository(db).create_season(2064, "2064 other season")
    outsider = other_identities.create_entry(
        other_season.season_id, "outsider", other_identities.create_coach("Outsider Coach").coach_id, "Outsider"
    )
    with pytest.raises(ValueError, match="own season"):
        lifecycle.create_stream_matchup(round_.bbbffl_round_id, 1, entries[0].season_entry_id, outsider.season_entry_id)


# -- Frozen-context validation is stream-aware but still mapping-checked ----


def test_validate_frozen_context_skips_fixture_check_for_null_fixture_draw():
    """The fixture-draw half of `_validate_frozen_context` never even runs
    for a round with no fixture context -- proven by this transition
    succeeding at all (an ordinary round with a null/foreign fixture_draw_id
    would instead raise "frozen fixture context changed")."""
    _db, lifecycle, _season, round_, _entries = _non_ordinary_round("finals", year=2053)
    lifecycle.transition(round_.bbbffl_round_id, "open")  # does not raise


def test_validate_frozen_context_still_enforces_mapping_revision_for_null_fixture_draw():
    """The mapping-revision half of the same check is unconditional --
    stream-awareness only ever skips the fixture-draw half."""
    db, lifecycle, _season, round_, _entries = _non_ordinary_round("finals", year=2054)
    RoundMappingRepository(db).correct(
        round_.bbbffl_round_id, 2054, 2054 + 1, KnownRound(2054, 2054 + 1), reason="AFL correction"
    )
    with pytest.raises(ValueError, match="mapping changed"):
        lifecycle.transition(round_.bbbffl_round_id, "open")
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "upcoming"


# -- Lineup submission through the unmodified WeeklyLineupRepository --------


def _submit_one_position(db, lifecycle, season, round_, entry):
    scope = db.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    OwnershipRepository(db).configure_squad_limit(scope["season_id"], 5)
    player = PlayerPoolRepository(db).refresh_player(scope["season_id"], 900001, "Stream Fixture Player")
    OwnershipRepository(db).acquire(player.season_player_id, entry.season_entry_id)
    lineups = WeeklyLineupRepository(db)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": player.season_player_id},
        expected_revision=0,
    )
    return lineups.submit(draft.lineup_id, expected_draft_revision=1, expected_submission_version=0)


def test_finals_round_accepts_a_lineup_submission_through_weekly_lineup_repository_submit():
    db, lifecycle, season, round_, entries = _non_ordinary_round("finals", year=2055)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    submitted = _submit_one_position(db, lifecycle, season, round_, entries[0])
    assert submitted.version == 1
    assert submitted.positions["F1"] is not None


def test_superscore_round_accepts_a_lineup_submission_through_weekly_lineup_repository_submit():
    db, lifecycle, season, round_, entries = _non_ordinary_round("superscore", year=2056)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    submitted = _submit_one_position(db, lifecycle, season, round_, entries[0])
    assert submitted.version == 1


# -- Adjudication eligibility resolves live/review, not "unknown" -----------


def test_eligibility_resolves_live_state_for_a_finals_round_instead_of_unknown():
    db, lifecycle, season, round_, entries = _non_ordinary_round("finals", year=2057)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    lifecycle.transition(round_.bbbffl_round_id, "live")
    scope = db.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    service = LineupAdjudicationService(db, afl_client=None)
    service.match_facts = FakeMatchFacts([])
    _lineup_id, _effective_version, round_state, _activated, _reasons = service._eligibility(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )
    assert round_state == "live"
    assert round_state != "unknown"


def test_eligibility_resolves_review_state_for_a_superscore_round_instead_of_unknown():
    db, lifecycle, season, round_, entries = _non_ordinary_round("superscore", year=2058)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    lifecycle.transition(round_.bbbffl_round_id, "live")
    lifecycle.transition(round_.bbbffl_round_id, "review")
    scope = db.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    service = LineupAdjudicationService(db, afl_client=None)
    service.match_facts = FakeMatchFacts([])
    _lineup_id, _effective_version, round_state, _activated, _reasons = service._eligibility(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )
    assert round_state == "review"


# -- Finals matchup storage works with the existing calculation/review paths


def _finals_matchup_with_submitted_lineups(year=2059):
    db, lifecycle, season, round_, entries = _non_ordinary_round("finals", year=year)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    matchup = lifecycle.create_stream_matchup(
        round_.bbbffl_round_id, 1, entries[0].season_entry_id, entries[1].season_entry_id
    )
    assert matchup.fixture_matchup_id is None
    scope = db.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    stats = {}
    now = _now()
    with db.engine.begin() as conn:
        for index, entry in enumerate(entries):
            lineup_id = f"finals-lineup-{year}-{index}"
            conn.execute(
                text(
                    "INSERT INTO weekly_lineup "
                    "(lineup_id, season_id, competition_id, bbbffl_round_id, "
                    "season_entry_id, draft_revision, effective_submission_version, "
                    "created_at, updated_at) "
                    "VALUES (:lineup_id, :season_id, :competition_id, :round_id, "
                    ":entry_id, 1, 1, :now, :now)"
                ),
                {
                    "lineup_id": lineup_id,
                    "season_id": scope["season_id"],
                    "competition_id": scope["competition_id"],
                    "round_id": round_.bbbffl_round_id,
                    "entry_id": entry.season_entry_id,
                    "now": now,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO weekly_lineup_submission "
                    "(lineup_id, version, based_on_draft_revision, submitted_at, "
                    "actor_type, actor_id, actor_role, source_type, source_detail, reason) "
                    "VALUES (:lineup_id, 1, 1, :now, 'coach', NULL, 'coach', "
                    "'coach', NULL, NULL)"
                ),
                {"lineup_id": lineup_id, "now": now},
            )
            for position in POSITIONS:
                player = None
                if position == "F1":
                    canonical = 9000 + index
                    player = f"finals-player-{year}-{canonical}"
                    conn.execute(
                        text(
                            "INSERT INTO season_player_pool "
                            "(season_player_id, season_id, canonical_player_id, "
                            "display_name, afl_team_id, afl_team_name, eligible, "
                            "source_provider, source_fetched_at, source_updated_at, "
                            "created_at, updated_at) "
                            "VALUES (:player_id, :season_id, :canonical_id, :name, "
                            "1, NULL, TRUE, 'afl-api', :now, NULL, :now, :now)"
                        ),
                        {
                            "player_id": player,
                            "season_id": scope["season_id"],
                            "canonical_id": canonical,
                            "name": f"FP{canonical}",
                            "now": now,
                        },
                    )
                    stats[canonical] = PlayerStatLine(canonical, goals=index + 1)
                conn.execute(
                    text(
                        "INSERT INTO weekly_lineup_draft_slot "
                        "(lineup_id, position, season_player_id, updated_at) "
                        "VALUES (:lineup_id, :position, :player_id, :now)"
                    ),
                    {"lineup_id": lineup_id, "position": position, "player_id": player, "now": now},
                )
                conn.execute(
                    text(
                        "INSERT INTO weekly_lineup_submission_slot "
                        "(lineup_id, version, position, season_player_id) "
                        "VALUES (:lineup_id, 1, :position, :player_id)"
                    ),
                    {"lineup_id": lineup_id, "position": position, "player_id": player},
                )
    return db, lifecycle, round_, matchup, stats, entries


def test_finals_matchup_can_be_calculated_with_no_fixture_matchup_id():
    db, lifecycle, round_, matchup, stats, _entries = _finals_matchup_with_submitted_lineups()
    service = MatchupCalculationService(db, Facts(stats))
    calculated = service.calculate_matchup(matchup.matchup_id)
    assert calculated.matchup_id == matchup.matchup_id
    assert calculated.snapshot["home"]["score"] is not None
    assert lifecycle.get_calculation(matchup.matchup_id).revision == 1


def test_finals_matchup_supports_dnp_ruling_override_and_review_build():
    db, lifecycle, round_, matchup, stats, entries = _finals_matchup_with_submitted_lineups(year=2060)
    MatchupCalculationService(db, Facts(stats)).calculate_matchup(matchup.matchup_id)
    review_repo = RoundReviewRepository(db)
    scorer = ActorContext.anonymous_operator("scorer")

    next_version = review_repo.record_dnp_ruling(
        matchup.matchup_id,
        entries[0].season_entry_id,
        "F1",
        True,
        expected_review_version=1,
        actor=scorer,
        reason="fixture ruling",
    )
    assert next_version == 2

    next_version = review_repo.record_override(
        matchup.matchup_id,
        entries[1].season_entry_id,
        "F1",
        55.0,
        None,
        "manual override for fixture",
        expected_review_version=next_version,
        actor=scorer,
    )
    assert next_version == 3

    review = build_matchup_review(lifecycle, review_repo, None, lifecycle.get_matchup(matchup.matchup_id))
    assert review.matchup_id == matchup.matchup_id
    assert review.review_version == 3
    home_f1 = next(slot for slot in review.home.slots if slot.slot == "F1")
    assert home_f1.dnp_ruling is True
    away_f1 = next(slot for slot in review.away.slots if slot.slot == "F1")
    assert away_f1.override_score == 55.0


def test_finals_matchup_official_result_can_be_corrected_with_no_fixture_matchup_id():
    """`CompetitionLifecycleRepository.correct_matchup_result` (the
    single-matchup correction path `app.round_review.attempt_correction`
    uses) never joins against `season_fixture_matchup` -- it works
    identically for a finals matchup with a null `fixture_matchup_id`. The
    round-wide `publish_results`/`correct_results` (hard-coded to exactly
    five matchups) remain out of scope for a finals week's variable match
    count -- that adapter is #190's job, not #197's -- so this seeds the
    first official version directly, exactly as #190's own finals-specific
    publish command will."""
    db, lifecycle, round_, matchup, stats, entries = _finals_matchup_with_submitted_lineups(year=2061)
    now = _now()
    with db.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO bbbffl_official_result VALUES (:matchup_id, 1, 50, 40, :now, NULL, NULL, NULL)"),
            {"matchup_id": matchup.matchup_id, "now": now},
        )
        conn.execute(
            text("UPDATE bbbffl_matchup SET effective_official_version=1 WHERE matchup_id=:matchup_id"),
            {"matchup_id": matchup.matchup_id},
        )
        conn.execute(
            text("UPDATE bbbffl_round_lifecycle SET state='final' WHERE bbbffl_round_id=:round_id"),
            {"round_id": round_.bbbffl_round_id},
        )
    corrected = lifecycle.correct_matchup_result(
        matchup.matchup_id, 60, 40, reason="fixture correction for issue #197 regression coverage"
    )
    assert corrected.version == 2
    assert corrected.home_score == 60
    history = lifecycle.result_history(matchup.matchup_id)
    assert [result.version for result in history] == [1, 2]
