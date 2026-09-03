"""HTTP-level coverage for Opening Round Operations discoverability,
human-readable presentation, and server-authoritative validation (issue
#131). Exercises the real `/api/operations/seasons/{season_id}/opening-round`
surface through `app.main.app`, the same authenticated Replay Operator
acting-context flow `tests/test_round_preflight.py::
test_authenticated_preflight_happy_path_retains_operator_provenance_and_freezes_context`
already establishes for Secretary/round-preflight.

Every nomination here is an explicitly synthetic test scenario built from
`tests/opening_round_evidence.py`'s real AFL-side facts -- no historical
BBBFFL nomination record exists in this repository (see
docs/opening-round-deferred-selection.md's evidence-boundary section).
"""

import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.opening_round import OpeningRoundNominationRepository, OpeningRoundRuleRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests import opening_round_evidence as evidence
from tests.test_competition_lifecycle import configured

ADMIN = ActorContext.anonymous_operator("admin")
GWS_CLUB_ID = 15
GWS_CLUB_NAME = "GWS Giants"
OTHER_CLUB_ID = 5
OTHER_CLUB_NAME = "Carlton"


class KnownRounds:
    """`app.round_mapping.AflReferenceValidator` for `OpeningRoundRuleRepository.
    accept` -- accepts only the explicit (season, round) pairs supplied."""

    def __init__(self, *pairs):
        self.pairs = set(pairs)

    def round_exists(self, season, round_):
        return (season, round_) in self.pairs


class StaticAflClient:
    """Duck-typed AFL client for the HTTP boundary: `get_matches` resolves a
    nomination's source Opening Round match, `get_rounds` resolves AFL round
    numbers for human-readable presentation (issue #131 #3)."""

    def __init__(self, matches_by_round, rounds_by_season):
        self.matches_by_round = matches_by_round
        self.rounds_by_season = rounds_by_season

    def get_matches(self, round_id):
        return self.matches_by_round.get(round_id, [])

    def get_rounds(self, season_id):
        return self.rounds_by_season.get(season_id, [])


def _round_obj(round_id, round_number):
    return SimpleNamespace(round_id=round_id, round_number=round_number)


def _own(db, season_id, entry, canonical_id, name, afl_team_id, afl_team_name):
    player = PlayerPoolRepository(db).refresh_player(
        season_id, canonical_id, name, afl_team_id=afl_team_id, afl_team_name=afl_team_name
    )
    OwnershipRepository(db).acquire(player.season_player_id, entry.season_entry_id)
    return player


def _accept_gws_2026_rule(db, season_id, bbbffl_round_id):
    ev = evidence.EVIDENCE_2026
    bye_round_id = ev.compensating_bye_round["GWS"]
    validator = KnownRounds((ev.afl_season_id, ev.afl_opening_round_id), (ev.afl_season_id, bye_round_id))
    rule = OpeningRoundRuleRepository(db).accept(
        season_id,
        GWS_CLUB_ID,
        ev.afl_season_id,
        ev.afl_opening_round_id,
        bye_round_id,
        bbbffl_round_id,
        validator,
        actor=ADMIN,
        reason="synthetic test scenario built from tests/opening_round_evidence.py",
    )
    return rule, ev, bye_round_id


def _afl_client(ev, bye_round_id, opening_match_home=GWS_CLUB_ID):
    from app.afl_client import Match, Team

    return StaticAflClient(
        matches_by_round={
            ev.afl_opening_round_id: [
                Match(
                    match_id=7001,
                    home_team=Team(opening_match_home, GWS_CLUB_NAME),
                    away_team=Team(1, "Adelaide"),
                    status="CONCLUDED",
                )
            ]
        },
        rounds_by_season={
            ev.afl_season_id: [_round_obj(ev.afl_opening_round_id, 0), _round_obj(bye_round_id, 4)],
        },
    )


@pytest.fixture
def operations_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _authenticate_replay_operator(client, season_id, represented_entry_id):
    """Real authenticated-coach acting-context flow -- see
    `tests/test_round_preflight.py`'s identical pattern. Returns
    `(cookies, headers)` for subsequent CSRF-protected requests."""
    app = client.app
    operator = app.state.identities.create_coach("Authenticated Replay Operator", email="replay-op@example.com")
    app.state.credentials.set_password(operator.coach_id, "correct horse battery staple", actor=ADMIN)
    app.state.role_grants.grant(operator.coach_id, "replay_operator", season_id=season_id, actor=ADMIN)

    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    login = client.post(
        "/login",
        data={"email": "replay-op@example.com", "password": "correct horse battery staple", "csrf_token": token},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    session = login.cookies["bbbffl_session"]
    account = client.get("/account", cookies={"bbbffl_session": session})
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)
    cookies = {"bbbffl_session": session, "bbbffl_csrf": account.cookies["bbbffl_csrf"]}
    headers = {"X-CSRF-Token": csrf}
    assert (
        client.post("/api/context/role", json={"role": "replay_operator"}, cookies=cookies, headers=headers).status_code
        == 200
    )
    assert (
        client.post(
            "/api/context/represented-entry",
            json={"season_entry_id": represented_entry_id},
            cookies=cookies,
            headers=headers,
        ).status_code
        == 200
    )
    return operator, cookies, headers


def test_season_centre_exposes_opening_round_operations_only_once_rules_are_accepted(operations_client):
    client = operations_client
    db = client.app.state.database
    round_, entries = configured(db, 2026, evidence.EVIDENCE_2026.compensating_bye_round["GWS"])
    season_id = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()["season_id"]

    before = client.get(f"/api/admin/season-centre/{season_id}").json()
    assert before["links"]["opening_round"] is None
    assert before["readiness"]["opening_round"] is None

    _accept_gws_2026_rule(db, season_id, round_.bbbffl_round_id)

    after = client.get(f"/api/admin/season-centre/{season_id}").json()
    assert after["links"]["opening_round"] == f"/operations/seasons/{season_id}/opening-round"
    assert after["readiness"]["opening_round"]["total_required"] == 0  # no entry owns an eligible player yet
    assert after["readiness"]["opening_round"]["is_ready"] is True

    OwnershipRepository(db).configure_squad_limit(season_id, 5)
    _own(db, season_id, entries[0], 920001, "Test GWS Player", GWS_CLUB_ID, GWS_CLUB_NAME)
    incomplete = client.get(f"/api/admin/season-centre/{season_id}").json()
    assert incomplete["readiness"]["opening_round"]["total_required"] == 1
    assert incomplete["readiness"]["opening_round"]["total_completed"] == 0
    assert incomplete["readiness"]["opening_round"]["is_ready"] is False
    assert incomplete["readiness"]["opening_round"]["entries_requiring_action"][0]["team_name"] == "Team 0"


def test_round_preparation_blocks_on_incomplete_opening_round_nominations_with_navigation_link(operations_client):
    client = operations_client
    db = client.app.state.database
    round_, entries = configured(db, 2026, evidence.EVIDENCE_2026.compensating_bye_round["GWS"])
    season_id = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()["season_id"]
    OwnershipRepository(db).configure_squad_limit(season_id, 5)
    rule, ev, bye_round_id = _accept_gws_2026_rule(db, season_id, round_.bbbffl_round_id)
    _own(db, season_id, entries[0], 920002, "Test GWS Player", GWS_CLUB_ID, GWS_CLUB_NAME)

    view = client.get(f"/api/admin/round-preflight/{round_.bbbffl_round_id}").json()
    codes = {b["code"] for b in view["readiness"]["blockers"]}
    assert "opening_round_nominations_incomplete" in codes
    blocker = next(b for b in view["readiness"]["blockers"] if b["code"] == "opening_round_nominations_incomplete")
    assert blocker["url"] == f"/operations/seasons/{season_id}/opening-round"
    assert view["readiness"]["safe_to_open"] is False

    # Never silently inferred/created: no nomination row exists merely
    # because the round preflight surfaced the gap.
    assert OpeningRoundNominationRepository(db).list_for_round(round_.bbbffl_round_id) == []


def test_round_preflight_blocks_when_a_nominated_player_is_traded_away_after_nomination(operations_client, monkeypatch):
    """PR #132 review (P1): a nomination row existing under the rule is not
    by itself enough to consider the round safe to open -- once the
    nominated player is traded away, `build_opening_round_readiness` (fixed
    to revalidate current ownership, not just cached club) must stop
    counting it as complete, and round preflight must block again."""
    client = operations_client
    db = client.app.state.database
    round_, entries = configured(db, 2026, evidence.EVIDENCE_2026.compensating_bye_round["GWS"])
    season_id = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()["season_id"]
    OwnershipRepository(db).configure_squad_limit(season_id, 5)
    rule, ev, bye_round_id = _accept_gws_2026_rule(db, season_id, round_.bbbffl_round_id)
    nominated_player = _own(db, season_id, entries[0], 920009, "Nominated GWS Player", GWS_CLUB_ID, GWS_CLUB_NAME)
    # A second owned GWS-eligible player keeps entries[0] "required" for
    # this rule even after the nominated player is released below --
    # otherwise this would only prove the entry vanished from tracking, not
    # that the stale nomination itself was caught.
    _own(db, season_id, entries[0], 920010, "Replacement GWS Player", GWS_CLUB_ID, GWS_CLUB_NAME)
    monkeypatch.setattr(client.app.state, "afl_client", _afl_client(ev, bye_round_id))
    OpeningRoundNominationRepository(db).nominate(
        rule.rule_id,
        entries[0].season_entry_id,
        "M1",
        nominated_player.season_player_id,
        client.app.state.afl_client,
        actor=ADMIN,
        reason="synthetic",
    )

    ready = client.get(f"/api/admin/round-preflight/{round_.bbbffl_round_id}").json()
    assert "opening_round_nominations_incomplete" not in {b["code"] for b in ready["readiness"]["blockers"]}

    OwnershipRepository(db).release(nominated_player.season_player_id)

    blocked = client.get(f"/api/admin/round-preflight/{round_.bbbffl_round_id}").json()
    codes = {b["code"] for b in blocked["readiness"]["blockers"]}
    assert "opening_round_nominations_incomplete" in codes
    assert blocked["readiness"]["safe_to_open"] is False


def test_represented_operator_workflow_end_to_end(operations_client, monkeypatch):
    """The full issue #131 acceptance path: structured owned-player data
    scoped to the represented entry only, human-readable rule/round/club
    labels, player -> rule auto-resolution, a valid nomination creating the
    existing locked deferred selection, and readable recorded-nomination
    presentation with correction history."""
    client = operations_client
    db = client.app.state.database
    round_, entries = configured(db, 2026, evidence.EVIDENCE_2026.compensating_bye_round["GWS"])
    season_id = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()["season_id"]
    OwnershipRepository(db).configure_squad_limit(season_id, 5)
    rule, ev, bye_round_id = _accept_gws_2026_rule(db, season_id, round_.bbbffl_round_id)

    represented = entries[0]
    other_entry = entries[1]
    gws_player = _own(db, season_id, represented, 920003, "Represented GWS Player", GWS_CLUB_ID, GWS_CLUB_NAME)
    off_club_player = _own(
        db, season_id, represented, 920004, "Represented Other Player", OTHER_CLUB_ID, OTHER_CLUB_NAME
    )
    _own(db, season_id, other_entry, 920005, "Other Entry GWS Player", GWS_CLUB_ID, GWS_CLUB_NAME)

    operator, cookies, headers = _authenticate_replay_operator(client, season_id, represented.season_entry_id)
    monkeypatch.setattr(client.app.state, "afl_client", _afl_client(ev, bye_round_id))

    url = f"/api/operations/seasons/{season_id}/opening-round"
    view = client.get(url, cookies=cookies).json()

    # #4: structured owned-player data, scoped server-side to the
    # represented entry only -- never another entry's players.
    player_ids = {p["season_player_id"] for p in view["players"]}
    assert player_ids == {gws_player.season_player_id, off_club_player.season_player_id}
    by_id = {p["season_player_id"]: p for p in view["players"]}
    assert by_id[gws_player.season_player_id]["display_name"] == "Represented GWS Player"
    assert by_id[gws_player.season_player_id]["afl_club_name"] == GWS_CLUB_NAME
    assert by_id[gws_player.season_player_id]["afl_club_id"] == GWS_CLUB_ID

    # #3: human-readable rule label, never a bare UUID/club-ID primary label.
    assert len(view["rules"]) == 1
    rule_view = view["rules"][0]
    assert rule_view["display_label"] == f"{GWS_CLUB_NAME} — Opening Round → compensating AFL Round 4 → {round_.label}"
    assert rule_view["afl_club_name"] == GWS_CLUB_NAME
    assert rule_view["bbbffl_round_label"] == round_.label
    assert rule_view["rule_id"] == rule.rule_id  # internal id retained as secondary diagnostic

    # #8: readiness before any nomination.
    assert view["readiness"]["required_rule_ids"] == [rule.rule_id]
    assert view["readiness"]["is_complete"] is False

    # #5/#6: a mismatched player/rule club combination is rejected server-side.
    mismatch = client.post(
        url + "/nominations",
        json={
            "rule_id": rule.rule_id,
            "season_player_id": off_club_player.season_player_id,
            "position": "M1",
            "reason": "mismatch attempt",
        },
        cookies=cookies,
        headers=headers,
    )
    assert mismatch.status_code == 409
    assert OpeningRoundNominationRepository(db).list_for_round(round_.bbbffl_round_id) == []

    # #6/#7: a valid nomination creates the existing locked deferred selection.
    created = client.post(
        url + "/nominations",
        json={
            "rule_id": rule.rule_id,
            "season_player_id": gws_player.season_player_id,
            "position": "M1",
            "reason": "synthetic replay input",
        },
        cookies=cookies,
        headers=headers,
    )
    assert created.status_code == 200, created.text
    nomination = created.json()["nomination"]
    assert nomination["player_display_name"] == "Represented GWS Player"
    assert nomination["afl_club_name"] == GWS_CLUB_NAME
    assert nomination["rule_display_label"] == rule_view["display_label"]
    assert nomination["bbbffl_round_label"] == round_.label
    assert nomination["entered_by_display_name"] == "Authenticated Replay Operator"
    assert nomination["provenance"] == "replay/reconstructed"
    nomination_id = nomination["nomination_id"]

    lineups = client.app.state.lineups
    draft = lineups.get_draft(season_id, round_.competition_id, round_.bbbffl_round_id, represented.season_entry_id)
    assert draft.positions["M1"] == gws_player.season_player_id  # locked/preloaded, not a UI-only flag

    ready = client.get(url, cookies=cookies).json()
    assert ready["readiness"]["is_complete"] is True
    assert ready["readiness"]["nominated_rule_ids"] == [rule.rule_id]

    # #7: correction workflow remains functional and its history is readable.
    corrected = client.patch(
        url + f"/nominations/{nomination_id}",
        json={"position": "M2", "reason": "audited replay correction"},
        cookies=cookies,
        headers=headers,
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["nomination"]["position"] == "M2"

    final = client.get(url, cookies=cookies).json()
    final_nomination = next(n for n in final["nominations"] if n["nomination_id"] == nomination_id)
    assert final_nomination["position"] == "M2"
    assert len(final_nomination["correction_history"]) == 1
    assert (
        final_nomination["correction_history"][0]["reason"]
        == "Replay/reconstructed input correction: audited replay correction"
    )

    # CSRF protection remains in force for the represented operator flow.
    unprotected = client.post(
        url + "/nominations",
        json={
            "rule_id": rule.rule_id,
            "season_player_id": gws_player.season_player_id,
            "position": "M3",
            "reason": "no csrf",
        },
        cookies=cookies,
    )
    assert unprotected.status_code == 403


def test_cross_entry_and_cross_season_nomination_ids_are_not_found(operations_client, monkeypatch):
    client = operations_client
    db = client.app.state.database
    round_a, entries_a = configured(db, 2026, evidence.EVIDENCE_2026.compensating_bye_round["GWS"])
    season_a = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_a.bbbffl_round_id,),
    ).fetchone()["season_id"]
    OwnershipRepository(db).configure_squad_limit(season_a, 5)
    rule_a, ev, bye_round_id = _accept_gws_2026_rule(db, season_a, round_a.bbbffl_round_id)
    represented = entries_a[0]
    other_entry = entries_a[1]
    represented_player = _own(db, season_a, represented, 920006, "Represented Player", GWS_CLUB_ID, GWS_CLUB_NAME)
    other_player = _own(db, season_a, other_entry, 920007, "Other Entry Player", GWS_CLUB_ID, GWS_CLUB_NAME)

    operator, cookies, headers = _authenticate_replay_operator(client, season_a, represented.season_entry_id)
    monkeypatch.setattr(client.app.state, "afl_client", _afl_client(ev, bye_round_id))
    url_a = f"/api/operations/seasons/{season_a}/opening-round"

    own_nomination = client.post(
        url_a + "/nominations",
        json={
            "rule_id": rule_a.rule_id,
            "season_player_id": represented_player.season_player_id,
            "position": "M1",
            "reason": "synthetic",
        },
        cookies=cookies,
        headers=headers,
    ).json()["nomination"]

    # A nomination belonging to a different entry (created directly through
    # the domain layer, never through this operator's own HTTP session)
    # must be inaccessible for correction.
    cross_entry_nomination = OpeningRoundNominationRepository(db).nominate(
        rule_a.rule_id,
        other_entry.season_entry_id,
        "M1",
        other_player.season_player_id,
        client.app.state.afl_client,
        actor=ADMIN,
        reason="synthetic cross-entry fixture data",
    )
    cross_entry_response = client.patch(
        url_a + f"/nominations/{cross_entry_nomination.nomination_id}",
        json={"reason": "attempted cross-entry correction"},
        cookies=cookies,
        headers=headers,
    )
    assert cross_entry_response.status_code == 404

    # A different season entirely: the represented operator's grant/entry
    # do not exist there, so the season itself is unreachable...
    round_b, entries_b = configured(db, 2027, evidence.EVIDENCE_2026.compensating_bye_round["GWS"])
    season_b = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_b.bbbffl_round_id,),
    ).fetchone()["season_id"]
    cross_season_response = client.get(f"/api/operations/seasons/{season_b}/opening-round", cookies=cookies)
    assert cross_season_response.status_code in (403, 404, 409)

    # ...and a real nomination that exists only in that other season (its
    # own accepted rule, own entry, own player) must remain unreachable
    # through this operator's season-A URL, even by nomination id alone.
    OwnershipRepository(db).configure_squad_limit(season_b, 5)
    rule_b, _, _ = _accept_gws_2026_rule(db, season_b, round_b.bbbffl_round_id)
    season_b_player = _own(db, season_b, entries_b[0], 920008, "Season B GWS Player", GWS_CLUB_ID, GWS_CLUB_NAME)
    season_b_nomination = OpeningRoundNominationRepository(db).nominate(
        rule_b.rule_id,
        entries_b[0].season_entry_id,
        "M1",
        season_b_player.season_player_id,
        client.app.state.afl_client,
        actor=ADMIN,
        reason="synthetic cross-season fixture data",
    )
    cross_season_nomination_response = client.patch(
        url_a + f"/nominations/{season_b_nomination.nomination_id}",
        json={"reason": "attempted cross-season correction"},
        cookies=cookies,
        headers=headers,
    )
    assert cross_season_nomination_response.status_code == 404

    assert own_nomination["nomination_id"] != cross_entry_nomination.nomination_id
    assert own_nomination["nomination_id"] != season_b_nomination.nomination_id
