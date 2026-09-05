"""Scorer/Admin authorised correction of an already-locked weekly lineup
(issue #137) -- the Season Operations surface for
`app.lineup_correction.LineupCorrectionService`.

This is deliberately a distinct, narrower authority from the ordinary
delegated/proxy lineup surface (`app/routes/delegated_operations.py`):
`require_capability("lineup.correct_locked")` never falls back to generic
`lineup.proxy` authority, and every mutating request is additionally
season-scoped via `require_role_covers_season` -- the same pattern
`app/routes/round_review.py` already uses -- so a Scorer/Replay-Operator
role granted for one season can never correct a locked lineup in another.
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import (
    Principal,
    principal_has_capability,
    require_authenticated,
    require_role_covers_season,
    resolve_principal,
)
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token
from app.lineup_correction import LineupCorrectionService

router = APIRouter(prefix="/api/admin/lineup-correction")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

CAPABILITY = "lineup.correct_locked"


def require_lineup_corrector(principal: Principal = Depends(resolve_principal)) -> Principal:
    require_authenticated(principal)
    if not principal_has_capability(principal, CAPABILITY):
        raise HTTPException(status_code=403, detail="Locked-lineup correction authority required")
    return principal


def _actor(principal: Principal) -> ActorContext:
    return ActorContext("anonymous_operator", principal.coach_id, principal.role.value)


def _csrf(request: Request, principal: Principal) -> None:
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(403, "Invalid CSRF token")


def _authorise_round(request: Request, principal: Principal, round_id: str) -> dict:
    """Resolve the target round server-side, then apply season-scope --
    never trust a browser-supplied season identifier as authority."""
    row = request.app.state.database.execute(
        "SELECT r.bbbffl_round_id, r.label round_label, r.sequence, r.competition_id, "
        "c.season_id, s.label season_label, l.state round_state "
        "FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "JOIN bbbffl_season s ON s.season_id=c.season_id "
        "LEFT JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id=r.bbbffl_round_id "
        "WHERE r.bbbffl_round_id=? AND c.stream_type='ordinary'",
        (round_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown ordinary BBBFFL round")
    require_role_covers_season(request, principal, row["season_id"])
    return dict(row)


def _authorise_entry(request: Request, scope: dict, season_entry_id: str) -> dict:
    entry = request.app.state.identities.get_public_team(season_entry_id)
    if entry is None or entry.season_id != scope["season_id"]:
        raise HTTPException(status_code=404, detail="Private resource not found")
    coach = request.app.state.identities.get_current_coach(season_entry_id)
    return {"team_name": entry.team_name, "coach_name": coach.display_name if coach else None}


class CorrectionRequest(BaseModel):
    expected_submission_version: int
    position_changes: dict[str, str | None]
    reason: str


def _candidate_view(candidate, entry_meta: dict) -> dict:
    view = asdict(candidate)
    view["team_name"] = entry_meta.get("team_name")
    view["coach_name"] = entry_meta.get("coach_name")
    return view


@router.get("/{round_id}")
def list_round_entries(round_id: str, request: Request, principal: Principal = Depends(require_lineup_corrector)):
    """Season/round/team selection step: every BBBFFL team competing in this
    round, human-readable, for the operator to pick a target from."""
    scope = _authorise_round(request, principal, round_id)
    state = request.app.state
    matchups = state.lifecycle.list_matchups(round_id)
    entry_ids = sorted(
        {matchup.home_season_entry_id for matchup in matchups} | {m.away_season_entry_id for m in matchups}
    )
    entries = []
    for entry_id in entry_ids:
        team = state.identities.get_public_team(entry_id)
        coach = state.identities.get_current_coach(entry_id)
        entries.append(
            {
                "season_entry_id": entry_id,
                "team_name": team.team_name if team else None,
                "coach_name": coach.display_name if coach else None,
            }
        )
    return {
        "round": {
            "bbbffl_round_id": scope["bbbffl_round_id"],
            "label": scope["round_label"],
            "sequence": scope["sequence"],
            "state": scope["round_state"],
        },
        "season": {"season_id": scope["season_id"], "label": scope["season_label"]},
        "entries": entries,
    }


@router.get("/{round_id}/{season_entry_id}")
def get_correction_candidate(
    round_id: str, season_entry_id: str, request: Request, principal: Principal = Depends(require_lineup_corrector)
):
    scope = _authorise_round(request, principal, round_id)
    entry_meta = _authorise_entry(request, scope, season_entry_id)
    service = LineupCorrectionService(request.app.state.database, request.app.state.afl_client)
    candidate = service.describe(scope["season_id"], scope["competition_id"], round_id, season_entry_id)
    return _candidate_view(candidate, entry_meta)


@router.post("/{round_id}/{season_entry_id}/correct")
def correct_locked_lineup(
    round_id: str,
    season_entry_id: str,
    payload: CorrectionRequest,
    request: Request,
    principal: Principal = Depends(require_lineup_corrector),
):
    scope = _authorise_round(request, principal, round_id)
    entry_meta = _authorise_entry(request, scope, season_entry_id)
    _csrf(request, principal)
    if not payload.reason or not payload.reason.strip():
        raise HTTPException(status_code=400, detail="A substantive reason is required to correct a locked lineup")
    service = LineupCorrectionService(request.app.state.database, request.app.state.afl_client)
    service.correct(
        scope["season_id"],
        scope["competition_id"],
        round_id,
        season_entry_id,
        payload.position_changes,
        expected_submission_version=payload.expected_submission_version,
        actor=_actor(principal),
        reason=payload.reason,
    )
    candidate = service.describe(scope["season_id"], scope["competition_id"], round_id, season_entry_id)
    return _candidate_view(candidate, entry_meta)


@page_router.get("/scorer/lineup-correction", response_class=HTMLResponse)
@page_router.get("/scorer/lineup-correction/{round_id}", response_class=HTMLResponse)
def lineup_correction_page(request: Request, round_id: str | None = None):
    """Browser shell; private reads and every mutation remain API-authorised."""
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(
        request, "lineup_correction.html", {"round_id": round_id, "csrf_token": token}
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
