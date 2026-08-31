"""Full synthetic ten-entry scorer-operated draft, driven entirely through
the real HTTP admin API (roadmap package 14, issue #53) -- proves the whole
domain workflow (board, search, proxy picks, stale-turn/ownership
rejection, traded-pick ownership, pause/resume, correction, and guarded
finalisation) works end-to-end, not merely that a page renders.

Uses its own isolated SQLite database (never the default/production path)
and its own season, so it cannot contaminate any other season's state.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditEventRepository
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.season import SeasonRepository

ENTRIES = 10
SQUAD_LIMIT = 3
TOTAL_PICKS = ENTRIES * SQUAD_LIMIT


@pytest.fixture
def synthetic_draft_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    # `app.main.app`'s lifespan re-reads settings/env and rebuilds
    # `app.state` on every `with TestClient(app):` entry, so the same
    # module-level app object is safe to reuse here -- no need to (and
    # importantly, must not) `importlib.reload(app.main)`, which would
    # mint new exception classes and break `isinstance`/`except` matching
    # for any other test module that already imported the old ones (see
    # tests/test_startup.py's ReplayModeNotWiredError checks).
    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _seed_season(database):
    season = SeasonRepository(database).create_season(2098, "Synthetic 10-team replay")
    identities = IdentityRepository(database)
    entries = [
        identities.create_entry(
            season.season_id,
            f"synthetic-{number}",
            identities.create_coach(f"Synthetic Coach {number}").coach_id,
            f"Synthetic Team {number}",
        )
        for number in range(ENTRIES)
    ]
    OwnershipRepository(database).configure_squad_limit(season.season_id, SQUAD_LIMIT)
    pool = PlayerPoolRepository(database)
    recognisable = [
        (1001, "Marcus Bontempelli", 7, "Western Bulldogs"),
        (1002, "Nick Daicos", 4, "Collingwood"),
        (1003, "Patrick Cripps", 3, "Carlton"),
    ]
    players = [
        pool.refresh_player(season.season_id, canonical_id, name, afl_team_id=team_id, afl_team_name=team)
        for canonical_id, name, team_id, team in recognisable
    ]
    players.extend(
        pool.refresh_player(season.season_id, number + 2000, f"Synthetic Player {number:03d}")
        for number in range(TOTAL_PICKS + 12)
    )
    ineligible = pool.refresh_player(season.season_id, 9999, "Identity unresolved", eligible=False)
    return season, entries, players, ineligible


def test_full_ten_entry_draft_runs_through_finalisation_via_the_admin_api(synthetic_draft_client):
    client = synthetic_draft_client
    database = client.app.state.database
    season, entries, players, ineligible = _seed_season(database)
    from app.draft import DraftRepository

    draft_id = DraftRepository(database).accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    api = f"/api/admin/draft/{season.season_id}"

    def board():
        response = client.get(f"{api}/board")
        assert response.status_code == 200
        return response.json()

    def draft_next(scorer_name="Steve the Scorer"):
        current = board()["current_pick"]
        available = client.get(f"{api}/available-players").json()
        chosen = available[0]
        response = client.post(
            f"{api}/pick",
            json={
                "season_entry_id": current["current_season_entry_id"],
                "season_player_id": chosen["season_player_id"],
                "scorer_name": scorer_name,
            },
        )
        assert response.status_code == 200, response.text
        return current, chosen

    # -- Correct first pick resolves to the accepted order's first entry.
    initial = board()
    assert initial["status"]["total_picks"] == TOTAL_PICKS
    assert initial["status"]["completed_picks"] == 0
    assert initial["current_pick"]["current_season_entry_id"] == entries[0].season_entry_id
    assert initial["order"][0]["season_entry_id"] == entries[0].season_entry_id

    # -- Issue #101's player-pool browser is a season-scoped joined view of
    # real player facts and ownership, not a second availability ledger.
    page = client.get(f"/admin/draft/{season.season_id}")
    assert page.status_code == 200
    assert "Season player pool" in page.text
    assert "if (token) headers['X-Admin-Token'] = token" in page.text
    assert "'X-Admin-Token': getToken()" not in page.text
    scorer_page = client.get(
        f"/admin/draft/{season.season_id}",
        headers={"X-Admin-Token": "legacy-operator", "X-Authority-Role": "scorer"},
    )
    assert scorer_page.status_code == 200

    # Season Centre must not advertise a scorer-only workflow to a
    # Secretary, whose player-pool read capability alone is insufficient.
    from app.authorization import Principal, Role
    from app.routes.season_centre import _season_view

    secretary_view = _season_view(client.app.state, season.season_id, Principal(Role.SECRETARY))
    admin_view = _season_view(client.app.state, season.season_id, Principal(Role.ADMIN))
    assert secretary_view["links"]["draft"] is None
    assert admin_view["links"]["draft"] == f"/admin/draft/{season.season_id}"
    searched = client.get(f"{api}/players", params={"q": "marcus bont"}).json()
    assert len(searched) == 1
    assert searched[0]["display_name"] == "Marcus Bontempelli"
    assert searched[0]["canonical_player_id"] == 1001
    assert searched[0]["afl_team_name"] == "Western Bulldogs"
    assert searched[0]["availability"] == "available"
    unresolved = client.get(f"{api}/players", params={"availability": "unresolved"}).json()
    assert unresolved == [
        {
            "season_player_id": ineligible.season_player_id,
            "season_id": season.season_id,
            "canonical_player_id": 9999,
            "display_name": "Identity unresolved",
            "afl_team_id": None,
            "afl_team_name": None,
            "eligible": False,
            "availability": "unresolved",
            "owner_season_entry_id": None,
            "owner_team_name": None,
            "diagnostic": "Not selectable: season player identity or eligibility requires investigation",
        }
    ]

    other_season = SeasonRepository(database).create_season(2097, "Other pool")
    other_player = PlayerPoolRepository(database).refresh_player(other_season.season_id, 1001, "Wrong-season Marcus")
    scoped_ids = {item["season_player_id"] for item in client.get(f"{api}/players").json()}
    assert other_player.season_player_id not in scoped_ids

    # -- The ineligible player never appears in search results.
    available_names = {player["display_name"] for player in client.get(f"{api}/available-players").json()}
    assert ineligible.display_name not in available_names

    # -- A proxy pick for the wrong team is rejected without advancing the turn.
    wrong_response = client.post(
        f"{api}/pick",
        json={"season_entry_id": entries[1].season_entry_id, "season_player_id": players[0].season_player_id},
    )
    assert wrong_response.status_code == 409
    assert board()["status"]["completed_picks"] == 0

    # -- Pick #1, with proxy provenance recorded.
    first_pick, first_player = draft_next(scorer_name="Steve the Scorer")
    assert board()["status"]["completed_picks"] == 1
    events = AuditEventRepository(database).list_events(action="draft.pick.completed")
    assert events[-1].actor_role == "scorer" and events[-1].actor_id == "Steve the Scorer"
    claimed = client.get(f"{api}/players", params={"q": first_player["canonical_player_id"]}).json()[0]
    assert claimed["availability"] == "owned"
    assert claimed["owner_season_entry_id"] == entries[0].season_entry_id
    assert claimed["owner_team_name"] == "Synthetic Team 0"

    # -- Picking an already-owned player is rejected without advancing the turn.
    completed_count = board()["status"]["completed_picks"]
    reuse_response = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": board()["current_pick"]["current_season_entry_id"],
            "season_player_id": first_player["season_player_id"],
        },
    )
    assert reuse_response.status_code == 409
    assert board()["status"]["completed_picks"] == completed_count

    # -- Pick #2, #3: advance the snake order normally.
    draft_next()
    draft_next()

    # -- Pause, verify picking is rejected, resume from the same turn.
    before_pause = board()["current_pick"]
    pause_response = client.post(f"{api}/pause", json={"reason": "half-time break"})
    assert pause_response.status_code == 200
    assert board()["status"]["paused_at"] is not None
    paused_pick_response = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": before_pause["current_season_entry_id"],
            "season_player_id": players[10].season_player_id,
        },
    )
    assert paused_pick_response.status_code == 409
    resume_response = client.post(f"{api}/resume", json={})
    assert resume_response.status_code == 200
    assert board()["status"]["paused_at"] is None
    assert board()["current_pick"] == before_pause

    # -- Pick #4, deliberately wrong, then correct it and re-select.
    erroneous_pick, erroneous_player = draft_next()
    correction_response = client.post(
        f"{api}/correct",
        json={"draft_pick_id": erroneous_pick["draft_pick_id"], "reason": "scorer mis-clicked the wrong player"},
    )
    assert correction_response.status_code == 200
    reopened_board = board()
    # The reopened pick is a *new* row for the same slot (a correction never
    # rewrites the original, immutable row -- see app.draft.correct_pick's
    # docstring), so its draft_pick_id differs; overall_number is what
    # identifies "the same slot" across a correction.
    assert reopened_board["current_pick"]["draft_pick_id"] != erroneous_pick["draft_pick_id"]
    assert reopened_board["current_pick"]["overall_number"] == erroneous_pick["overall_number"]
    assert reopened_board["status"]["completed_picks"] == 3
    assert reopened_board["corrections"][0]["reason"] == "scorer mis-clicked the wrong player"
    # The erroneously-selected player is available again.
    reavailable_players = client.get(f"{api}/available-players").json()
    reavailable_ids = {player["season_player_id"] for player in reavailable_players}
    assert erroneous_player["season_player_id"] in reavailable_ids
    # Deliberately pick a *different* player this time to prove the
    # correction produced a genuinely different, deliberate selection.
    corrected_player = next(
        p for p in reavailable_players if p["season_player_id"] != erroneous_player["season_player_id"]
    )
    corrected_response = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": reopened_board["current_pick"]["current_season_entry_id"],
            "season_player_id": corrected_player["season_player_id"],
            "scorer_name": "Steve the Scorer",
        },
    )
    assert corrected_response.status_code == 200, corrected_response.text
    corrected_pick_row = next(
        p for p in board()["completed_picks"] if p["overall_number"] == erroneous_pick["overall_number"]
    )
    assert corrected_pick_row["selected_season_player_id"] == corrected_player["season_player_id"]

    # -- A trade changes the effective owner of an upcoming pick; the board
    # shows both the original and current (traded) team. Squad capacity is
    # frozen to the configured squad size (roadmap package 13), so a single
    # one-way transfer would leave the receiving entry unable to complete
    # its draft -- this swaps a pair of upcoming picks between two entries
    # instead, a realistic even trade that keeps every entry's eventual
    # total at exactly SQUAD_LIMIT.
    upcoming = board()["upcoming_picks"]
    upcoming_before_trade = upcoming[0]
    trade_target_entry_id = next(
        pick["current_season_entry_id"]
        for pick in upcoming
        if pick["current_season_entry_id"] != upcoming_before_trade["current_season_entry_id"]
    )
    trade_target = next(entry for entry in entries if entry.season_entry_id == trade_target_entry_id)
    reciprocal_pick = next(pick for pick in upcoming if pick["current_season_entry_id"] == trade_target_entry_id)
    draft_repository = DraftRepository(database)
    draft_repository.transfer_pick(
        upcoming_before_trade["draft_pick_id"], trade_target.season_entry_id, reason="synthetic trade"
    )
    draft_repository.transfer_pick(
        reciprocal_pick["draft_pick_id"],
        upcoming_before_trade["current_season_entry_id"],
        reason="synthetic trade (return leg)",
    )
    traded_board = board()
    traded_pick = next(
        p for p in traded_board["upcoming_picks"] if p["draft_pick_id"] == upcoming_before_trade["draft_pick_id"]
    )
    assert traded_pick["traded"] is True
    assert traded_pick["current_season_entry_id"] == trade_target.season_entry_id
    assert traded_pick["original_season_entry_id"] == upcoming_before_trade["current_season_entry_id"]
    assert (
        traded_pick["current_team_name"]
        == IdentityRepository(database).get_public_team(trade_target.season_entry_id).team_name
    )

    # -- Finalisation is blocked while picks remain.
    early_finalize = client.post(f"{api}/finalize", json={})
    assert early_finalize.status_code == 409

    # -- Run the remaining picks to completion, respecting the trade.
    while board()["current_pick"] is not None:
        draft_next()

    completed_board = board()
    assert completed_board["status"]["total_picks"] == TOTAL_PICKS
    assert completed_board["status"]["completed_picks"] == TOTAL_PICKS
    assert len(completed_board["completed_picks"]) == TOTAL_PICKS
    traded_completed = next(
        p for p in completed_board["completed_picks"] if p["draft_pick_id"] == upcoming_before_trade["draft_pick_id"]
    )
    assert traded_completed["current_season_entry_id"] == trade_target.season_entry_id
    assert traded_completed["traded"] is True

    # -- Explicit finalisation.
    finalize_response = client.post(f"{api}/finalize", json={"note": "synthetic ten-team draft complete"})
    assert finalize_response.status_code == 200
    finalized_board = board()
    assert finalized_board["status"]["finalized_at"] is not None
    assert finalized_board["status"]["finalized_note"] == "synthetic ten-team draft complete"

    # -- Ordinary controls no longer mutate the finalised draft.
    post_finalize_pick = client.post(
        f"{api}/pick",
        json={"season_entry_id": entries[0].season_entry_id, "season_player_id": players[-1].season_player_id},
    )
    assert post_finalize_pick.status_code == 423
    post_finalize_pause = client.post(f"{api}/pause", json={})
    assert post_finalize_pause.status_code == 423
    post_finalize_correction = client.post(
        f"{api}/correct", json={"draft_pick_id": completed_board["completed_picks"][0]["draft_pick_id"]}
    )
    assert post_finalize_correction.status_code == 423

    # -- Complete pick history remains available and internally consistent:
    # every entry ends with exactly the configured squad size, and every
    # completed pick still resolves a real player selection.
    final_history = board()
    assert len(final_history["completed_picks"]) == TOTAL_PICKS
    squad_sizes = {}
    for pick in final_history["completed_picks"]:
        squad_sizes[pick["current_season_entry_id"]] = squad_sizes.get(pick["current_season_entry_id"], 0) + 1
        assert pick["selected_player_name"]
    assert set(squad_sizes.values()) == {SQUAD_LIMIT}
    assert len(squad_sizes) == ENTRIES

    # -- Reopening requires the deliberate confirmation phrase, not an
    # ordinary control -- a bare reopen with the wrong/missing phrase fails.
    bad_reopen = client.post(f"{api}/reopen", json={"reason": "audit follow-up", "confirm": "nope"})
    assert bad_reopen.status_code == 400
    assert board()["status"]["finalized_at"] is not None
    good_reopen = client.post(f"{api}/reopen", json={"reason": "audit follow-up", "confirm": "REOPEN FINALIZED DRAFT"})
    assert good_reopen.status_code == 200
    assert board()["status"]["finalized_at"] is None

    # -- draft_id, ownership periods, and the full audit trail all persisted.
    assert draft_id
    ownership = OwnershipRepository(database)
    for entry in entries:
        assert len(ownership.squad_at(entry.season_entry_id, "9999-12-31")) == SQUAD_LIMIT
    completion_events = AuditEventRepository(database).list_events(action="draft.pick.completed")
    assert len(completion_events) == TOTAL_PICKS + 1  # one corrected pick re-selected once
