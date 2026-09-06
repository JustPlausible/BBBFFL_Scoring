"""Route-orchestration regressions for issue #117's delegated surface."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.authorization import Principal, Role
from app.identity import IdentityRepository
from app.lineups import WeeklyLineupRepository
from app.main import opening_round_error_handler
from app.opening_round import OpeningRoundError
from app.routes import delegated_operations
from tests.test_carry_forward import acquire_players, context, submit_round


@pytest.mark.parametrize(
    "persisted_positions,visible_positions,persisted_revision",
    [
        ({"F1": "draft-a"}, {"F1": "draft-b"}, 7),
        ({}, {"F1": "first-visible-selection"}, 1),
    ],
)
def test_submit_saves_and_submits_exact_visible_positions(
    monkeypatch, persisted_positions, visible_positions, persisted_revision
):
    """Direct Submit must never authorise the older persisted draft."""
    calls = []
    resulting_draft = SimpleNamespace(lineup_id="lineup-1", revision=persisted_revision + 1)

    class Proxy:
        def __init__(self, database, afl_client):
            pass

        def create_or_amend(self, season_id, competition_id, round_id, entry_id, positions, **kwargs):
            calls.append(("save", dict(positions), kwargs["expected_revision"], kwargs["actor"]))
            return resulting_draft

        def submit(self, lineup_id, **kwargs):
            calls.append(("submit", lineup_id, kwargs["expected_draft_revision"], kwargs["actor"]))

    scope = {
        "season_id": "season-a",
        "competition_id": "competition-a",
        "bbbffl_round_id": "round-a",
        "season_entry_id": "entry-a",
    }
    monkeypatch.setattr(delegated_operations, "LineupProxyService", Proxy)
    monkeypatch.setattr(delegated_operations, "_scope", lambda *args: scope)
    monkeypatch.setattr(delegated_operations, "_csrf", lambda *args: None)
    monkeypatch.setattr(delegated_operations, "_guard", lambda *args: "guard")
    monkeypatch.setattr(delegated_operations, "_lineup_view", lambda *args: {"ok": True})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=object(), afl_client=object())))
    principal = Principal(Role.REPLAY_OPERATOR, coach_id="operator-1")
    payload = delegated_operations.SubmitRequest(
        positions=visible_positions,
        expected_draft_revision=persisted_revision,
        expected_submission_version=2,
        reason="reviewed visible selections",
    )

    assert delegated_operations.submit("round-a", payload, request, principal) == {"ok": True}
    assert calls[0][0:3] == ("save", visible_positions, persisted_revision)
    assert calls[1][0:3] == ("submit", "lineup-1", persisted_revision + 1)
    assert calls[0][1] != persisted_positions or visible_positions == persisted_positions
    assert calls[0][3].actor_id == "operator-1"
    assert calls[1][3].actor_id == "operator-1"


def test_opening_round_domain_conflict_has_controlled_http_409_response():
    response = asyncio.run(
        opening_round_error_handler(SimpleNamespace(), OpeningRoundError("target slot M1 is already nominated"))
    )
    assert response.status_code == 409
    assert json.loads(response.body) == {"detail": "target slot M1 is already nominated"}


def test_lineup_view_names_released_carry_forward_player_without_making_it_selectable():
    db, _, rounds, entries, scope_row, pool, ownership = context(rounds=2)
    entry = entries[0]
    released_player, current_player = acquire_players(pool, ownership, scope_row, entry, 1, 2)
    submit_round(
        WeeklyLineupRepository(db),
        scope_row,
        rounds[0],
        entry,
        {"F1": released_player.season_player_id},
    )
    ownership.release(released_player.season_player_id)
    represented_team = IdentityRepository(db).get_public_team(entry.season_entry_id)
    assert represented_team is not None

    scope = {
        **dict(scope_row),
        "bbbffl_round_id": rounds[1],
        "season_entry_id": entry.season_entry_id,
        "team_name": represented_team.team_name,
        "season_label": "Test season",
        "round_label": "Round 2",
        "sequence": 2,
    }
    # No AFL match evidence at all for round 2 -- issue #138's read model
    # must still fail closed (indeterminate) rather than raising, exactly
    # like the Coach page's `view()` does for the same gap.
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(database=db, afl_client=SimpleNamespace(get_matches=lambda _id: [])))
    )
    principal = Principal(
        Role.REPLAY_OPERATOR,
        coach_id="operator-1",
        display_name="Replay Operator",
        represented_season_entry_id=entry.season_entry_id,
    )

    view = delegated_operations._lineup_view(request, principal, scope)

    assert view["carry_forward_source"]["positions"]["F1"] == released_player.season_player_id
    assert view["player_display_names"][released_player.season_player_id] == released_player.display_name
    assert released_player.season_player_id not in {player["season_player_id"] for player in view["players"]}
    assert current_player.season_player_id in {player["season_player_id"] for player in view["players"]}
    # Issue #138: the same authoritative lock-state read model as the Coach
    # page is materialised for every ordinary position -- round 2's fresh
    # draft has no selections yet, so each is a deliberate vacancy,
    # reported editable/"empty" rather than omitted or guessed.
    lock_by_position = {row["position"]: row for row in view["lock_state"]}
    assert set(lock_by_position) == set(delegated_operations.COACH_LINEUP_POSITIONS)
    assert lock_by_position["F1"]["state"] == "editable"
    assert lock_by_position["F1"]["lock_type"] == "vacant"
    assert lock_by_position["F1"]["reason_code"] == "empty"
