"""Season Centre driven through the real HTTP admin API and page routes
(issue #100) -- proves the workflow (reach the page, create a season,
create coaches/entries, edit team names/coach assignments, see readiness)
works end-to-end through `app.main.app`, not merely that the underlying
repositories do. Mirrors `tests/test_preseason_api.py`'s isolated-database
fixture pattern.
"""

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.opening_round import OpeningRoundRuleRepository
from tests.test_competition_lifecycle import configured

ADMIN = ActorContext.anonymous_operator("admin")


class _AnyRoundExists:
    def round_exists(self, season, round_):
        return True


@pytest.fixture
def season_centre_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def test_season_centre_reachable_from_a_clean_database_for_the_2026_replay_season(season_centre_client):
    client = season_centre_client

    created = client.post("/api/admin/season-centre/seasons", json={"year": 2026, "label": "2026 Replay"})
    assert created.status_code == 200
    season_id = created.json()["season_id"]

    page = client.get(f"/admin/season-centre/{season_id}")
    assert page.status_code == 200
    assert "Season Centre" in page.text

    centre = client.get(f"/api/admin/season-centre/{season_id}")
    assert centre.status_code == 200
    body = centre.json()
    assert body["season"]["year"] == 2026
    assert body["entries"] == []
    assert body["readiness"]["entries_established"] == 0


def test_season_index_page_is_reachable(season_centre_client):
    page = season_centre_client.get("/admin/season-centre")
    assert page.status_code == 200
    assert "Season Centre" in page.text


def test_operator_can_establish_ten_replay_entries_without_sql_via_the_api(season_centre_client):
    client = season_centre_client
    season_id = client.post("/api/admin/season-centre/seasons", json={"year": 2026, "label": "2026 Replay"}).json()[
        "season_id"
    ]

    for letter in "ABCDEFGHIJ":
        coach_id = client.post("/api/admin/season-centre/coaches", json={"display_name": f"Coach {letter}"}).json()[
            "coach_id"
        ]
        response = client.post(
            f"/api/admin/season-centre/{season_id}/entries",
            json={"coach_id": coach_id, "team_name": f"BBBFFL Team {letter}"},
        )
        assert response.status_code == 200, response.text

    centre = client.get(f"/api/admin/season-centre/{season_id}").json()
    assert len(centre["entries"]) == 10
    assert centre["readiness"]["entries_established"] == 10
    assert centre["readiness"]["distinct_coaches"] == 10


def test_rename_team_and_transfer_coach_persist_and_keep_the_entry_id_stable(season_centre_client):
    client = season_centre_client
    season_id = client.post("/api/admin/season-centre/seasons", json={"year": 2027, "label": "2027"}).json()[
        "season_id"
    ]
    coach_a = client.post("/api/admin/season-centre/coaches", json={"display_name": "Coach A"}).json()
    coach_b = client.post("/api/admin/season-centre/coaches", json={"display_name": "Coach B"}).json()
    entry = client.post(
        f"/api/admin/season-centre/{season_id}/entries",
        json={"coach_id": coach_a["coach_id"], "team_name": "Original Team"},
    ).json()
    # create_entry's response is the full rebuilt Season Centre view.
    entry_id = entry["entries"][0]["season_entry_id"]

    renamed = client.post(f"/api/admin/season-centre/entries/{entry_id}/team-name", json={"team_name": "Renamed Team"})
    assert renamed.status_code == 200
    assert renamed.json()["entries"][0]["team_name"] == "Renamed Team"
    assert renamed.json()["entries"][0]["season_entry_id"] == entry_id

    transferred = client.post(
        f"/api/admin/season-centre/entries/{entry_id}/coach", json={"coach_id": coach_b["coach_id"]}
    )
    assert transferred.status_code == 200
    view = transferred.json()["entries"][0]
    assert view["season_entry_id"] == entry_id
    assert view["coach_display_name"] == "Coach B"
    assert view["team_name"] == "Renamed Team"


def test_validation_error_returns_400_not_a_raw_500(season_centre_client):
    client = season_centre_client
    season_id = client.post("/api/admin/season-centre/seasons", json={"year": 2027, "label": "2027"}).json()[
        "season_id"
    ]
    coach = client.post("/api/admin/season-centre/coaches", json={"display_name": "Coach"}).json()

    blank_name = client.post(
        f"/api/admin/season-centre/{season_id}/entries", json={"coach_id": coach["coach_id"], "team_name": "   "}
    )
    assert blank_name.status_code == 400

    unknown_coach = client.post(
        f"/api/admin/season-centre/{season_id}/entries", json={"coach_id": "missing", "team_name": "A Team"}
    )
    assert unknown_coach.status_code == 400

    duplicate_year = client.post("/api/admin/season-centre/seasons", json={"year": 2027, "label": "dup"})
    assert duplicate_year.status_code == 400


def test_unknown_season_returns_404(season_centre_client):
    response = season_centre_client.get("/api/admin/season-centre/does-not-exist")
    assert response.status_code == 404


def test_season_centre_endpoints_require_admin_authority_not_merely_scorer(season_centre_client):
    """Every Season Centre mutation/read here uses `require_admin`, the same
    strict-admin dependency `app.routes.draft`/`app.routes.preseason` use for
    their own setup endpoints (never the looser `require_scorer_or_admin`) --
    narrowing the shared operator token to the `scorer` authority (see
    `app.authorization.resolve_principal`) must be refused."""
    client = season_centre_client
    season_id = client.post("/api/admin/season-centre/seasons", json={"year": 2027, "label": "2027"}).json()[
        "season_id"
    ]
    scorer_headers = {"X-Admin-Token": "open-mode", "X-Authority-Role": "scorer"}
    response = client.get(f"/api/admin/season-centre/{season_id}", headers=scorer_headers)
    assert response.status_code == 403


def test_2026_replay_and_2027_season_entries_stay_separated_over_http(season_centre_client):
    client = season_centre_client
    replay_id = client.post("/api/admin/season-centre/seasons", json={"year": 2026, "label": "2026 Replay"}).json()[
        "season_id"
    ]
    live_id = client.post("/api/admin/season-centre/seasons", json={"year": 2027, "label": "2027"}).json()["season_id"]
    coach = client.post("/api/admin/season-centre/coaches", json={"display_name": "Shared Coach"}).json()
    client.post(
        f"/api/admin/season-centre/{replay_id}/entries",
        json={"coach_id": coach["coach_id"], "team_name": "Replay Team"},
    )
    client.post(
        f"/api/admin/season-centre/{live_id}/entries", json={"coach_id": coach["coach_id"], "team_name": "Live Team"}
    )

    replay_centre = client.get(f"/api/admin/season-centre/{replay_id}").json()
    live_centre = client.get(f"/api/admin/season-centre/{live_id}").json()
    assert [e["team_name"] for e in replay_centre["entries"]] == ["Replay Team"]
    assert [e["team_name"] for e in live_centre["entries"]] == ["Live Team"]


def test_update_coach_over_http_distinguishes_omitted_fields_from_explicit_null(season_centre_client):
    """Regression test for a Codex review finding: the browser form sends
    an explicit `null` to clear email/phone, and omits fields it does not
    edit -- the API must honour that distinction rather than treating a
    JSON-default `None` the same as an omitted field."""
    client = season_centre_client
    coach = client.post(
        "/api/admin/season-centre/coaches",
        json={"display_name": "Coach", "email": "old@example.test", "phone": "0400000000"},
    ).json()

    # Body omits "email" and "phone" entirely -- both must be left alone.
    renamed = client.post(f"/api/admin/season-centre/coaches/{coach['coach_id']}", json={"display_name": "Renamed"})
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["display_name"] == "Renamed"
    assert body["email"] == "old@example.test"
    assert body["phone"] == "0400000000"

    # Body sends an explicit null for "email" -- it must be cleared.
    cleared = client.post(f"/api/admin/season-centre/coaches/{coach['coach_id']}", json={"email": None})
    assert cleared.status_code == 200
    body = cleared.json()
    assert body["email"] is None
    assert body["phone"] == "0400000000"
    assert body["display_name"] == "Renamed"


def test_create_and_update_coach_reject_duplicate_email_with_400_not_500(season_centre_client):
    client = season_centre_client
    client.post("/api/admin/season-centre/coaches", json={"display_name": "First", "email": "dup@example.test"})
    other = client.post("/api/admin/season-centre/coaches", json={"display_name": "Second"}).json()

    duplicate_create = client.post(
        "/api/admin/season-centre/coaches", json={"display_name": "Third", "email": "Dup@Example.Test"}
    )
    assert duplicate_create.status_code == 400

    duplicate_update = client.post(
        f"/api/admin/season-centre/coaches/{other['coach_id']}", json={"email": "dup@example.test"}
    )
    assert duplicate_update.status_code == 400


def test_secretary_does_not_see_opening_round_link_but_admin_does(season_centre_client):
    """PR #132 review: Opening Round Operations requires `opening_round.
    nominate` (Scorer/Replay Operator/Admin), which a Secretary lacks --
    Season Centre must hide the link for a Secretary even once rules are
    accepted for the season, exactly like the existing `draft.participate`
    filtering already applied to the `draft` link."""
    client = season_centre_client
    db = client.app.state.database
    round_, _entries = configured(db, 2026, 1345)
    season_id = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()["season_id"]
    OpeningRoundRuleRepository(db).accept(
        season_id, 15, 2026, 1343, 1345, round_.bbbffl_round_id, _AnyRoundExists(), actor=ADMIN, reason="test"
    )

    admin_view = client.get(f"/api/admin/season-centre/{season_id}").json()
    assert admin_view["links"]["opening_round"] == f"/operations/seasons/{season_id}/opening-round"

    operator = client.app.state.identities.create_coach("Authenticated Secretary", email="secretary@example.test")
    client.app.state.credentials.set_password(operator.coach_id, "correct horse battery staple", actor=ADMIN)
    client.app.state.role_grants.grant(operator.coach_id, "secretary", season_id=season_id, actor=ADMIN)

    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    login = client.post(
        "/login",
        data={"email": "secretary@example.test", "password": "correct horse battery staple", "csrf_token": token},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    session = login.cookies["bbbffl_session"]
    account = client.get("/account", cookies={"bbbffl_session": session})
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)
    cookies = {"bbbffl_session": session, "bbbffl_csrf": account.cookies["bbbffl_csrf"]}
    headers = {"X-CSRF-Token": csrf}
    assert (
        client.post("/api/context/role", json={"role": "secretary"}, cookies=cookies, headers=headers).status_code
        == 200
    )

    secretary_view = client.get(f"/api/admin/season-centre/{season_id}", cookies=cookies).json()
    assert secretary_view["links"]["opening_round"] is None
    # The readiness summary itself (nomination progress counts) stays
    # informational for a Secretary -- only the link into a page they
    # cannot use is hidden.
    assert secretary_view["readiness"]["opening_round"] is not None
