"""Issue #211 workflow improvement D: `GET /api/context/entries` (the
delegated "Switch represented team" selector's data source) must narrow to
a finals week's actual active participants when a `round_id` is supplied,
excluding a bye seed (shown as context/status elsewhere, never as a
lineup-submission task) -- but must keep listing every eligible entry for a
SuperScore round or an ordinary round, using the existing
`app.finals_participation.list_round_participant_entry_ids` fail-closed
read model, never a second UI-only eligibility rule."""

from types import SimpleNamespace

from app.authorization import Principal, Role
from app.finals_participation import list_round_participant_entry_ids
from app.routes import context as context_routes
from tests.test_carry_forward import context as ordinary_context
from tests.test_coach_lineup_finals_superscore import _open_finals_week1, _open_superscore1


def _entry_view(entry):
    return SimpleNamespace(season_entry_id=entry.season_entry_id, team_name="Team", coach_display_name="Coach")


class _StubActingContext:
    def __init__(self, entries):
        self._entries = entries

    def representable_entries(self, coach_id, role, season_id):
        return self._entries


def _request(database, entries):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(database=database, acting_context=_StubActingContext(entries)))
    )


def _principal():
    return Principal(Role.ADMIN, coach_id="operator-1", display_name="Operator", session_id="s1")


def test_finals_round_narrows_to_active_participants_excluding_the_bye():
    built = _open_finals_week1()
    database = built["database"]
    all_entries = [_entry_view(e) for e in built["entries"]]
    request = _request(database, all_entries)

    result = context_routes.representable_entries(
        built["season"].season_id, request, built["week1_round_id"], _principal()
    )

    participant_ids = set(list_round_participant_entry_ids(database, built["week1_round_id"]))
    returned_ids = {row["season_entry_id"] for row in result}
    assert returned_ids == participant_ids
    # Confirms real narrowing actually happened, not a no-op passthrough.
    assert returned_ids != {e.season_entry_id for e in built["entries"]}
    assert len(returned_ids) < len(built["entries"])


def test_superscore_round_still_lists_all_ten_entries():
    built = _open_superscore1()
    database = built["database"]
    all_entries = [_entry_view(e) for e in built["entries"]]
    request = _request(database, all_entries)

    result = context_routes.representable_entries(
        built["season"].season_id, request, built["ss1_round_id"], _principal()
    )

    assert {row["season_entry_id"] for row in result} == {e.season_entry_id for e in built["entries"]}
    assert len(result) == 10


def test_ordinary_round_behaviour_is_unchanged():
    db, _, rounds, entries, _scope, _pool, _ownership = ordinary_context(rounds=1)
    all_entries = [_entry_view(e) for e in entries]
    request = _request(db, all_entries)
    season_id = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (rounds[0],),
    ).fetchone()["season_id"]

    result = context_routes.representable_entries(season_id, request, rounds[0], _principal())
    assert {row["season_entry_id"] for row in result} == {e.season_entry_id for e in entries}


def test_no_round_id_behaves_exactly_as_before():
    built = _open_finals_week1(2410)
    database = built["database"]
    all_entries = [_entry_view(e) for e in built["entries"]]
    request = _request(database, all_entries)

    result = context_routes.representable_entries(built["season"].season_id, request, None, _principal())
    assert {row["season_entry_id"] for row in result} == {e.season_entry_id for e in built["entries"]}
