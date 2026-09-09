"""Issue #178: the 2026 second-half replay continuation (9 -> 20 rounds).

Builds a realistic verified-Phase-1 baseline (nine finalised logical rounds,
a frozen ten-team fixture draw, published official results and lifecycle
history) and proves the continuation preserves every byte of it while
extending the season/fixture/logical-round structure to Rounds 10-20.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.audit import AuditEventRepository
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import transaction
from app.fixtures import FixtureRepository, fixture_number_rotation
from app.identity import IdentityRepository
from app.replay_continuation import (
    SOURCE_ROUND_COUNT,
    TARGET_ROUND_COUNT,
    ReplayContinuationError,
    continue_second_half_regular_season,
    describe_second_half_continuation,
)
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


class KnownRound:
    def __init__(self, season, rounds):
        self.reference = {(season, r) for r in rounds}

    def round_exists(self, season, round_):
        return (season, round_) in self.reference


def _nine_round_2026_baseline(database):
    """A verified Phase 1 checkpoint: 2026 season, 9-round frozen fixture,
    all nine logical rounds finalised with published official results --
    exactly the shape `docs/2026-second-half-replay-playbook.md` restores
    into the second-half working copy before continuation."""
    seasons = SeasonRepository(database)
    season = seasons.create_season(2026, "2026", regular_season_round_count=SOURCE_ROUND_COUNT)
    rules = seasons.create_rules_version(season.season_id, "ordinary", 1, "Rules")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "Ordinary", "ordinary"
    )
    identities = IdentityRepository(database)
    entries = []
    for number in range(1, 11):
        coach = identities.create_coach(f"Coach {number}")
        entries.append(identities.create_entry(season.season_id, f"licence-{number}", coach.coach_id, f"Team {number}"))

    fixtures = FixtureRepository(database)
    ordered_entries = [entry.season_entry_id for entry in entries]
    fixtures.save_draft(season.season_id, ordered_entries)
    fixtures.freeze(season.season_id)

    validator = KnownRound(2026, range(100, 100 + SOURCE_ROUND_COUNT))
    lifecycle = CompetitionLifecycleRepository(database)
    mapping = RoundMappingRepository(database)
    logical_rounds = {}
    for number in range(1, SOURCE_ROUND_COUNT + 1):
        round_ = seasons.create_round(competition.competition_id, f"round-{number}", f"Round {number}", number)
        logical_rounds[number] = round_
        mapping.accept(round_.bbbffl_round_id, 2026, 100 + number - 1, validator)
        lifecycle.create_ordinary_round(round_.bbbffl_round_id)
        for state in ("open", "live", "review"):
            lifecycle.transition(round_.bbbffl_round_id, state)
        matches = lifecycle.list_matchups(round_.bbbffl_round_id)
        results = {m.matchup_id: (100 + m.matchup_order, 90) for m in matches}
        lifecycle.publish_results(round_.bbbffl_round_id, results)

    return {
        "database": database,
        "season": season,
        "competition": competition,
        "entries": entries,
        "ordered_entries": ordered_entries,
        "logical_rounds": logical_rounds,
        "fixtures": fixtures,
        "lifecycle": lifecycle,
    }


@pytest.fixture
def baseline():
    return _nine_round_2026_baseline(migrated_connection())


def _matchup_snapshot(fixtures, season_id, round_number=None):
    return [
        (m.fixture_matchup_id, m.bbbffl_round_number, m.matchup_order, m.home_season_entry_id, m.away_season_entry_id)
        for m in fixtures.list_matchups(season_id, round_number)
    ]


def test_valid_nine_round_baseline_continues_to_twenty(baseline):
    database, season = baseline["database"], baseline["season"]
    report = continue_second_half_regular_season(database)
    assert report["mutated"] is True
    assert report["already_continued"] is False
    assert report["regular_season_round_count"] == TARGET_ROUND_COUNT
    assert report["preserved_rounds"] == [1, SOURCE_ROUND_COUNT]
    assert report["appended_rounds"] == [SOURCE_ROUND_COUNT + 1, TARGET_ROUND_COUNT]
    assert len(report["logical_rounds_created"]) == 11

    updated = SeasonRepository(database).get_season(season.season_id)
    assert updated.regular_season_round_count == TARGET_ROUND_COUNT


def test_round_1_to_9_logical_round_ids_are_unchanged(baseline):
    database, competition = baseline["database"], baseline["competition"]
    before = {n: r.bbbffl_round_id for n, r in baseline["logical_rounds"].items()}
    continue_second_half_regular_season(database)
    after_rows = SeasonRepository(database).list_rounds(competition.competition_id)
    after = {r.sequence: r.bbbffl_round_id for r in after_rows if r.sequence <= SOURCE_ROUND_COUNT}
    assert after == before


def test_round_1_to_9_matchup_ids_pairings_and_order_are_unchanged(baseline):
    database, season, fixtures = baseline["database"], baseline["season"], baseline["fixtures"]
    before = _matchup_snapshot(fixtures, season.season_id)
    assert len(before) == SOURCE_ROUND_COUNT * 5
    continue_second_half_regular_season(database)
    after = _matchup_snapshot(fixtures, season.season_id, None)
    after_first_nine = [row for row in after if row[1] <= SOURCE_ROUND_COUNT]
    assert after_first_nine == before


def test_round_10_to_20_pairings_match_fixture_number_rotation(baseline):
    database, season = baseline["database"], baseline["season"]
    entries = baseline["ordered_entries"]
    fixtures = FixtureRepository(database)
    continue_second_half_regular_season(database)
    rotation = fixture_number_rotation(TARGET_ROUND_COUNT)
    for round_number in range(SOURCE_ROUND_COUNT + 1, TARGET_ROUND_COUNT + 1):
        matchups = sorted(fixtures.list_matchups(season.season_id, round_number), key=lambda m: m.matchup_order)
        expected = rotation[round_number - 1]
        actual = [(m.home_season_entry_id, m.away_season_entry_id) for m in matchups]
        expected_pairs = [(entries[home - 1], entries[away - 1]) for home, away in expected]
        assert actual == expected_pairs


def test_exactly_five_matchups_for_every_round_one_to_twenty(baseline):
    database, season = baseline["database"], baseline["season"]
    fixtures = FixtureRepository(database)
    continue_second_half_regular_season(database)
    for round_number in range(1, TARGET_ROUND_COUNT + 1):
        assert len(fixtures.list_matchups(season.season_id, round_number)) == 5


def test_logical_round_10_to_20_definitions_created_once(baseline):
    database, competition = baseline["database"], baseline["competition"]
    continue_second_half_regular_season(database)
    rounds = SeasonRepository(database).list_rounds(competition.competition_id)
    assert [r.sequence for r in rounds] == list(range(1, TARGET_ROUND_COUNT + 1))
    for round_ in rounds:
        assert round_.round_key == f"round-{round_.sequence}"
        assert round_.label == f"Round {round_.sequence}"

    # Rerunning must not create duplicates.
    continue_second_half_regular_season(database)
    rounds_again = SeasonRepository(database).list_rounds(competition.competition_id)
    assert [r.bbbffl_round_id for r in rounds_again] == [r.bbbffl_round_id for r in rounds]


def test_fixture_remains_frozen_after_continuation(baseline):
    database, season = baseline["database"], baseline["season"]
    fixtures = FixtureRepository(database)
    continue_second_half_regular_season(database)
    draw = fixtures.get_draw(season.season_id)
    assert draw.state == "frozen"
    assert draw.frozen_at is not None


def test_ordinary_frozen_fixture_editing_remains_prohibited_after_continuation(baseline):
    database, season = baseline["database"], baseline["season"]
    fixtures = FixtureRepository(database)
    continue_second_half_regular_season(database)

    with pytest.raises(ValueError, match="immutable"):
        fixtures.save_draft(season.season_id, baseline["ordered_entries"])
    with pytest.raises(IntegrityError, match="immutable"):
        with transaction(database) as conn:
            conn.execute(
                "DELETE FROM season_fixture_matchup WHERE fixture_draw_id=? AND bbbffl_round_number=20",
                (fixtures.get_draw(season.season_id).fixture_draw_id,),
            )
    with pytest.raises(IntegrityError, match="frozen fixture draw fixes season length"):
        with transaction(database) as conn:
            conn.execute(
                "UPDATE bbbffl_season SET regular_season_round_count=25 WHERE season_id=?",
                (season.season_id,),
            )


def test_round_1_to_9_lifecycle_submission_result_and_audit_history_unchanged(baseline):
    database = baseline["database"]
    lifecycle = baseline["lifecycle"]
    before_results = {}
    for number, round_ in baseline["logical_rounds"].items():
        persisted = lifecycle.get_round(round_.bbbffl_round_id)
        before_results[number] = (
            persisted.state,
            persisted.version,
            [
                (m.matchup_id, lifecycle.effective_result(m.matchup_id))
                for m in lifecycle.list_matchups(round_.bbbffl_round_id)
            ],
        )
    before_events = AuditEventRepository(database).list_events()

    continue_second_half_regular_season(database)

    for number, round_ in baseline["logical_rounds"].items():
        persisted = lifecycle.get_round(round_.bbbffl_round_id)
        after = (
            persisted.state,
            persisted.version,
            [
                (m.matchup_id, lifecycle.effective_result(m.matchup_id))
                for m in lifecycle.list_matchups(round_.bbbffl_round_id)
            ],
        )
        assert after == before_results[number]

    # Prior audit history is an unmodified prefix; new events are only appended.
    after_events = AuditEventRepository(database).list_events()
    assert after_events[: len(before_events)] == before_events
    assert len(after_events) > len(before_events)


def test_continuation_is_audited_with_before_after_provenance(baseline):
    database, season = baseline["database"], baseline["season"]
    fixtures = FixtureRepository(database)
    draw_before = fixtures.get_draw(season.season_id)
    report = continue_second_half_regular_season(database, reason="issue #178 test continuation")

    season_events = AuditEventRepository(database).list_events(
        entity_type="season", entity_id=season.season_id, action="replay.season.continued"
    )
    assert len(season_events) == 1
    event = season_events[0]
    assert event.reason == "issue #178 test continuation"
    assert event.before_state == {"regular_season_round_count": SOURCE_ROUND_COUNT}
    assert event.after_state == {"regular_season_round_count": TARGET_ROUND_COUNT}
    assert event.payload["fixture_draw_id"] == draw_before.fixture_draw_id
    assert event.payload["preserved_rounds"] == [1, SOURCE_ROUND_COUNT]
    assert event.payload["appended_rounds"] == [SOURCE_ROUND_COUNT + 1, TARGET_ROUND_COUNT]

    draw_events = AuditEventRepository(database).list_events(
        entity_type="fixture_draw", entity_id=draw_before.fixture_draw_id, action="fixture.draw.continued"
    )
    assert len(draw_events) == 1
    assert draw_events[0].before_state["version"] == draw_before.version
    assert draw_events[0].after_state["version"] == draw_before.version + 1
    assert report["fixture_draw_version"] == draw_before.version + 1


def test_rerun_is_idempotent_and_deterministic(baseline):
    database, season = baseline["database"], baseline["season"]
    fixtures = FixtureRepository(database)
    first = continue_second_half_regular_season(database)
    matchups_after_first = _matchup_snapshot(fixtures, season.season_id)
    rounds_after_first = SeasonRepository(database).list_rounds(baseline["competition"].competition_id)

    second = continue_second_half_regular_season(database)
    assert second["already_continued"] is True
    assert second["mutated"] is False
    assert second["fixture_draw_version"] == first["fixture_draw_version"]

    matchups_after_second = _matchup_snapshot(fixtures, season.season_id)
    rounds_after_second = SeasonRepository(database).list_rounds(baseline["competition"].competition_id)
    assert matchups_after_second == matchups_after_first
    assert rounds_after_second == rounds_after_first

    # status reporting agrees, without mutating anything.
    status = describe_second_half_continuation(database)
    assert status["already_continued"] is True
    assert status["ready"] is True


def test_status_reports_pre_continuation_baseline_without_mutating(baseline):
    database = baseline["database"]
    fixtures = FixtureRepository(database)
    before = _matchup_snapshot(fixtures, baseline["season"].season_id)
    status = describe_second_half_continuation(database)
    assert status["ready"] is True
    assert status["already_continued"] is False
    assert status["regular_season_round_count"] == SOURCE_ROUND_COUNT
    assert _matchup_snapshot(fixtures, baseline["season"].season_id) == before


def _competition_with_entries(database, year, round_count):
    seasons = SeasonRepository(database)
    season = seasons.create_season(year, str(year), regular_season_round_count=round_count)
    rules = seasons.create_rules_version(season.season_id, "ordinary", 1, "Rules")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "Ordinary", "ordinary"
    )
    identities = IdentityRepository(database)
    entries = [
        identities.create_entry(
            season.season_id, f"licence-{i}", identities.create_coach(f"Coach {i}").coach_id, f"Team {i}"
        )
        for i in range(1, 11)
    ]
    return season, competition, entries


def test_season_round_count_outside_nine_or_twenty_fails_before_mutation():
    # An unrelated, incorrect round count -- no fixture draw is even required
    # to catch this; the season fact alone already rules the baseline out.
    database = migrated_connection()
    SeasonRepository(database).create_season(2026, "2026", regular_season_round_count=15)
    with pytest.raises(ReplayContinuationError, match="expected"):
        continue_second_half_regular_season(database)
    assert SeasonRepository(database).get_season_by_year(2026).regular_season_round_count == 15


def test_fixture_draw_not_yet_frozen_fails_before_mutation():
    database = migrated_connection()
    season, competition, entries = _competition_with_entries(database, 2026, SOURCE_ROUND_COUNT)
    for number in range(1, SOURCE_ROUND_COUNT + 1):
        SeasonRepository(database).create_round(
            competition.competition_id, f"round-{number}", f"Round {number}", number
        )
    fixtures = FixtureRepository(database)
    fixtures.save_draft(season.season_id, [e.season_entry_id for e in entries])  # never frozen

    with pytest.raises(ReplayContinuationError, match="frozen"):
        continue_second_half_regular_season(database)
    assert fixtures.get_draw(season.season_id).state == "draft"


def test_missing_logical_round_fails_before_mutation():
    database = migrated_connection()
    season, competition, entries = _competition_with_entries(database, 2026, SOURCE_ROUND_COUNT)
    for number in range(1, SOURCE_ROUND_COUNT):  # deliberately omit round 9
        SeasonRepository(database).create_round(
            competition.competition_id, f"round-{number}", f"Round {number}", number
        )
    FixtureRepository(database).save_draft(season.season_id, [e.season_entry_id for e in entries])
    FixtureRepository(database).freeze(season.season_id)

    with pytest.raises(ReplayContinuationError, match="logical rounds"):
        continue_second_half_regular_season(database)


def test_unexpected_extra_round_fails_before_mutation():
    database = migrated_connection()
    season, competition, entries = _competition_with_entries(database, 2026, SOURCE_ROUND_COUNT)
    seasons = SeasonRepository(database)
    for number in range(1, SOURCE_ROUND_COUNT + 1):
        seasons.create_round(competition.competition_id, f"round-{number}", f"Round {number}", number)
    seasons.create_round(competition.competition_id, "round-10", "Round 10", 10)  # unexpected, pre-continuation

    with pytest.raises(ReplayContinuationError, match="logical rounds"):
        continue_second_half_regular_season(database)


def test_round_9_not_final_fails_before_mutation(baseline):
    database, season = baseline["database"], baseline["season"]
    fixtures = FixtureRepository(database)
    before = _matchup_snapshot(fixtures, season.season_id)
    round_9_id = baseline["logical_rounds"][9].bbbffl_round_id
    with transaction(database) as conn:
        conn.execute(
            "UPDATE bbbffl_round_lifecycle SET state='review' WHERE bbbffl_round_id=?",
            (round_9_id,),
        )

    with pytest.raises(ReplayContinuationError, match="final"):
        continue_second_half_regular_season(database)

    assert SeasonRepository(database).get_season(season.season_id).regular_season_round_count == SOURCE_ROUND_COUNT
    assert _matchup_snapshot(fixtures, season.season_id) == before


def test_no_2026_season_fails_closed():
    database = migrated_connection()
    SeasonRepository(database).create_season(2027, "2027")
    with pytest.raises(ReplayContinuationError, match="exactly one"):
        continue_second_half_regular_season(database)
