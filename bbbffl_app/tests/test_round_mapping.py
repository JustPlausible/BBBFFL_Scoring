"""Regression coverage for authoritative BBBFFL/AFL round contexts."""

import pytest

from app.afl_client import Round, Season
from app.audit import AuditEventRepository
from app.round_mapping import MappingRecommendation, RoundMappingRepository, recommend_mapping
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


class KnownRounds:
    def __init__(self, *references):
        self.references = set(references)

    def round_exists(self, season_id, round_id):
        return (season_id, round_id) in self.references


class RecommendationEvidence:
    """A minimal duck-typed AFL client double exposing only what
    `recommend_mapping` reads -- `get_seasons`/`get_rounds` -- mirroring
    the common contract shared by `AflApiClient` and the replay client."""

    def __init__(self, seasons, rounds_by_season, *, error=None):
        self._seasons = seasons
        self._rounds_by_season = rounds_by_season
        self._error = error

    def get_seasons(self):
        if self._error:
            raise self._error
        return self._seasons

    def get_rounds(self, season_id):
        if self._error:
            raise self._error
        return self._rounds_by_season.get(season_id, [])


class NoSeasonListing:
    """An older duck-typed double (e.g. tests elsewhere) that never grew a
    `get_seasons` method -- recommend_mapping must fail closed, not crash."""

    def get_rounds(self, season_id):
        return []


def test_recommend_mapping_matches_corresponding_year_and_round_number():
    client = RecommendationEvidence(
        seasons=[Season(season_id=85, is_current=True, current_round_number=1, year=2026, name="2026 Season")],
        rounds_by_season={85: [Round(round_id=1300, round_number=1), Round(round_id=1301, round_number=2)]},
    )
    recommendation = recommend_mapping(client, bbbffl_year=2026, bbbffl_sequence=1, bbbffl_stream_type="ordinary")
    assert recommendation == MappingRecommendation(
        afl_season_id=85,
        afl_round_id=1300,
        afl_season_year=2026,
        afl_round_number=1,
        evidence=recommendation.evidence,
    )
    assert "2026" in recommendation.evidence and "1" in recommendation.evidence


def test_recommend_mapping_returns_none_on_deliberate_finals_style_round_divergence():
    """BBBFFL finals week 4 (sequence 4) maps to AFL round 24 -- a numeric
    correspondence recommend_mapping must never invent (see
    docs/round-afl-mapping.md's 2026 finals evidence)."""
    client = RecommendationEvidence(
        seasons=[Season(season_id=84, is_current=False, current_round_number=24, year=2026)],
        rounds_by_season={84: [Round(round_id=1400, round_number=24)]},
    )
    assert recommend_mapping(client, bbbffl_year=2026, bbbffl_sequence=4, bbbffl_stream_type="ordinary") is None


def test_recommend_mapping_gated_to_ordinary_stream_even_when_numbers_coincidentally_match():
    """Codex review (P1) on issue #152's PR: a finals/superscore stream's
    own sequence numbering restarts independently of AFL's -- e.g. the
    repository's Grand Final fixture uses sequence 4
    (`test_authorised_correction_preserves_history_and_audit` et al. use
    `make(2026, ...)`; the real Grand Final round is `sequence=4` while its
    correct AFL mapping is round 24, per docs/round-afl-mapping.md). If an
    AFL season also happens to publish an (unrelated) round numbered 4,
    the ordinary-stream equal-number heuristic must never be applied to a
    non-ordinary stream just because the numbers happen to coincide."""
    client = RecommendationEvidence(
        seasons=[Season(season_id=84, is_current=False, current_round_number=24, year=2026)],
        rounds_by_season={84: [Round(round_id=1390, round_number=4), Round(round_id=1400, round_number=24)]},
    )
    assert recommend_mapping(client, bbbffl_year=2026, bbbffl_sequence=4, bbbffl_stream_type="finals") is None
    assert recommend_mapping(client, bbbffl_year=2026, bbbffl_sequence=4, bbbffl_stream_type="superscore") is None
    # The same evidence *does* produce a recommendation for the ordinary stream.
    assert recommend_mapping(client, bbbffl_year=2026, bbbffl_sequence=4, bbbffl_stream_type="ordinary") is not None


def test_recommend_mapping_returns_none_for_ambiguous_multi_season_year():
    client = RecommendationEvidence(
        seasons=[
            Season(season_id=1, is_current=False, current_round_number=1, year=2026),
            Season(season_id=2, is_current=True, current_round_number=1, year=2026),
        ],
        rounds_by_season={},
    )
    assert recommend_mapping(client, bbbffl_year=2026, bbbffl_sequence=1, bbbffl_stream_type="ordinary") is None


def test_recommend_mapping_returns_none_for_ambiguous_multi_round_match():
    client = RecommendationEvidence(
        seasons=[Season(season_id=85, is_current=True, current_round_number=1, year=2026)],
        rounds_by_season={85: [Round(round_id=1, round_number=1), Round(round_id=2, round_number=1)]},
    )
    assert recommend_mapping(client, bbbffl_year=2026, bbbffl_sequence=1, bbbffl_stream_type="ordinary") is None


def test_recommend_mapping_returns_none_without_get_seasons_support():
    assert (
        recommend_mapping(NoSeasonListing(), bbbffl_year=2026, bbbffl_sequence=1, bbbffl_stream_type="ordinary") is None
    )


def test_recommend_mapping_returns_none_when_evidence_unavailable():
    client = RecommendationEvidence(seasons=[], rounds_by_season={}, error=RuntimeError("afl-api unavailable"))
    assert recommend_mapping(client, bbbffl_year=2026, bbbffl_sequence=1, bbbffl_stream_type="ordinary") is None


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
    mappings.propose(
        ambiguous.bbbffl_round_id,
        state="ambiguous",
        afl_season_id=84,
        afl_round_id=1390,
        reason="Opening Round/deferred-bye treatment is not fully evidenced",
    )
    assert mappings.resolve(unresolved.bbbffl_round_id) is None
    assert mappings.resolve(ambiguous.bbbffl_round_id) is None


def test_revised_proposal_audit_preserves_before_and_after(domain):
    database, mappings, make = domain
    round_ = make(2026)
    first = mappings.propose(round_.bbbffl_round_id, state="unresolved")
    mappings.propose(
        round_.bbbffl_round_id,
        state="ambiguous",
        afl_season_id=84,
        afl_round_id=1390,
        reason="Two historical interpretations remain",
    )
    events = AuditEventRepository(database).list_events(entity_type="round.afl_mapping", entity_id=first.mapping_id)
    assert events[-1].before_state == {
        "state": "unresolved",
        "afl_season_id": None,
        "afl_round_id": None,
    }
    assert events[-1].after_state == {
        "state": "ambiguous",
        "afl_season_id": 84,
        "afl_round_id": 1390,
    }


def test_ordinary_and_superscore_independently_share_afl_context(domain):
    _, mappings, make = domain
    ordinary = make(2027, round_key="final-1")
    superscore = make(2027, "superscore", "superscore", "ss1")
    known = KnownRounds((85, 1412))
    first = mappings.accept(ordinary.bbbffl_round_id, 85, 1412, known)
    second = mappings.accept(superscore.bbbffl_round_id, 85, 1412, known)
    assert first.mapping_id != second.mapping_id
    assert (
        mappings.resolve(ordinary.bbbffl_round_id).afl_round_id
        == mappings.resolve(superscore.bbbffl_round_id).afl_round_id
    )


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
    corrected = mappings.correct(
        round_.bbbffl_round_id, 84, 1301, known, reason="Official AFL round identity corrected"
    )
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
