"""Focused domain/read-model coverage for issue #105's operator preflight."""

from app.afl_client import Match, Team
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.identity import IdentityRepository
from app.lockouts import LockoutTriggerRepository
from app.round_mapping import RoundMappingRepository
from app.round_preflight import build_round_preflight
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import configured


class Evidence:
    def __init__(self, matches):
        self.matches = matches

    def get_matches(self, round_id):
        return self.matches


def _match(match_id=9001):
    return Match(match_id, Team(1, "Carlton"), Team(2, "Richmond"), "UPCOMING", "2026-03-12T08:30:00+00:00")


def _view(db, round_, matches=(_match(),)):
    return build_round_preflight(
        db, CompetitionLifecycleRepository(db), IdentityRepository(db), Evidence(list(matches)), round_.bbbffl_round_id
    )


def test_valid_round_represents_five_named_matchups_afl_evidence_and_match_based_lockout():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 2, [9001])
    view = _view(db, round_)
    assert view["readiness"]["safe_to_open"] is True
    assert len(view["fixture_matchups"]) == 5
    assert view["fixture_matchups"][0]["home_team_name"].startswith("Team")
    assert view["afl_matches"][0]["home_team"] == "Carlton"
    assert view["lockout_triggers"][0]["activating_matches"][0]["match_id"] == 9001
    assert view["lockout_triggers"][0]["scope"] == "All remaining selections"
    assert view["readiness"]["advisories"]  # absence of selective stage is advisory, not blocker


def test_missing_or_ambiguous_mapping_and_invalid_lockout_evidence_fail_closed():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    unmapped = SeasonRepository(db).create_round(round_.competition_id, "round-2", "Round 2", 2)
    missing = _view(db, unmapped)
    assert missing["readiness"]["safe_to_open"] is False
    assert "mapping_missing" in {item["code"] for item in missing["readiness"]["blockers"]}
    RoundMappingRepository(db).propose(
        unmapped.bbbffl_round_id, state="ambiguous", afl_season_id=2026, afl_round_id=101
    )
    ambiguous = _view(db, unmapped)
    assert "mapping_unresolved" in {item["code"] for item in ambiguous["readiness"]["blockers"]}


def test_main_trigger_must_resolve_to_mapped_afl_match_and_opening_state_is_not_manufactured():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9999])
    view = _view(db, round_)
    codes = {item["code"] for item in view["readiness"]["blockers"]}
    assert "lockout_match_unresolved" in codes
    assert view["opening_round"] == {"applies": False, "deferred_selections": []}
