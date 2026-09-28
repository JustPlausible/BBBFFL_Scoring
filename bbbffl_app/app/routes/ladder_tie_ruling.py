"""Ladder tie ruling (issue #241): the Scorer/Administrator browser surface
for recording an audited manual resolution of an exact mathematical ladder
tie that the configured ladder criteria (competition points, percentage,
points for) cannot separate on their own.

Thin HTTP translation only -- every readiness check, staleness comparison
and the persisted ruling itself live in `app.ladder_tie_ruling`. Authority
mirrors `app.routes.season_activation` exactly: Scorer or Administrator only
(`app.authorization.require_scorer_or_admin`), plus the existing
season-scoped `require_role_covers_season` check every other season-model
route uses. Recording a ruling requires an explicit reason and -- for a
cookie-authenticated session -- the double-submit CSRF token, exactly like
`app.routes.season_setup`/`app.routes.season_activation`. This is
deliberately the *only* route that can ever write a `ladder_tie_ruling` row:
there is no bare JSON/CLI shortcut, matching the issue's "exceptional
competition-governance action, not a routine ladder-editing screen" scope.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_role_covers_season, require_scorer_or_admin, resolve_principal
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token
from app.ladder_tie_ruling import preview, record_ruling_for_season

router = APIRouter(prefix="/api/scorer/ladder-tie-ruling")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


class RecordRulingRequest(BaseModel):
    decided_order: list[str]
    reason: str | None = None


def require_ladder_tie_ruling_operator(principal: Principal = Depends(resolve_principal)) -> Principal:
    """Route-level `Depends`-ready wrapper, matching `app.routes.
    season_activation`'s identical local-wrapper convention."""
    return require_scorer_or_admin(principal)


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _require_session_csrf(request: Request, principal: Principal) -> None:
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _require_known_season(request: Request, season_id: str) -> None:
    if request.app.state.seasons.get_season(season_id) is None:
        raise HTTPException(status_code=404, detail="Unknown season")


def _open_tie_payload(open_tie) -> dict:
    payload = dataclasses.asdict(open_tie)
    if open_tie.ruling is not None:
        payload["ruling"] = dataclasses.asdict(open_tie.ruling)
    return payload


def _team_names(request: Request, season_id: str) -> dict:
    """`request.app.state.identities` is the already-constructed
    `IdentityRepository` instance every other route reaches through --
    never imported directly here (see `app.routes.season_centre`'s
    identical convention) -- so team names can be shown alongside the raw
    `season_entry_id` values `app.ladder_tie_ruling` deals in."""
    return {entry.season_entry_id: entry.team_name for entry in request.app.state.identities.list_entries(season_id)}


def _preview_payload(request: Request, report: dict) -> dict:
    names = _team_names(request, report["season_id"])
    return {
        "season_id": report["season_id"],
        "competition_id": report["competition_id"],
        "through_round": report["through_round"],
        "team_names": names,
        "open_ties": [_open_tie_payload(open_tie) for open_tie in report["open_ties"]],
    }


@router.get("/{season_id}")
def ladder_tie_ruling_preview(
    season_id: str, request: Request, principal: Principal = Depends(require_ladder_tie_ruling_operator)
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    report = preview(request.app.state.database, season_id)
    return _preview_payload(request, report)


@router.post("/{season_id}/rulings")
def ladder_tie_ruling_record(
    season_id: str,
    payload: RecordRulingRequest,
    request: Request,
    principal: Principal = Depends(require_ladder_tie_ruling_operator),
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    _require_session_csrf(request, principal)
    database = request.app.state.database
    ruling, created = record_ruling_for_season(
        database, season_id, payload.decided_order, actor=_actor(principal), reason=payload.reason or ""
    )
    return {
        "created": created,
        "ruling": dataclasses.asdict(ruling),
        "preview": _preview_payload(request, preview(database, season_id)),
    }


@page_router.get("/scorer/ladder-tie-ruling/{season_id}", response_class=HTMLResponse)
def ladder_tie_ruling_page(season_id: str, request: Request):
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(
        request, "ladder_tie_ruling.html", {"season_id": season_id, "csrf_token": token}
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
