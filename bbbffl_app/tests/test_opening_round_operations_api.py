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
from app.opening_round import (
    OpeningRoundNominationRepository,
    OpeningRoundRuleRepository,
    OpeningRoundSubmissionRepository,
)
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
    # No entry has confirmed yet -- not "0 required", never inferred from
    # eligible clubs/players (issue #133).
    assert after["readiness"]["opening_round"]["total_entries"] == len(entries)
    assert after["readiness"]["opening_round"]["total_confirmed"] == 0
    assert after["readiness"]["opening_round"]["is_ready"] is False

    OwnershipRepository(db).configure_squad_limit(season_id, 5)
    _own(db, season_id, entries[0], 920001, "Test GWS Player", GWS_CLUB_ID, GWS_CLUB_NAME)
    # Owning an eligible player still does not by itself make confirmation
    # required for that entry any more than for any other -- the readiness
    # signal is unchanged by ownership alone.
    still_unconfirmed = client.get(f"/api/admin/season-centre/{season_id}").json()
    assert still_unconfirmed["readiness"]["opening_round"]["total_confirmed"] == 0
    assert still_unconfirmed["readiness"]["opening_round"]["is_ready"] is False
    awaiting_names = {
        e["team_name"] for e in still_unconfirmed["readiness"]["opening_round"]["entries_awaiting_confirmation"]
    }
    assert "Team 0" in awaiting_names

    from app.audit import ActorContext
    from app.opening_round import OpeningRoundSubmissionRepository

    submissions = OpeningRoundSubmissionRepository(db)
    for entry in entries:
        submissions.confirm(season_id, entry.season_entry_id, actor=ActorContext.anonymous_operator("admin"))
    confirmed = client.get(f"/api/admin/season-centre/{season_id}").json()
    assert confirmed["readiness"]["opening_round"]["total_confirmed"] == len(entries)
    assert confirmed["readiness"]["opening_round"]["is_ready"] is True
    assert confirmed["readiness"]["opening_round"]["entries_awaiting_confirmation"] == []


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


def test_round_preflight_blocks_when_a_nominated_player_is_traded_away_after_confirmation(
    operations_client, monkeypatch
):
    """Issue #133: confirmation completeness and nomination integrity are
    independent signals. Once every required entry has confirmed, a
    nomination row later corrupted by a trade-away must not silently pass
    round preflight -- it now blocks via a distinct integrity conflict, not
    by pretending the entry became unconfirmed."""
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
    submissions = OpeningRoundSubmissionRepository(db)
    for entry in entries:
        submissions.confirm(season_id, entry.season_entry_id, actor=ADMIN)

    ready = client.get(f"/api/admin/round-preflight/{round_.bbbffl_round_id}").json()
    codes = {b["code"] for b in ready["readiness"]["blockers"]}
    assert "opening_round_nominations_incomplete" not in codes
    assert "opening_round_integrity_conflict" not in codes

    OwnershipRepository(db).release(nominated_player.season_player_id)

    blocked = client.get(f"/api/admin/round-preflight/{round_.bbbffl_round_id}").json()
    codes = {b["code"] for b in blocked["readiness"]["blockers"]}
    # The entry's confirmation itself is untouched by the trade -- this is
    # an integrity conflict, never a reversion to "incomplete".
    assert "opening_round_nominations_incomplete" not in codes
    assert "opening_round_integrity_conflict" in codes
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

    # #8: readiness before any nomination -- unconfirmed, and never
    # expressed as "required" against eligible rules/players (issue #133).
    assert view["readiness"]["is_confirmed"] is False
    assert view["submission"]["state"] == "draft"

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

    # A nomination alone still does not confirm anything -- confirmation is
    # a separate, explicit action (issue #133).
    ready = client.get(url, cookies=cookies).json()
    assert ready["readiness"]["is_confirmed"] is False
    assert ready["submission"]["state"] == "draft"

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

    # CSRF protection also guards the confirm/reopen actions.
    unprotected_confirm = client.post(url + "/confirm", json={"reason": "no csrf"}, cookies=cookies)
    assert unprotected_confirm.status_code == 403

    # Explicit confirmation: a partial legal set (one nomination, other
    # eligible slots left vacant) is a valid, confirmable submission.
    confirmed = client.post(
        url + "/confirm", json={"reason": "partial replay submission confirmed"}, cookies=cookies, headers=headers
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["submission"]["state"] == "confirmed"
    assert confirmed.json()["submission"]["confirmed_by_display_name"] == "Authenticated Replay Operator"
    assert confirmed.json()["readiness"]["is_confirmed"] is True

    after_confirm = client.get(url, cookies=cookies).json()
    assert after_confirm["submission"]["state"] == "confirmed"
    assert after_confirm["readiness"]["is_confirmed"] is True

    # A confirmed submission cannot be silently mutated: a further
    # correction attempt is refused until an explicit, audited reopen.
    blocked_correction = client.patch(
        url + f"/nominations/{nomination_id}",
        json={"position": "Ruck", "reason": "attempted post-confirmation change"},
        cookies=cookies,
        headers=headers,
    )
    assert blocked_correction.status_code == 409

    # Confirming again is idempotent -- no error, no duplicated history.
    reconfirmed = client.post(
        url + "/confirm", json={"reason": "repeat confirmation"}, cookies=cookies, headers=headers
    )
    assert reconfirmed.status_code == 200
    assert len(reconfirmed.json()["submission"]["history"]) == 1

    # Explicit, audited reopen is required before further edits.
    reopened = client.post(
        url + "/reopen",
        json={"reason": "historical reconstruction needs a correction"},
        cookies=cookies,
        headers=headers,
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["submission"]["state"] == "draft"

    now_editable = client.patch(
        url + f"/nominations/{nomination_id}",
        json={"position": "Ruck", "reason": "correction after audited reopen"},
        cookies=cookies,
        headers=headers,
    )
    assert now_editable.status_code == 200, now_editable.text
    assert now_editable.json()["nomination"]["position"] == "Ruck"

    reopen_history = client.get(url, cookies=cookies).json()["submission"]["history"]
    assert [h["state"] for h in reopen_history] == ["confirmed", "draft"]
    assert reopen_history[1]["reason"] == "historical reconstruction needs a correction"


def test_round_preflight_blocks_a_round_holding_a_drifted_nomination_no_rule_currently_targets(operations_client):
    """PR #134 review (P1): `conflicting_nominations` entries are, by
    definition, nominations whose persisted `bbbffl_round_id` no longer
    matches any rule's *current* target -- so the round that nomination
    actually lives in (still returned by `list_for_round`, still treated as
    an active locked selection) has no rule targeting it, and the old
    rule-ID-only match would skip the entire integrity check for that round
    entirely. It must still block via `opening_round_integrity_conflict`."""
    from app.competition_lifecycle import CompetitionLifecycleRepository
    from app.db import transaction as database_transaction
    from app.round_mapping import RoundMappingRepository
    from app.season import SeasonRepository

    client = operations_client
    db = client.app.state.database
    round2, entries = configured(db, 2026, evidence.EVIDENCE_2026.compensating_bye_round["GWS"])
    season_id = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round2.bbbffl_round_id,),
    ).fetchone()["season_id"]
    OwnershipRepository(db).configure_squad_limit(season_id, 5)
    rule, ev, bye_round_id = _accept_gws_2026_rule(db, season_id, round2.bbbffl_round_id)
    player = _own(db, season_id, entries[0], 920011, "Nominated GWS Player", GWS_CLUB_ID, GWS_CLUB_NAME)
    afl_client = _afl_client(ev, bye_round_id)

    nomination = OpeningRoundNominationRepository(db).nominate(
        rule.rule_id,
        entries[0].season_entry_id,
        "M1",
        player.season_player_id,
        afl_client,
        actor=ADMIN,
        reason="synthetic",
    )
    submissions = OpeningRoundSubmissionRepository(db)
    for entry in entries:
        submissions.confirm(season_id, entry.season_entry_id, actor=ADMIN)

    # A round no accepted rule currently targets -- created after the
    # nomination so the drift below is genuinely "no rule targets this
    # round", not merely "not yet".
    round3 = SeasonRepository(db).create_round(round2.competition_id, "round-3-drift", "Round 3", 3)
    RoundMappingRepository(db).accept(
        round3.bbbffl_round_id,
        2026,
        evidence.EVIDENCE_2026.compensating_bye_round["GCFC"],
        KnownRounds((2026, evidence.EVIDENCE_2026.compensating_bye_round["GCFC"])),
    )
    CompetitionLifecycleRepository(db).create_ordinary_round(round3.bbbffl_round_id)

    # Simulate the nomination's persisted target drifting to round3 (never
    # possible through app.opening_round's own write paths).
    with database_transaction(db) as conn:
        conn.execute(
            "UPDATE opening_round_nomination SET bbbffl_round_id=? WHERE nomination_id=?",
            (round3.bbbffl_round_id, nomination.nomination_id),
        )

    drifted_round_view = client.get(f"/api/admin/round-preflight/{round3.bbbffl_round_id}").json()
    codes = {b["code"] for b in drifted_round_view["readiness"]["blockers"]}
    assert "opening_round_integrity_conflict" in codes
    assert drifted_round_view["readiness"]["safe_to_open"] is False

    # The rule's own current target round must still catch it too (the
    # rule-ID match path, unaffected by this fix).
    original_round_view = client.get(f"/api/admin/round-preflight/{round2.bbbffl_round_id}").json()
    assert "opening_round_integrity_conflict" in {b["code"] for b in original_round_view["readiness"]["blockers"]}


def test_represented_entry_response_returns_each_nomination_once_when_rules_share_a_target_round(
    operations_client, monkeypatch
):
    """The exact discovered reproduction (issue #133): four accepted rules
    sharing one target BBBFFL round previously caused the represented-entry
    GET response to repeat that round's single persisted nomination once
    per rule. A second nomination in a second shared-round group must
    likewise appear exactly once."""
    from app.afl_client import Match, Team

    client = operations_client
    db = client.app.state.database
    ev = evidence.EVIDENCE_2026
    round2, entries = configured(db, 2026, ev.compensating_bye_round["BL"])  # afl round 1345
    season_id = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round2.bbbffl_round_id,),
    ).fetchone()["season_id"]
    OwnershipRepository(db).configure_squad_limit(season_id, 10)

    from app.competition_lifecycle import CompetitionLifecycleRepository
    from app.round_mapping import RoundMappingRepository
    from app.season import SeasonRepository

    round3 = SeasonRepository(db).create_round(round2.competition_id, "round-3", "Round 3", 3)
    RoundMappingRepository(db).accept(
        round3.bbbffl_round_id,
        2026,
        ev.compensating_bye_round["GCFC"],
        KnownRounds((2026, ev.compensating_bye_round["GCFC"])),
    )
    CompetitionLifecycleRepository(db).create_ordinary_round(round3.bbbffl_round_id)

    def accept(club_id, code, bbbffl_round_id):
        validator = KnownRounds(
            (ev.afl_season_id, ev.afl_opening_round_id), (ev.afl_season_id, ev.compensating_bye_round[code])
        )
        return OpeningRoundRuleRepository(db).accept(
            season_id,
            club_id,
            ev.afl_season_id,
            ev.afl_opening_round_id,
            ev.compensating_bye_round[code],
            bbbffl_round_id,
            validator,
            actor=ADMIN,
            reason="synthetic",
        )

    # Round 2: four accepted rules (BL, COLL, CARL, GEEL) sharing one target
    # BBBFFL round -- exactly the reported reproduction.
    rule_bl = accept(2, "BL", round2.bbbffl_round_id)
    accept(3, "COLL", round2.bbbffl_round_id)
    accept(5, "CARL", round2.bbbffl_round_id)
    accept(10, "GEEL", round2.bbbffl_round_id)
    # Round 3: two accepted rules (GCFC, WB) sharing another target round.
    rule_gcfc = accept(4, "GCFC", round3.bbbffl_round_id)
    accept(8, "WB", round3.bbbffl_round_id)

    represented = entries[0]
    bl_player = _own(db, season_id, represented, 921001, "BL Player", 2, "Brisbane Lions")
    gcfc_player = _own(db, season_id, represented, 921002, "GCFC Player", 4, "Gold Coast")

    operator, cookies, headers = _authenticate_replay_operator(client, season_id, represented.season_entry_id)
    afl_client = StaticAflClient(
        matches_by_round={
            ev.afl_opening_round_id: [
                Match(match_id=7101, home_team=Team(2, "BL"), away_team=Team(4, "GCFC"), status="CONCLUDED"),
            ],
        },
        rounds_by_season={},
    )
    monkeypatch.setattr(client.app.state, "afl_client", afl_client)

    url = f"/api/operations/seasons/{season_id}/opening-round"
    nomination_1 = client.post(
        url + "/nominations",
        json={
            "rule_id": rule_bl.rule_id,
            "season_player_id": bl_player.season_player_id,
            "position": "M1",
            "reason": "synthetic",
        },
        cookies=cookies,
        headers=headers,
    ).json()["nomination"]

    # After only one nomination (round 2's), the response must contain
    # exactly that one -- not four copies of it.
    only_one = client.get(url, cookies=cookies).json()
    assert len(only_one["nominations"]) == 1
    assert only_one["nominations"][0]["nomination_id"] == nomination_1["nomination_id"]

    nomination_2 = client.post(
        url + "/nominations",
        json={
            "rule_id": rule_gcfc.rule_id,
            "season_player_id": gcfc_player.season_player_id,
            "position": "M2",
            "reason": "synthetic",
        },
        cookies=cookies,
        headers=headers,
    ).json()["nomination"]

    both = client.get(url, cookies=cookies).json()
    assert {n["nomination_id"] for n in both["nominations"]} == {
        nomination_1["nomination_id"],
        nomination_2["nomination_id"],
    }
    assert len(both["nominations"]) == 2


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
