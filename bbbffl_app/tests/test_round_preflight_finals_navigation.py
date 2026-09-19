"""Issue #221: closes the remaining Finals preflight navigation gap while
preserving the existing separation between Round Preflight
(`app.round_preflight`/`/admin/round-preflight/...`) and the Scorer
Operations workflow. Covers:

- the `/admin/round-preflight` index listing Finals Week 1, Finals Week 2,
  Preliminary Final and Grand Final -- in bracket order, immediately after
  that season's Rounds 1-20, using the identical human-readable labels
  `app.scorer_dashboard.season_round_options` already surfaces on the
  Scorer Round selector -- and every entry routing straight to its own
  `/admin/round-preflight/{bbbffl_round_id}`, no UUID required;
- the Scorer "Next safe action" exposing a direct link into a blocked
  Finals week's own stream-aware preflight page, both from the ordinary
  dashboard's Finals-phase bridge (`app.scorer_dashboard.
  _finals_phase_next_action`) and from the composed Finals-week dashboard
  itself (`app.finals_superscore_dashboard._finals_week_next_action`) --
  for an earlier Finals week and for the Grand Final, proving this is
  generic for every Finals week rather than a Grand-Final-only special
  case;
- season-scoped authorisation still applying to the extended index and to
  direct preflight navigation into a Finals round;
- ordinary Round 1-20 preflight navigation remaining unaffected;
- the round-preflight page's return link back to the Scorer dashboard.

Builds on the same finals fixtures tests/test_finals.py and
tests/test_scorer_dashboard_finals_next_action.py already use -- never a
new seeding convention."""

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext, AuditEventRepository
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals_preflight import open_finals_week
from app.identity import IdentityRepository
from app.round_review import RoundReviewRepository
from app.scorer_dashboard import build_scorer_dashboard
from app.season import SeasonRepository
from tests.finals_helpers import (
    accept_week_mapping,
    build_finals_ready_season,
    mark_finals_round_final,
    seed_official_result,
)
from tests.test_finals import ACTOR, _advance_to_week3, _advance_week1, _create, _repo
from tests.test_scorer_dashboard_finals_superscore import _StubAflClient


def _login(client, email, password):
    """The full coach-session login flow (login -> account CSRF -> cookies/
    headers) `tests.test_round_preflight`'s authenticated happy-path test
    already uses -- factored out so more than one test here can act as a
    non-admin, season-scoped role without repeating this boilerplate."""
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    login = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    session = login.cookies["bbbffl_session"]
    account = client.get("/account", cookies={"bbbffl_session": session})
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)
    cookies = {"bbbffl_session": session, "bbbffl_csrf": account.cookies["bbbffl_csrf"]}
    headers = {"X-CSRF-Token": csrf}
    return cookies, headers


def _dashboard(database, season_id, *, round_id=None):
    return build_scorer_dashboard(
        database,
        CompetitionLifecycleRepository(database),
        IdentityRepository(database),
        SeasonRepository(database),
        RoundReviewRepository(database),
        AuditEventRepository(database),
        _StubAflClient(),
        season_id,
        round_id=round_id,
    )


def _bracket_ready_for_grand_final_preflight(year, database=None):
    """A bracket advanced all the way to a materialised Grand Final pairing
    (weeks 1-3 mapped, opened, seeded and advanced, exactly mirroring
    `tests.test_finals.test_grand_final_pairing_and_tie_progression`) but
    deliberately *without* an accepted AFL mapping for week 4 -- so the
    Grand Final's own preflight is blocked on `mapping_missing` alone
    (never `pairing_missing`), the exact "Complete Grand Final preflight"
    state issue #221's acceptance scenario describes."""
    built = build_finals_ready_season(database=database, year=year)
    bracket = _create(built, reason="issue #221 grand final preflight regression bracket")["bracket"]
    database = built["database"]
    repo = _repo(built)
    for week in (1, 2, 3):
        round_id = repo.get_week_round_id(bracket.bracket_id, week)
        accept_week_mapping(database, round_id, year=year, afl_round_id=9000 + week)

    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    # `_finals_phase_next_action` (the ordinary dashboard's Finals-phase
    # bridge) reads each week's own *persisted lifecycle* state, unlike
    # `advance_bracket` itself (which only ever looks at `bbbffl_matchup`/
    # `bbbffl_official_result`, per `seed_official_result`'s docstring) --
    # so each completed week must be explicitly marked `final` here, the
    # same technique `tests.test_scorer_dashboard_finals_next_action`
    # already uses for the identical reason.
    mark_finals_round_final(database, repo.get_week_round_id(bracket.bracket_id, 1))
    _advance_to_week3(built, bracket, ss2_result=(100, 50), fs_result=(50, 100))
    mark_finals_round_final(database, repo.get_week_round_id(bracket.bracket_id, 2))
    open_finals_week(database, bracket.bracket_id, 3, actor=ACTOR)
    pf = repo.list_pairings(bracket.bracket_id, week_number=3)[0]
    seed_official_result(database, pf.matchup_id, 100, 50)
    repo.advance_bracket(bracket.bracket_id, 3, actor=ACTOR, reason="advance from week 3")
    mark_finals_round_final(database, repo.get_week_round_id(bracket.bracket_id, 3))

    built["bracket"] = bracket
    built["week4_round_id"] = repo.get_week_round_id(bracket.bracket_id, 4)
    return built


@pytest.fixture
def nav_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


# -- 1. Round Preflight index: Finals entries, order, labels, routing -------


def test_round_preflight_index_lists_finals_weeks_after_ordinary_rounds_with_correct_labels_and_routes(nav_client):
    client = nav_client
    database = client.app.state.database
    built = build_finals_ready_season(database=database, year=2900)
    bracket = _create(built)["bracket"]
    repo = _repo(built)

    response = client.get("/api/admin/round-preflight")
    assert response.status_code == 200
    season_rows = [r for r in response.json()["rounds"] if r["season_id"] == built["season"].season_id]

    assert [r["round_label"] for r in season_rows] == [f"Round {n}" for n in range(1, 21)] + [
        "Finals Week 1",
        "Finals Week 2",
        "Preliminary Final",
        "Grand Final",
    ]
    assert [r["round_type"] for r in season_rows] == ["ordinary"] * 20 + ["finals"] * 4

    finals_rows = season_rows[20:]
    for week_number, row in zip((1, 2, 3, 4), finals_rows):
        round_id = repo.get_week_round_id(bracket.bracket_id, week_number)
        # No UUID needs to be separately known: the index itself resolves
        # each Finals week's real round_id and hands back its own
        # preflight_url, exactly like an ordinary round always has.
        assert row["bbbffl_round_id"] == round_id
        assert row["preflight_url"] == f"/admin/round-preflight/{round_id}"

    page = client.get("/admin/round-preflight")
    assert page.status_code == 200


def test_round_preflight_index_ordinary_rounds_are_unaffected_when_no_finals_bracket_exists(nav_client):
    """Regression: a season with no Finals bracket yet still lists exactly
    its ordinary Rounds 1-20, precisely as before issue #221."""
    client = nav_client
    database = client.app.state.database
    built = build_finals_ready_season(database=database, year=2910)

    response = client.get("/api/admin/round-preflight")
    assert response.status_code == 200
    season_rows = [r for r in response.json()["rounds"] if r["season_id"] == built["season"].season_id]
    assert len(season_rows) == 20
    assert all(r["round_type"] == "ordinary" for r in season_rows)


# -- 2. Direct Scorer-to-preflight navigation while blocked ------------------
# -- generic for every Finals week: an earlier week and the Grand Final -----


def test_ordinary_dashboard_bridge_links_directly_to_a_blocked_earlier_finals_weeks_preflight():
    built = build_finals_ready_season(year=2920)
    database = built["database"]
    bracket = _create(built)["bracket"]
    week1_round_id = _repo(built).get_week_round_id(bracket.bracket_id, 1)

    dashboard = _dashboard(database, built["season"].season_id)
    assert dashboard["next_action"]["code"] == "finals_week_preflight_incomplete"
    assert dashboard["next_action"]["title"] == "Complete Finals Week 1 preflight"
    assert dashboard["next_action"]["url"] == f"/admin/round-preflight/{week1_round_id}"
    assert dashboard["next_action"]["capability"] == "roundsetup.manage"


def test_ordinary_dashboard_bridge_links_directly_to_a_blocked_grand_final_preflight():
    """Generic for every Finals week, not a Grand-Final-only special case:
    the exact same `finals_week_preflight_incomplete` action, now pointed
    at week 4's own round."""
    built = _bracket_ready_for_grand_final_preflight(year=2921)
    dashboard = _dashboard(built["database"], built["season"].season_id)
    assert dashboard["next_action"]["code"] == "finals_week_preflight_incomplete"
    assert dashboard["next_action"]["title"] == "Complete Grand Final preflight"
    assert dashboard["next_action"]["url"] == f"/admin/round-preflight/{built['week4_round_id']}"
    assert dashboard["next_action"]["capability"] == "roundsetup.manage"


def test_composed_finals_dashboard_links_directly_to_its_own_blocked_preflight_for_an_earlier_week(nav_client):
    client = nav_client
    database = client.app.state.database
    built = build_finals_ready_season(database=database, year=2922)
    bracket = _create(built)["bracket"]
    week1_round_id = _repo(built).get_week_round_id(bracket.bracket_id, 1)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": week1_round_id}
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard["stream"] == "finals_week"
    assert dashboard["next_action"]["code"] == "finals_week_preflight_incomplete"
    assert dashboard["next_action"]["title"] == "Complete Finals Week 1 preflight"
    assert dashboard["next_action"]["url"] == f"/admin/round-preflight/{week1_round_id}"
    # The link is presented as actually actionable for an Administrator,
    # mirroring `_annotate_actionability`'s existing convention.
    assert dashboard["next_action"]["actionable_by_you"] is True


def test_composed_finals_dashboard_links_directly_to_its_own_blocked_grand_final_preflight(nav_client):
    client = nav_client
    built = _bracket_ready_for_grand_final_preflight(year=2923, database=client.app.state.database)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["week4_round_id"]}
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard["stream"] == "finals_week"
    assert dashboard["next_action"]["code"] == "finals_week_preflight_incomplete"
    assert dashboard["next_action"]["title"] == "Complete Grand Final preflight"
    assert dashboard["next_action"]["url"] == f"/admin/round-preflight/{built['week4_round_id']}"


# -- 3. Season-scoped authorisation extends to the Finals entries -----------


def test_finals_navigation_respects_season_scoped_authorisation(nav_client):
    """A Secretary scoped to one season must never see, nor be able to
    directly reach through the round-preflight route, another season's
    Finals weeks -- the same `require_role_covers_season` boundary already
    enforced for ordinary rounds must cover the new Finals entries too."""
    client = nav_client
    database = client.app.state.database
    built_a = build_finals_ready_season(database=database, year=2930)
    built_b = build_finals_ready_season(database=database, year=2931)
    bracket_a = _create(built_a, reason="season A bracket")["bracket"]
    bracket_b = _create(built_b, reason="season B bracket")["bracket"]
    week4_round_id_a = _repo(built_a).get_week_round_id(bracket_a.bracket_id, 4)
    week1_round_id_b = _repo(built_b).get_week_round_id(bracket_b.bracket_id, 1)

    operator = client.app.state.identities.create_coach("Scoped Secretary", email="scoped-secretary@example.com")
    client.app.state.credentials.set_password(
        operator.coach_id, "correct horse battery staple", actor=ActorContext.anonymous_operator("admin")
    )
    client.app.state.role_grants.grant(
        operator.coach_id,
        "secretary",
        season_id=built_a["season"].season_id,
        actor=ActorContext.anonymous_operator("admin"),
    )

    cookies, headers = _login(client, "scoped-secretary@example.com", "correct horse battery staple")
    assert (
        client.post("/api/context/role", json={"role": "secretary"}, cookies=cookies, headers=headers).status_code
        == 200
    )

    response = client.get("/api/admin/round-preflight", cookies=cookies)
    assert response.status_code == 200
    round_ids = {r["bbbffl_round_id"] for r in response.json()["rounds"]}
    assert week4_round_id_a in round_ids  # this operator's own season's Grand Final is visible
    assert week1_round_id_b not in round_ids  # another season's Finals week is never listed

    allowed = client.get(f"/api/admin/round-preflight/{week4_round_id_a}", cookies=cookies)
    assert allowed.status_code == 200

    denied = client.get(f"/api/admin/round-preflight/{week1_round_id_b}", cookies=cookies)
    assert denied.status_code == 403


# -- 4. Return path: back to the Scorer dashboard from Round Preflight ------


def test_round_preflight_page_offers_a_return_link_back_to_the_scorer_dashboard(nav_client):
    """After completing Finals preflight, the operator must be able to get
    naturally back to the same Finals Scorer context and continue with the
    existing paired `Open finals week` action -- reusing the existing
    `/scorer?season_id=...&round_id=...` navigation pattern (`app.
    scorer_dashboard.SCORER_DASHBOARD_URL`) rather than a new workflow.
    Default test-mode principal (no admin token configured) resolves to
    Administrator, which can view the Scorer dashboard."""
    client = nav_client
    database = client.app.state.database
    built = build_finals_ready_season(database=database, year=2940)
    bracket = _create(built)["bracket"]
    week1_round_id = _repo(built).get_week_round_id(bracket.bracket_id, 1)

    page = client.get(f"/admin/round-preflight/{week1_round_id}")
    assert page.status_code == 200
    assert "Back to Scorer dashboard" in page.text
    assert "/scorer?season_id=" in page.text
    assert "round_id=" in page.text
    # The page renders unconditionally for every role (this shell performs
    # no server-side gate of its own -- the JSON APIs it calls do), so the
    # link markup itself is always present in source; the actual runtime
    # decision is this flag, rendered server-side from the active role.
    assert "canViewScorerDashboard=true" in page.text

    # The JSON the page's own script consumes carries everything the return
    # link is built from -- the round's season_id alongside its own id --
    # so no separate lookup/UUID is ever required to construct it.
    view = client.get(f"/api/admin/round-preflight/{week1_round_id}")
    assert view.status_code == 200
    assert view.json()["round"]["season_id"] == built["season"].season_id
    assert view.json()["round"]["bbbffl_round_id"] == week1_round_id


def test_round_preflight_page_hides_the_return_link_for_a_role_that_cannot_view_the_scorer_dashboard(nav_client):
    """Codex review, PR #222 (P2): a Secretary holds `roundsetup.manage`
    and can reach this very preflight page (and the extended Finals
    index), but `/api/scorer/dashboard` admits only Scorer/Replay-Operator/
    Administrator (`app.routes.scorer_dashboard.require_scorer_dashboard`)
    -- the return link must never promise a destination this role cannot
    actually reach."""
    client = nav_client
    database = client.app.state.database
    built = build_finals_ready_season(database=database, year=2941)
    bracket = _create(built)["bracket"]
    week1_round_id = _repo(built).get_week_round_id(bracket.bracket_id, 1)

    operator = client.app.state.identities.create_coach(
        "Preflight-only Secretary", email="preflight-secretary@example.com"
    )
    client.app.state.credentials.set_password(
        operator.coach_id, "correct horse battery staple", actor=ActorContext.anonymous_operator("admin")
    )
    client.app.state.role_grants.grant(
        operator.coach_id,
        "secretary",
        season_id=built["season"].season_id,
        actor=ActorContext.anonymous_operator("admin"),
    )
    cookies, headers = _login(client, "preflight-secretary@example.com", "correct horse battery staple")
    assert (
        client.post("/api/context/role", json={"role": "secretary"}, cookies=cookies, headers=headers).status_code
        == 200
    )

    # The Secretary can still reach preflight itself -- roundsetup.manage
    # is unaffected -- only the misleading return link is suppressed.
    view = client.get(f"/api/admin/round-preflight/{week1_round_id}", cookies=cookies)
    assert view.status_code == 200

    page = client.get(f"/admin/round-preflight/{week1_round_id}", cookies=cookies)
    assert page.status_code == 200
    assert "canViewScorerDashboard=false" in page.text
