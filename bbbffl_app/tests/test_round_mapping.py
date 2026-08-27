"""Regression coverage for authoritative BBBFFL/AFL round contexts."""

import pytest

from app.audit import AuditEventRepository
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


class KnownRounds:
    def __init__(self, *references):
        self.references = set(references)

    def round_exists(self, season_id, round_id):
        return (season_id, round_id) in self.references


@pytest.fixture
def domain():
    database = migrated_connection()
    seasons = SeasonRepository(database)
    mappings = RoundMappingRepository(database)

    def make(year, stream_key="ordinary", stream_type="ordinary", round_key="r1", sequence=1):
        season = seasons.get_season_by_year(year)
        if not season:
            season = seasons.create_season(year, f"{year} {'Replay' if year == 2026 else 'Live'}")
            rules = seasons.create_rules_version(season.season_id, "canonical", 1, "Rules")
        else:
            rules = seasons.list_rules_versions(season.season_id)[0]
        competitions = {c.stream_key: c for c in seasons.list_competitions(season.season_id)}
        competition = competitions.get(stream_key) or seasons.create_competition(
            season.season_id, rules.rules_version_id, stream_key, stream_key, stream_type
        )
        return seasons.create_round(competition.competition_id, round_key, round_key, sequence)

    return database, mappings, make


def test_regular_mapping_is_explicitly_validated_and_resolved(domain):
    _, mappings, make = domain
    round_ = make(2027)
    accepted = mappings.accept(round_.bbbffl_round_id, 85, 1412, KnownRounds((85, 1412)))
    assert mappings.resolve(round_.bbbffl_round_id) == accepted
    assert accepted.afl_round_id == 1412  # never inferred from BBBFFL sequence 1


def test_supported_2026_finals_exception_does_not_assume_equal_numbers(domain):
    """The workbook has four finals weeks after 20 H&A rounds; GF maps to AFL R24."""
    _, mappings, make = domain
    grand_final = make(2026, "finals", "finals", "grand-final", 4)
    result = mappings.accept(grand_final.bbbffl_round_id, 84, 1400, KnownRounds((84, 1400)))
    assert (grand_final.sequence, result.afl_round_id) == (4, 1400)


def test_unresolved_and_ambiguous_mappings_fail_closed(domain):
    _, mappings, make = domain
    unresolved = make(2026, round_key="opening-round-unresolved")
    ambiguous = make(2026, round_key="opening-round-ambiguous", sequence=2)
    mappings.propose(unresolved.bbbffl_round_id)
    mappings.propose(ambiguous.bbbffl_round_id, state="ambiguous", afl_season_id=84, afl_round_id=1390,
                     reason="Opening Round/deferred-bye treatment is not fully evidenced")
    assert mappings.resolve(unresolved.bbbffl_round_id) is None
    assert mappings.resolve(ambiguous.bbbffl_round_id) is None


def test_ordinary_and_superscore_independently_share_afl_context(domain):
    _, mappings, make = domain
    ordinary = make(2027, round_key="final-1")
    superscore = make(2027, "superscore", "superscore", "ss1")
    known = KnownRounds((85, 1412))
    first = mappings.accept(ordinary.bbbffl_round_id, 85, 1412, known)
    second = mappings.accept(superscore.bbbffl_round_id, 85, 1412, known)
    assert first.mapping_id != second.mapping_id
    assert mappings.resolve(ordinary.bbbffl_round_id).afl_round_id == mappings.resolve(superscore.bbbffl_round_id).afl_round_id


def test_replay_and_live_seasons_do_not_leak(domain):
    _, mappings, make = domain
    replay, live = make(2026), make(2027)
    known = KnownRounds((84, 1300), (85, 1412))
    mappings.accept(replay.bbbffl_round_id, 84, 1300, known)
    mappings.accept(live.bbbffl_round_id, 85, 1412, known)
    assert mappings.resolve(replay.bbbffl_round_id).afl_season_id == 84
    assert mappings.resolve(live.bbbffl_round_id).afl_season_id == 85


def test_new_convention_cannot_rewrite_frozen_history(domain):
    _, mappings, make = domain
    round_ = make(2026)
    mappings.accept(round_.bbbffl_round_id, 84, 1300, KnownRounds((84, 1300)))
    with pytest.raises(ValueError, match="correction"):
        mappings.propose(round_.bbbffl_round_id, afl_season_id=85, afl_round_id=1412)
    assert mappings.resolve(round_.bbbffl_round_id).afl_round_id == 1300


def test_authorised_correction_preserves_history_and_audit(domain):
    database, mappings, make = domain
    round_ = make(2026)
    known = KnownRounds((84, 1300), (84, 1301))
    original = mappings.accept(round_.bbbffl_round_id, 84, 1300, known)
    corrected = mappings.correct(round_.bbbffl_round_id, 84, 1301, known, reason="Official AFL round identity corrected")
    assert [item.afl_round_id for item in mappings.history(round_.bbbffl_round_id)] == [1300, 1301]
    assert corrected.revision == original.revision + 1
    events = AuditEventRepository(database).list_events(entity_type="round.afl_mapping", entity_id=original.mapping_id)
    assert events[-1].action == "round_mapping.corrected"
    assert events[-1].before_state["afl_round_id"] == 1300


def test_nonexistent_afl_reference_is_rejected_without_operational_state(domain):
    _, mappings, make = domain
    round_ = make(2027)
    with pytest.raises(ValueError, match="does not exist"):
        mappings.accept(round_.bbbffl_round_id, 85, 9999, KnownRounds((85, 1412)))
    assert mappings.resolve(round_.bbbffl_round_id) is None


def test_resolution_returns_only_current_authoritative_revision(domain):
    _, mappings, make = domain
    draft, accepted = make(2027), make(2027, round_key="r2", sequence=2)
    mappings.propose(draft.bbbffl_round_id, state="ambiguous")
    mappings.accept(accepted.bbbffl_round_id, 85, 1413, KnownRounds((85, 1413)))
    assert mappings.resolve(draft.bbbffl_round_id) is None
    assert mappings.resolve(accepted.bbbffl_round_id).state == "accepted"
