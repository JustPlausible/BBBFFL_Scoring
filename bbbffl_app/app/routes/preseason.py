"""Preseason transaction/finalisation window operator surface (roadmap
package 15, issue #54) -- extends the scorer/admin surface issue #53
introduced for the draft (see app/routes/draft.py) rather than creating an
unrelated second admin interface.

Every rule -- whether the window can open, whether a trade's legs are valid,
whether every opening squad is valid enough to close -- lives in
`app.preseason.PreseasonRepository`, inside one database transaction per
command. Nothing here decides any of that: each mutating endpoint is a thin
translation from an HTTP request to one `PreseasonRepository` call, and
every read rebuilds its response from the database.
"""

import dataclasses

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.audit import ActorContext
from app.routes.admin import require_admin

router = APIRouter(prefix="/api/admin/preseason")


class OpenWindowRequest(BaseModel):
    reason: str | None = None


class TradeLegRequest(BaseModel):
    season_player_id: str
    from_season_entry_id: str
    to_season_entry_id: str


class SubmitTradeRequest(BaseModel):
    legs: list[TradeLegRequest]
    scorer_name: str | None = None
    reason: str | None = None


class CloseWindowRequest(BaseModel):
    reason: str | None = None


class CorrectSnapshotRequest(BaseModel):
    season_entry_id: str
    remove_season_player_id: str
    add_season_player_id: str
    reason: str
    scorer_name: str | None = None


def _scorer_actor(scorer_name: str | None) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=scorer_name, actor_role="scorer")


def _window_view(window) -> dict | None:
    return dataclasses.asdict(window) if window else None


def _status(request: Request, season_id: str) -> dict:
    preseason = request.app.state.preseason
    window = preseason.get_window(season_id)
    squad_issues = preseason.validate_squads(season_id) if window else []
    snapshot = preseason.current_snapshot(season_id) if window else None
    return {
        "season_id": season_id,
        "window": _window_view(window),
        "squad_issues": squad_issues,
        "opening_snapshot": dataclasses.asdict(snapshot) if snapshot else None,
        "trades": [dataclasses.asdict(trade) for trade in preseason.list_trades(season_id)] if window else [],
    }


@router.get("/{season_id}/status", dependencies=[Depends(require_admin)])
def status(season_id: str, request: Request):
    return _status(request, season_id)


@router.post("/{season_id}/open", dependencies=[Depends(require_admin)])
def open_window(season_id: str, payload: OpenWindowRequest, request: Request):
    request.app.state.preseason.open_window(
        season_id, actor=ActorContext.anonymous_operator("admin"), reason=payload.reason
    )
    return _status(request, season_id)


@router.post("/{season_id}/trade", dependencies=[Depends(require_admin)])
def submit_trade(season_id: str, payload: SubmitTradeRequest, request: Request):
    trade = request.app.state.preseason.submit_trade(
        season_id,
        [leg.model_dump() for leg in payload.legs],
        actor=_scorer_actor(payload.scorer_name),
        reason=payload.reason,
    )
    legs = request.app.state.preseason.trade_legs(trade.trade_id)
    return {
        "trade": dataclasses.asdict(trade),
        "legs": [dataclasses.asdict(leg) for leg in legs],
    }


@router.get("/{season_id}/trades", dependencies=[Depends(require_admin)])
def list_trades(season_id: str, request: Request):
    preseason = request.app.state.preseason
    trades = preseason.list_trades(season_id)
    return [
        {
            "trade": dataclasses.asdict(trade),
            "legs": [dataclasses.asdict(leg) for leg in preseason.trade_legs(trade.trade_id)],
        }
        for trade in trades
    ]


@router.post("/{season_id}/close", dependencies=[Depends(require_admin)])
def close_window(season_id: str, payload: CloseWindowRequest, request: Request):
    request.app.state.preseason.close_window(
        season_id, actor=ActorContext.anonymous_operator("admin"), reason=payload.reason
    )
    return _status(request, season_id)


@router.get("/{season_id}/opening-squad", dependencies=[Depends(require_admin)])
def opening_squad(season_id: str, request: Request, season_entry_id: str | None = None):
    entries = request.app.state.preseason.opening_squad(season_id, season_entry_id)
    return [dataclasses.asdict(entry) for entry in entries]


@router.post("/{season_id}/correct-opening-squad", dependencies=[Depends(require_admin)])
def correct_opening_squad(season_id: str, payload: CorrectSnapshotRequest, request: Request):
    request.app.state.preseason.correct_opening_snapshot(
        season_id,
        payload.season_entry_id,
        remove_season_player_id=payload.remove_season_player_id,
        add_season_player_id=payload.add_season_player_id,
        actor=_scorer_actor(payload.scorer_name),
        reason=payload.reason,
    )
    return _status(request, season_id)
