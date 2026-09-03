"""Session-native delegated lineup and Opening Round replay operations.

Every target is resolved from the shared acting context.  URL identifiers
locate a round; they never confer authority over a season or entry.
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_capability, require_entry_context, require_role_covers_season
from app.carry_forward import CarryForwardService
from app.coach_lineup import COACH_LINEUP_POSITIONS, CoachLineupService
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token
from app.lineup_proxy import LineupProxyService
from app.opening_round import (
    ENTITY_TYPE_NOMINATION,
    NOMINATION_CORRECTED,
    OpeningRoundNominationRepository,
    OpeningRoundRuleRepository,
    OpeningRoundSelectionGuard,
    build_opening_round_readiness,
    describe_accepted_rules,
)

router = APIRouter(prefix="/api/operations")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
require_proxy = require_capability("lineup.proxy")
require_nomination = require_capability("opening_round.nominate")


class DraftRequest(BaseModel):
    positions: dict[str, str | None]
    expected_revision: int


class SubmitRequest(BaseModel):
    positions: dict[str, str | None]
    expected_draft_revision: int
    expected_submission_version: int
    reason: str


class CarryRequest(BaseModel):
    expected_submission_version: int
    reason: str | None = None


class NominationRequest(BaseModel):
    rule_id: str
    position: str
    season_player_id: str
    reason: str = "Replay/reconstructed Opening Round nomination"


class CorrectionRequest(BaseModel):
    position: str | None = None
    season_player_id: str | None = None
    reason: str


def _actor(principal: Principal) -> ActorContext:
    return ActorContext("anonymous_operator", principal.coach_id, principal.role.value)


def _csrf(request: Request, principal: Principal) -> None:
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(403, "Invalid CSRF token")


def _scope(request: Request, principal: Principal, round_id: str) -> dict:
    row = request.app.state.database.execute(
        "SELECT r.bbbffl_round_id, r.label round_label, r.sequence, r.competition_id, "
        "c.season_id, s.label season_label FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "JOIN bbbffl_season s ON s.season_id=c.season_id "
        "WHERE r.bbbffl_round_id=? AND c.stream_type='ordinary'",
        (round_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown ordinary BBBFFL round")
    require_role_covers_season(request, principal, row["season_id"])
    entry_id = principal.represented_season_entry_id
    if entry_id is None:
        raise HTTPException(409, "Choose a represented team in acting context first")
    require_entry_context(request, principal, entry_id)
    entry = request.app.state.identities.get_public_team(entry_id)
    if entry is None or entry.season_id != row["season_id"]:
        raise HTTPException(404, "Private resource not found")
    return {**dict(row), "season_entry_id": entry_id, "team_name": entry.team_name}


def _guard(request: Request):
    service = CoachLineupService(request.app.state.database, request.app.state.afl_client)
    return OpeningRoundSelectionGuard(service.nominations, service.lockouts.guard(match_facts=service.match_facts))


def _lineup_view(request: Request, principal: Principal, scope: dict) -> dict:
    service = CoachLineupService(request.app.state.database, request.app.state.afl_client)
    entry = {
        "season_entry_id": scope["season_entry_id"],
        "competition_id": scope["competition_id"],
    }
    draft = service.ensure_draft(scope["season_id"], scope["bbbffl_round_id"], entry)
    submission = service.lineups.get_effective_submission(draft.lineup_id)
    source = CarryForwardService(request.app.state.database, request.app.state.afl_client).resolve_source(
        scope["season_id"], scope["competition_id"], scope["bbbffl_round_id"], scope["season_entry_id"]
    )
    deferred = {
        position: service.nominations.deferred_context(scope["bbbffl_round_id"], scope["season_entry_id"], position)
        for position in COACH_LINEUP_POSITIONS
    }
    players = [
        service.pool.get_by_id(p.season_player_id) for p in service.ownership.current_squad(scope["season_entry_id"])
    ]
    return {
        "acting_context": {
            "authenticated_operator_id": principal.coach_id,
            "display_name": principal.display_name,
            "active_role": principal.role.value,
            "represented_season_entry_id": scope["season_entry_id"],
            "team_name": scope["team_name"],
        },
        "season": {"season_id": scope["season_id"], "label": scope["season_label"]},
        "round": {
            "bbbffl_round_id": scope["bbbffl_round_id"],
            "label": scope["round_label"],
            "sequence": scope["sequence"],
        },
        "draft": asdict(draft),
        "submission": asdict(submission) if submission else None,
        "players": [asdict(player) for player in players if player is not None],
        "deferred": {position: value for position, value in deferred.items() if value},
        "carry_forward_source": asdict(source) if source else None,
        "carry_forward_message": None
        if source
        else "No previous submitted lineup is available. Enter this team manually.",
    }


@router.get("/rounds/{round_id}/lineup")
def view_lineup(round_id: str, request: Request, principal: Principal = Depends(require_proxy)):
    return _lineup_view(request, principal, _scope(request, principal, round_id))


@router.put("/rounds/{round_id}/lineup/draft")
def save_draft(round_id: str, payload: DraftRequest, request: Request, principal: Principal = Depends(require_proxy)):
    scope = _scope(request, principal, round_id)
    _csrf(request, principal)
    LineupProxyService(request.app.state.database, request.app.state.afl_client).create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        round_id,
        scope["season_entry_id"],
        payload.positions,
        expected_revision=payload.expected_revision,
        actor=_actor(principal),
    )
    return _lineup_view(request, principal, scope)


@router.post("/rounds/{round_id}/lineup/submit")
def submit(round_id: str, payload: SubmitRequest, request: Request, principal: Principal = Depends(require_proxy)):
    scope = _scope(request, principal, round_id)
    _csrf(request, principal)
    proxy = LineupProxyService(request.app.state.database, request.app.state.afl_client)
    # Submit is intentionally save-then-submit on the server. The browser's
    # visible choices and vacancy confirmation therefore describe the exact
    # persisted revision which becomes authoritative, even when the operator
    # never clicked the separate Save Draft convenience action.
    draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        round_id,
        scope["season_entry_id"],
        payload.positions,
        expected_revision=payload.expected_draft_revision,
        actor=_actor(principal),
    )
    proxy.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=payload.expected_submission_version,
        actor=_actor(principal),
        reason=payload.reason,
        lock_guard=_guard(request),
    )
    return _lineup_view(request, principal, scope)


@router.post("/rounds/{round_id}/lineup/carry-forward")
def carry_forward(
    round_id: str, payload: CarryRequest, request: Request, principal: Principal = Depends(require_proxy)
):
    scope = _scope(request, principal, round_id)
    _csrf(request, principal)
    CarryForwardService(request.app.state.database, request.app.state.afl_client).carry_forward(
        scope["season_id"],
        scope["competition_id"],
        round_id,
        scope["season_entry_id"],
        expected_submission_version=payload.expected_submission_version,
        actor=_actor(principal),
        reason=payload.reason or "Explicit replay carry-forward",
        lock_guard=_guard(request),
    )
    return _lineup_view(request, principal, scope)


def _describe_owned_players(request: Request, entry_id: str) -> list[dict]:
    """Structured, server-scoped owned-player data for one represented
    entry -- never an arbitrary player query filtered client-side (issue
    #131 #4): `current_squad` is already authoritatively scoped to
    `entry_id`, and every player is then resolved through the same
    `PlayerPoolRepository.get_by_id` the delegated lineup view uses."""
    ownership = CoachLineupService(request.app.state.database, request.app.state.afl_client).ownership
    pool = request.app.state.player_pool
    players = []
    for period in ownership.current_squad(entry_id):
        player = pool.get_by_id(period.season_player_id)
        if player is None:
            continue
        players.append(
            {
                "season_player_id": player.season_player_id,
                "display_name": player.display_name,
                "afl_club_id": player.afl_team_id,
                "afl_club_name": player.afl_team_name,
                "canonical_player_id": player.canonical_player_id,
            }
        )
    return players


def _describe_nomination(request: Request, nomination, rules_by_id: dict[str, dict]) -> dict:
    """Operator-readable presentation of one nomination -- player name/club,
    the accepted rule's human-readable labels, and audited correction
    history -- alongside every existing field (issue #131 #7). Never
    mutates the nomination or its audit trail; this only reads and joins
    already-persisted state."""
    state = request.app.state
    player = state.player_pool.get_by_id(nomination.season_player_id)
    operator = state.identities.get_coach(nomination.actor_id) if nomination.actor_id else None
    rule_view = rules_by_id.get(nomination.rule_id)
    events = state.audit_events.list_events(entity_type=ENTITY_TYPE_NOMINATION, entity_id=nomination.nomination_id)
    corrections = [
        {
            "occurred_at": event.occurred_at,
            "reason": event.reason,
            "before_state": event.before_state,
            "after_state": event.after_state,
            "actor_id": event.actor_id,
            "actor_role": event.actor_role,
        }
        for event in events
        if event.action == NOMINATION_CORRECTED
    ]
    return {
        **asdict(nomination),
        "player_display_name": player.display_name if player else None,
        "afl_club_id": player.afl_team_id if player else None,
        "afl_club_name": player.afl_team_name if player else None,
        "rule_display_label": rule_view["display_label"] if rule_view else None,
        "afl_opening_round_label": rule_view["afl_opening_round_label"] if rule_view else None,
        "afl_bye_round_label": rule_view["afl_bye_round_label"] if rule_view else None,
        "bbbffl_round_label": rule_view["bbbffl_round_label"] if rule_view else None,
        "entered_by_display_name": operator.display_name if operator else nomination.actor_id,
        "provenance": "replay/reconstructed",
        "correction_history": corrections,
    }


@router.get("/seasons/{season_id}/opening-round")
def opening_round(season_id: str, request: Request, principal: Principal = Depends(require_nomination)):
    require_role_covers_season(request, principal, season_id)
    entry_id = principal.represented_season_entry_id
    if entry_id is None:
        raise HTTPException(409, "Choose a represented team in acting context first")
    require_entry_context(request, principal, entry_id)
    entry = request.app.state.identities.get_public_team(entry_id)
    if entry is None or entry.season_id != season_id:
        raise HTTPException(404, "Private resource not found")
    database = request.app.state.database
    repo = OpeningRoundNominationRepository(database)
    rules = describe_accepted_rules(database, request.app.state.afl_client, season_id)
    rules_by_id = {rule["rule_id"]: rule for rule in rules}
    nominations = [
        _describe_nomination(request, nomination, rules_by_id)
        for rule in rules
        for nomination in repo.list_for_round(rule["bbbffl_round_id"])
        if nomination.season_entry_id == entry_id
    ]
    readiness = build_opening_round_readiness(database, season_id).for_entry(entry_id)
    return {
        "represented_entry": {"season_entry_id": entry_id, "team_name": entry.team_name},
        "rules": rules,
        "players": _describe_owned_players(request, entry_id),
        "nominations": nominations,
        "readiness": asdict(readiness) if readiness else None,
        "provenance_notice": f"Replay/reconstructed nominations are entered by {principal.display_name}; they are not historical coach evidence.",
    }


@router.post("/seasons/{season_id}/opening-round/nominations")
def nominate(
    season_id: str, payload: NominationRequest, request: Request, principal: Principal = Depends(require_nomination)
):
    view = opening_round(season_id, request, principal)
    _csrf(request, principal)
    rule = OpeningRoundRuleRepository(request.app.state.database).resolve_by_id(payload.rule_id)
    if rule is None or rule.season_id != season_id:
        raise HTTPException(404, "Accepted Opening Round rule not found for this season")
    entry_id = view["represented_entry"]["season_entry_id"]
    repo = OpeningRoundNominationRepository(request.app.state.database)
    nomination = repo.nominate(
        payload.rule_id,
        entry_id,
        payload.position,
        payload.season_player_id,
        request.app.state.afl_client,
        actor=_actor(principal),
        reason=f"Replay/reconstructed input: {payload.reason}",
    )
    target = request.app.state.database.execute(
        "SELECT competition_id FROM bbbffl_round WHERE bbbffl_round_id=?", (rule.bbbffl_round_id,)
    ).fetchone()
    repo.preload_target_lineup(
        request.app.state.lineups, season_id, target["competition_id"], rule.bbbffl_round_id, entry_id
    )
    rules_by_id = {r["rule_id"]: r for r in view["rules"]}
    return {
        "nomination": _describe_nomination(request, nomination, rules_by_id),
        "provenance": "replay/reconstructed",
        "entered_by": principal.display_name,
    }


@router.patch("/seasons/{season_id}/opening-round/nominations/{nomination_id}")
def correct(
    season_id: str,
    nomination_id: str,
    payload: CorrectionRequest,
    request: Request,
    principal: Principal = Depends(require_nomination),
):
    view = opening_round(season_id, request, principal)
    _csrf(request, principal)
    repo = OpeningRoundNominationRepository(request.app.state.database)
    existing = repo.get(nomination_id)
    if (
        existing is None
        or existing.season_id != season_id
        or existing.season_entry_id != principal.represented_season_entry_id
    ):
        raise HTTPException(404, "Nomination not found in represented context")
    corrected = repo.correct(
        nomination_id,
        position=payload.position,
        season_player_id=payload.season_player_id,
        actor=_actor(principal),
        reason=f"Replay/reconstructed input correction: {payload.reason}",
    )
    rules_by_id = {r["rule_id"]: r for r in view["rules"]}
    return {
        "nomination": _describe_nomination(request, corrected, rules_by_id),
        "provenance": "replay/reconstructed",
        "entered_by": principal.display_name,
    }


@page_router.get("/operations/rounds/{round_id}/lineup", response_class=HTMLResponse)
def lineup_page(round_id: str, request: Request):
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(request, "delegated_lineup.html", {"round_id": round_id, "csrf_token": token})
    response.set_cookie(
        "bbbffl_csrf",
        token,
        max_age=3600,
        httponly=True,
        secure=request.app.state.settings.is_production,
        samesite="lax",
    )
    return response


@page_router.get("/operations/seasons/{season_id}/opening-round", response_class=HTMLResponse)
def opening_round_page(season_id: str, request: Request):
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(
        request, "opening_round_operations.html", {"season_id": season_id, "csrf_token": token}
    )
    response.set_cookie(
        "bbbffl_csrf",
        token,
        max_age=3600,
        httponly=True,
        secure=request.app.state.settings.is_production,
        samesite="lax",
    )
    return response
