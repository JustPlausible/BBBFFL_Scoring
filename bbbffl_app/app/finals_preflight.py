"""Issue #190: stream-aware preflight/open-round adapter for a finals week.

Mirrors `app.round_preflight`'s shape (`build_round_preflight`/
`open_preflight_round`), but built for a finals week's variable match count
and results-derived (not pre-drawn) pairing. The ordinary adapter cannot be
reused as-is: `open_preflight_round` calls `create_ordinary_round` directly,
and `build_round_preflight` hard-requires exactly five frozen fixture
matchups -- neither holds for a finals week (see `app.finals`'s module
docstring). This module is the finals-specific sibling issue #190 requires,
not a modification of `app.round_preflight`."""

from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import SLOT_LABELS, WEEK_LABELS, FinalsBracketAdvanceStateError, FinalsBracketRepository
from app.round_mapping import RoundMappingRepository

__all__ = ["build_finals_week_preflight", "open_finals_week"]


def build_finals_week_preflight(database, bracket_id: str, week_number: int) -> dict:
    """Human-facing, fail-closed read model for opening one finals week.
    Never mutates. Mirrors `app.round_preflight.build_round_preflight`'s
    `readiness.safe_to_open`/`blockers` shape so an operator UI can reuse
    the same rendering convention for both."""
    bracket_repo = FinalsBracketRepository(database)
    bracket = bracket_repo.get_bracket_by_id(bracket_id)
    if bracket is None:
        raise KeyError(bracket_id)
    round_id = bracket_repo.get_week_round_id(bracket_id, week_number)
    lifecycle = CompetitionLifecycleRepository(database)
    mapping = RoundMappingRepository(database).resolve(round_id)
    pairings = bracket_repo.list_pairings(bracket_id, week_number=week_number)
    persisted = lifecycle.get_round(round_id)
    state = persisted.state if persisted else "not_created"

    blockers = []
    if mapping is None:
        blockers.append(
            {
                "code": "mapping_missing",
                "message": "No authoritative AFL mapping has been accepted for this finals week.",
            }
        )
    if not pairings:
        blockers.append(
            {
                "code": "pairing_missing",
                "message": f"Finals week {week_number} has no pairing yet; advance the bracket from the prior "
                "week's result(s) first.",
            }
        )
    advisories = []
    if persisted and state != "upcoming":
        advisories.append(
            {
                "code": "already_opened",
                "message": f"This finals week is already {state}; its persisted lifecycle is authoritative.",
            }
        )

    return {
        "bracket_id": bracket_id,
        "week_number": week_number,
        "label": WEEK_LABELS[week_number],
        "round_id": round_id,
        "mapping": mapping.__dict__ if mapping else None,
        "pairings": [
            {
                "slot": pairing.slot,
                "slot_label": SLOT_LABELS[pairing.slot],
                "home_season_entry_id": pairing.home_season_entry_id,
                "away_season_entry_id": pairing.away_season_entry_id,
                "matchup_id": pairing.matchup_id,
            }
            for pairing in pairings
        ],
        "round_state": state,
        "readiness": {
            "safe_to_open": not blockers and state in ("not_created", "upcoming"),
            "blockers": blockers,
            "advisories": advisories,
        },
    }


def open_finals_week(database, bracket_id: str, week_number: int, *, actor):
    """The finals equivalent of `app.round_preflight.open_preflight_round`:
    re-checks the same preflight this module's own read model reports
    before ever mutating, then delegates the actual lifecycle-creation/
    materialisation/open-transition work to
    `FinalsBracketRepository.open_finals_week`."""
    preflight = build_finals_week_preflight(database, bracket_id, week_number)
    if not preflight["readiness"]["safe_to_open"]:
        raise FinalsBracketAdvanceStateError(
            f"finals week {week_number} failed preflight and was not opened: {preflight['readiness']['blockers']}"
        )
    return FinalsBracketRepository(database).open_finals_week(bracket_id, week_number, actor=actor)
