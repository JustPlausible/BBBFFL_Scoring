"""Scorer/admin controls.

Deliberately small: mark/unmark DNP, assign/clear interchange, set/clear a
direct score override with a reason, and finalise. All actions are
reversible until the Grand Final is finalised, at which point the decisions
store is locked (HTTP 423) so results become stable for historical
inspection.

If BBBFFL_ADMIN_TOKEN is set, every endpoint here requires a matching
`X-Admin-Token` header. This is a lightweight gate suitable for a single
trusted scorer on a home-server prototype, not general-purpose auth.

Every mutation below also records an immutable audit event in the same
transaction as its domain write (see app/audit.py and
docs/audit-events.md). GET /audit-events is a tiny read-only diagnostic
surface over that trail -- not an audit UI.
"""

import dataclasses

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.config import BASE_DIR
from app.scoring import ROSTER_SLOTS, SCORABLE_POSITIONS
from app.service import build_matchup_state, get_matchup_view
from app.superscore import superscore_round_label

# The admin surface today is one shared token, not a per-person login (see
# require_admin below and roadmap package 19/20). Every mutation is
# attributed to this well-defined, non-impersonating actor -- see
# app/audit.py's module docstring for why "anonymous_operator" rather than
# inventing a fake authenticated identity. actor_role still distinguishes
# ordinary scorer duties from the privileged finalisation action.
SCORER_ACTOR = ActorContext.anonymous_operator(role="scorer")
ADMIN_ACTOR = ActorContext.anonymous_operator(role="admin")

router = APIRouter(prefix="/api/admin")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


class DnpRequest(BaseModel):
    team_key: str
    slot: str
    dnp: bool


class InterchangeRequest(BaseModel):
    team_key: str
    target_position: str | None = None


class OverrideRequest(BaseModel):
    team_key: str
    position: str
    override_score: float | None = None
    reason: str | None = None


class FinalizeRequest(BaseModel):
    note: str | None = None


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    settings = request.app.state.settings
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token")


def _team_keys(request: Request) -> set[str]:
    return {t.team_key for t in request.app.state.teams}


def _ensure_valid_team(request: Request, team_key: str) -> None:
    if team_key not in _team_keys(request):
        raise HTTPException(status_code=404, detail=f"Unknown team_key: {team_key}")


def _ensure_not_finalized(request: Request) -> None:
    if request.app.state.decisions.get_matchup_state().finalized:
        raise HTTPException(
            status_code=423, detail="Grand Final already finalised; decisions are locked"
        )


def _current_state(request: Request) -> dict:
    state = request.app.state
    return get_matchup_view(state.afl_client, state.teams, state.decisions, state.identity_cache)


@router.get("/state", dependencies=[Depends(require_admin)])
def admin_state(request: Request):
    return _current_state(request)


@router.post("/dnp", dependencies=[Depends(require_admin)])
def set_dnp(payload: DnpRequest, request: Request):
    _ensure_not_finalized(request)
    _ensure_valid_team(request, payload.team_key)
    if payload.slot not in ROSTER_SLOTS:
        raise HTTPException(status_code=400, detail=f"Unknown slot: {payload.slot}")
    request.app.state.decisions.set_dnp(payload.team_key, payload.slot, payload.dnp, actor=SCORER_ACTOR)
    return _current_state(request)


@router.post("/interchange", dependencies=[Depends(require_admin)])
def set_interchange(payload: InterchangeRequest, request: Request):
    _ensure_not_finalized(request)
    _ensure_valid_team(request, payload.team_key)
    if payload.target_position is not None and payload.target_position not in SCORABLE_POSITIONS:
        raise HTTPException(
            status_code=400, detail=f"Invalid target_position: {payload.target_position}"
        )
    request.app.state.decisions.set_interchange_assignment(
        payload.team_key, payload.target_position, actor=SCORER_ACTOR
    )
    return _current_state(request)


@router.post("/override", dependencies=[Depends(require_admin)])
def set_override(payload: OverrideRequest, request: Request):
    _ensure_not_finalized(request)
    _ensure_valid_team(request, payload.team_key)
    if payload.position not in SCORABLE_POSITIONS:
        raise HTTPException(status_code=400, detail=f"Invalid position: {payload.position}")
    request.app.state.decisions.set_override(
        payload.team_key, payload.position, payload.override_score, payload.reason, actor=SCORER_ACTOR
    )
    return _current_state(request)


@router.post("/finalize", dependencies=[Depends(require_admin)])
def finalize(payload: FinalizeRequest, request: Request):
    state = request.app.state
    # Compute live state exactly once here and reuse it as the frozen
    # snapshot -- a second afl-api round trip after finalize() commits would
    # mean a transient afl-api failure could make the endpoint report
    # failure for an already-irreversible finalisation (and a retry would
    # then 423, since the matchup is now locked).
    result = build_matchup_state(state.afl_client, state.teams, state.decisions, state.identity_cache)
    if result.status != "AWAITING_SCORER_SIGNOFF":
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot finalise until all relevant AFL matches are complete "
                "(status must be AWAITING_SCORER_SIGNOFF)."
            ),
        )
    snapshot = dataclasses.asdict(result)
    state.decisions.finalize(payload.note, snapshot, actor=ADMIN_ACTOR)
    return _current_state(request)


@router.get("/audit-events", dependencies=[Depends(require_admin)])
def list_audit_events(
    request: Request,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    correlation_id: str | None = None,
    limit: int = 200,
):
    """Tiny read-only diagnostic surface over the audit trail -- proves the
    append-only boundary end-to-end, not an admin audit UI. Deterministic
    chronological order (see AuditEventRepository.list_events)."""
    events = request.app.state.audit_events.list_events(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        correlation_id=correlation_id,
        limit=limit,
    )
    return [dataclasses.asdict(event) for event in events]


@page_router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    superscore_config = request.app.state.superscore_config
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "positions": list(SCORABLE_POSITIONS),
            "teams": request.app.state.teams,
            "superscore_enabled": superscore_config is not None,
            "superscore_round_label": (
                superscore_round_label(superscore_config.afl_round) if superscore_config else None
            ),
        },
    )
