"""Persisted ordinary-round lifecycle, publication atomicity and history."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.audit import AuditEventRepository
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.fixtures import FixtureRepository
from app.identity import IdentityRepository
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


class KnownRound:
    def __init__(self, season, round_):
        self.reference = season, round_

    def round_exists(self, season, round_):
        return (season, round_) == self.reference


def configured(database, year, afl_round=100):
    seasons = SeasonRepository(database)
    season = seasons.create_season(year, str(year))
    rules = seasons.create_rules_version(season.season_id, "ordinary", 1, "Rules")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "Ordinary", "ordinary"
    )
    logical_round = seasons.create_round(competition.competition_id, "round-1", "Round 1", 1)
    identities = IdentityRepository(database)
    entries = []
    for number in range(10):
        coach = identities.create_coach(f"Coach {year}-{number}")
        entries.append(identities.create_entry(season.season_id, f"licence-{number}", coach.coach_id, f"Team {number}"))
    fixtures = FixtureRepository(database)
    fixtures.save_draft(season.season_id, [entry.season_entry_id for entry in entries])
    fixtures.freeze(season.season_id)
    RoundMappingRepository(database).accept(logical_round.bbbffl_round_id, year, afl_round, KnownRound(year, afl_round))
    return logical_round, entries


def operational(database, year=2027, afl_round=100):
    logical_round, entries = configured(database, year, afl_round)
    lifecycle = CompetitionLifecycleRepository(database)
    lifecycle.create_ordinary_round(logical_round.bbbffl_round_id)
    return lifecycle, logical_round, entries


def progress_to_review(lifecycle, round_id):
    for state in ("open", "live", "review"):
        lifecycle.transition(round_id, state)


def scores(lifecycle, round_id, offset=0):
    return {
        match.matchup_id: (100 + offset + match.matchup_order, 90 + offset)
        for match in lifecycle.list_matchups(round_id)
    }


def test_creation_consumes_accepted_mapping_and_five_non_overlapping_fixture_pairs():
    database = migrated_connection()
    lifecycle, round_, entries = operational(database)
    persisted = lifecycle.get_round(round_.bbbffl_round_id)
    matchups = lifecycle.list_matchups(round_.bbbffl_round_id)
    assert persisted.state == "upcoming" and persisted.afl_round_id == 100
    assert len(matchups) == 5
    assert (
        len({entry for match in matchups for entry in (match.home_season_entry_id, match.away_season_entry_id)}) == 10
    )
    assert {entry.season_entry_id for entry in entries} == {
        entry for match in matchups for entry in (match.home_season_entry_id, match.away_season_entry_id)
    }


@pytest.mark.parametrize("mapping_state", ["unresolved", "ambiguous"])
def test_creation_fails_closed_for_nonaccepted_mapping(mapping_state):
    database = migrated_connection()
    seasons = SeasonRepository(database)
    season = seasons.create_season(2027, "2027")
    rules = seasons.create_rules_version(season.season_id, "r", 1, "Rules")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "Ordinary", "ordinary"
    )
    round_ = seasons.create_round(competition.competition_id, "r1", "R1", 1)
    identities = IdentityRepository(database)
    entries = [
        identities.create_entry(
            season.season_id,
            f"l-{i}",
            identities.create_coach(f"C{i}").coach_id,
            f"T{i}",
        )
        for i in range(10)
    ]
    fixtures = FixtureRepository(database)
    fixtures.save_draft(season.season_id, [entry.season_entry_id for entry in entries])
    fixtures.freeze(season.season_id)
    RoundMappingRepository(database).propose(round_.bbbffl_round_id, state=mapping_state)
    with pytest.raises(ValueError, match="accepted"):
        CompetitionLifecycleRepository(database).create_ordinary_round(round_.bbbffl_round_id)
    assert CompetitionLifecycleRepository(database).get_round(round_.bbbffl_round_id) is None


def test_valid_progression_and_invalid_transitions_have_no_mutation():
    database = migrated_connection()
    lifecycle, round_, _ = operational(database)
    with pytest.raises(ValueError, match="illegal"):
        lifecycle.transition(round_.bbbffl_round_id, "live")
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "upcoming"
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    with pytest.raises(ValueError, match="illegal"):
        lifecycle.transition(round_.bbbffl_round_id, "open")
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "review"


def test_mapping_revision_after_snapshot_cannot_silently_open_round():
    database = migrated_connection()
    lifecycle, round_, _ = operational(database)
    RoundMappingRepository(database).correct(
        round_.bbbffl_round_id,
        2027,
        101,
        KnownRound(2027, 101),
        reason="AFL correction",
    )
    with pytest.raises(ValueError, match="mapping changed"):
        lifecycle.transition(round_.bbbffl_round_id, "open")
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "upcoming"


def test_renames_do_not_change_historical_matchup_identity():
    database = migrated_connection()
    lifecycle, round_, entries = operational(database)
    before = lifecycle.list_matchups(round_.bbbffl_round_id)
    identities = IdentityRepository(database)
    identities.rename_team(entries[0].season_entry_id, "A new name")
    identities.transfer_entry(entries[1].season_entry_id, identities.create_coach("Replacement").coach_id)
    assert lifecycle.list_matchups(round_.bbbffl_round_id) == before


def test_first_finalisation_and_correction_preserve_immutable_versions():
    database = migrated_connection()
    lifecycle, round_, _ = operational(database)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    first = scores(lifecycle, round_.bbbffl_round_id)
    lifecycle.publish_results(round_.bbbffl_round_id, first, reason="approved")
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    assert [result.version for result in lifecycle.result_history(matchup.matchup_id)] == [1]
    assert lifecycle.effective_result(matchup.matchup_id).home_score == first[matchup.matchup_id][0]
    corrected = scores(lifecycle, round_.bbbffl_round_id, 10)
    lifecycle.correct_results(round_.bbbffl_round_id, corrected, reason="authorised transcription correction")
    history = lifecycle.result_history(matchup.matchup_id)
    assert [result.version for result in history] == [1, 2]
    assert history[0].home_score == first[matchup.matchup_id][0]
    assert lifecycle.effective_result(matchup.matchup_id).version == 2
    events = AuditEventRepository(database).list_events(entity_type="competition.matchup", entity_id=matchup.matchup_id)
    assert [event.action for event in events] == [
        "competition.result.published",
        "competition.result.corrected",
    ]
    assert events[-1].reason == "authorised transcription correction"
    with pytest.raises(IntegrityError, match="immutable"):
        from app.db import transaction

        with transaction(database) as conn:
            conn.execute(
                "UPDATE bbbffl_official_result SET home_score=0 WHERE matchup_id=? AND version=1",
                (matchup.matchup_id,),
            )


def test_simulated_failure_rolls_back_all_five_results_and_final_state():
    database = migrated_connection()
    lifecycle, round_, _ = operational(database)
    progress_to_review(lifecycle, round_.bbbffl_round_id)

    def fail_after_third(count):
        if count == 3:
            raise RuntimeError("simulated publication failure")

    with pytest.raises(RuntimeError, match="simulated"):
        lifecycle.publish_results(
            round_.bbbffl_round_id,
            scores(lifecycle, round_.bbbffl_round_id),
            failure_hook=fail_after_third,
        )
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "review"
    assert all(
        lifecycle.result_history(match.matchup_id) == [] and match.effective_official_version is None
        for match in lifecycle.list_matchups(round_.bbbffl_round_id)
    )


def test_replay_and_live_round_results_are_independent():
    database = migrated_connection()
    replay, replay_round, _ = operational(database, 2026, 90)
    live, live_round, _ = operational(database, 2027, 100)
    progress_to_review(replay, replay_round.bbbffl_round_id)
    replay.publish_results(replay_round.bbbffl_round_id, scores(replay, replay_round.bbbffl_round_id))
    assert replay.get_round(replay_round.bbbffl_round_id).state == "final"
    assert live.get_round(live_round.bbbffl_round_id).state == "upcoming"
    assert all(
        live.effective_result(match.matchup_id) is None for match in live.list_matchups(live_round.bbbffl_round_id)
    )


def test_calculated_snapshot_and_upstream_status_are_not_official_or_authoritative():
    database = migrated_connection()
    lifecycle, round_, _ = operational(database)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    lifecycle.save_calculation(matchup.matchup_id, {"home": 123})
    for status in ("LIVE", "POSTGAME", "CONCLUDED"):
        lifecycle.record_upstream_fact(round_.bbbffl_round_id, status)
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "upcoming"
    assert lifecycle.effective_result(matchup.matchup_id) is None
