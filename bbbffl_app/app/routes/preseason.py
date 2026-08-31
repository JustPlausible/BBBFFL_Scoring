"""Human-facing preseason squad operations over the authoritative ledger.

The read model below is rebuilt from IdentityRepository, PlayerPoolRepository,
OwnershipRepository and PreseasonRepository on every request.  Commands are
thin calls into PreseasonRepository; this module never owns or mutates a
second squad representation.
"""

import dataclasses

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext, AuditEventRepository
from app.authorization import Principal, require_capability, require_role_covers_season
from app.config import BASE_DIR
from app.routes.admin import require_admin

router = APIRouter(prefix="/api/admin/preseason")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


class OpenWindowRequest(BaseModel):
    reason: str | None = None


class TradeLegRequest(BaseModel):
    season_player_id: str
    from_season_entry_id: str
    to_season_entry_id: str


class SubmitTradeRequest(BaseModel):
    legs: list[TradeLegRequest]
    scorer_name: str | None = None  # legacy shared-token audit label
    reason: str | None = None


class CloseWindowRequest(BaseModel):
    reason: str | None = None


class CorrectSnapshotRequest(BaseModel):
    season_entry_id: str
    remove_season_player_id: str
    add_season_player_id: str
    reason: str
    scorer_name: str | None = None


def _actor(principal: Principal, legacy_name: str | None = None) -> ActorContext:
    return ActorContext(
        actor_type="anonymous_operator",
        actor_id=principal.coach_id or legacy_name,
        actor_role=principal.role.value,
    )


def _authorise(request: Request, principal: Principal, season_id: str) -> None:
    require_role_covers_season(request, principal, season_id)


def _entry_identity(request: Request, entry_id: str) -> dict:
    entry = request.app.state.identities.get_public_team(entry_id)
    coach = request.app.state.identities.get_current_coach(entry_id)
    return {
        "season_entry_id": entry_id,
        "team_name": entry.team_name if entry else "Unknown team",
        "coach_display_name": coach.display_name if coach else "Coach not assigned",
    }


def _status(request: Request, season_id: str) -> dict:
    preseason = request.app.state.preseason
    window = preseason.get_window(season_id)
    issues = preseason.validate_squads(season_id)
    issue_by_entry = {item.get("season_entry_id"): item for item in issues}
    config = request.app.state.database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
    ).fetchone()
    squad_limit = config["squad_limit"] if config else None
    pool = {item.season_player_id: item for item in request.app.state.player_pool.browse(season_id, limit=10000)}
    teams = []
    for entry in request.app.state.identities.list_entries(season_id):
        identity = _entry_identity(request, entry.season_entry_id)
        periods = preseason.ownership.current_squad(entry.season_entry_id)
        players = []
        for period in periods:
            player = pool.get(period.season_player_id)
            players.append(
                {
                    "season_player_id": period.season_player_id,
                    "canonical_player_id": player.canonical_player_id if player else None,
                    "display_name": player.display_name if player else "Unknown player",
                    "afl_team_name": player.afl_team_name if player else None,
                }
            )
        players.sort(key=lambda item: (item["display_name"].casefold(), str(item["canonical_player_id"])))
        issue = issue_by_entry.get(entry.season_entry_id)
        teams.append(
            {
                **identity,
                "players": players,
                "squad_count": len(players),
                "squad_limit": squad_limit,
                "ready": issue is None and squad_limit is not None,
                "readiness_problem": issue["problem"] if issue else None,
            }
        )
    teams.sort(key=lambda item: item["team_name"].casefold())

    event_repo = AuditEventRepository(request.app.state.database)
    history = []
    for trade in preseason.list_trades(season_id):
        event = event_repo.get_event(trade.audit_event_id)
        actor_name = None
        if event and event.actor_id:
            coach = request.app.state.identities.get_coach(event.actor_id)
            actor_name = coach.display_name if coach else event.actor_id
        legs = []
        for leg in preseason.trade_legs(trade.trade_id):
            player = pool.get(leg.season_player_id)
            legs.append(
                {
                    **dataclasses.asdict(leg),
                    "player_name": player.display_name if player else leg.season_player_id,
                    "from_team_name": _entry_identity(request, leg.from_season_entry_id)["team_name"],
                    "to_team_name": _entry_identity(request, leg.to_season_entry_id)["team_name"],
                }
            )
        history.append(
            {
                "trade": dataclasses.asdict(trade),
                "legs": legs,
                "actor_name": actor_name or "Shared-token operator",
                "actor_role": event.actor_role if event else None,
            }
        )
    snapshot = preseason.current_snapshot(season_id) if window else None
    return {
        "season_id": season_id,
        "window": dataclasses.asdict(window) if window else None,
        "squad_limit": squad_limit,
        "squad_issues": issues,
        "all_squads_ready": not issues and bool(teams),
        "opening_snapshot": dataclasses.asdict(snapshot) if snapshot else None,
        "teams": teams,
        "trades": history,
        "available_players": [dataclasses.asdict(item) for item in pool.values() if item.availability == "available"],
        "next_step_url": f"/admin/season-centre/{season_id}",
    }


manage = require_capability("preseason.manage")


@router.get("/{season_id}/status")
def status(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    return _status(request, season_id)


@router.post("/{season_id}/open")
def open_window(season_id: str, payload: OpenWindowRequest, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    request.app.state.preseason.open_window(season_id, actor=_actor(principal), reason=payload.reason)
    return _status(request, season_id)


@router.post("/{season_id}/trade")
def submit_trade(season_id: str, payload: SubmitTradeRequest, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    trade = request.app.state.preseason.submit_trade(
        season_id,
        [leg.model_dump() for leg in payload.legs],
        actor=_actor(principal, payload.scorer_name),
        reason=payload.reason,
    )
    return {
        "trade": dataclasses.asdict(trade),
        "legs": [dataclasses.asdict(x) for x in request.app.state.preseason.trade_legs(trade.trade_id)],
    }


@router.get("/{season_id}/trades")
def list_trades(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    return _status(request, season_id)["trades"]


@router.post("/{season_id}/close")
def close_window(season_id: str, payload: CloseWindowRequest, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    request.app.state.preseason.close_window(season_id, actor=_actor(principal), reason=payload.reason)
    return _status(request, season_id)


@router.get("/{season_id}/opening-squad")
def opening_squad(
    season_id: str, request: Request, season_entry_id: str | None = None, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    return [
        dataclasses.asdict(entry) for entry in request.app.state.preseason.opening_squad(season_id, season_entry_id)
    ]


# Post-freeze correction remains exceptional Administrator functionality; it
# is not exposed by the ordinary preseason page.
@router.post("/{season_id}/correct-opening-squad", dependencies=[Depends(require_admin)])
def correct_opening_squad(season_id: str, payload: CorrectSnapshotRequest, request: Request):
    request.app.state.preseason.correct_opening_snapshot(
        season_id,
        payload.season_entry_id,
        remove_season_player_id=payload.remove_season_player_id,
        add_season_player_id=payload.add_season_player_id,
        actor=ActorContext(actor_type="anonymous_operator", actor_id=payload.scorer_name, actor_role="admin"),
        reason=payload.reason,
    )
    return _status(request, season_id)


@page_router.get("/admin/preseason/{season_id}", response_class=HTMLResponse)
def preseason_page(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    return templates.TemplateResponse(request, "preseason.html", {"season_id": season_id})
