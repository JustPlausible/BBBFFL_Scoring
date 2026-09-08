"""Scorer-operated mid-season draft workflow (issue #164).

A thin JSON operator surface over `app.midseason_draft.MidseasonDraftRepository`
-- every mutating endpoint here is a direct translation from an HTTP request
to one repository call; turn sequencing, ownership, audit and validation all
live in that repository (and, once selections are generated, in
`app.draft.DraftRepository`). No business logic lives in this module.

For the 2026 replay this surface is deliberately proxy/operator-shaped
(Scorer/Admin/Replay Operator submit on a team's behalf); a dedicated
coach-facing page is future 2027 work and is not required for this issue --
see the module docstring in `app.midseason_draft` and
docs/midseason-draft-planning.md.
"""

import dataclasses

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_capability, require_role_covers_season

router = APIRouter(prefix="/api/admin/midseason-draft")

manage = require_capability("midseason_draft.manage")


def _actor(principal: Principal, legacy_name: str | None = None) -> ActorContext:
    return ActorContext(
        actor_type="anonymous_operator",
        actor_id=principal.coach_id or legacy_name,
        actor_role=principal.role.value,
    )


def _authorise(request: Request, principal: Principal, season_id: str) -> None:
    require_role_covers_season(request, principal, season_id)


class ConfirmLadderRequest(BaseModel):
    competition_id: str
    reason: str | None = None
    scorer_name: str | None = None


class OpenDelistingRequest(BaseModel):
    reason: str | None = None
    scorer_name: str | None = None


class OverrideOrderRequest(BaseModel):
    ordered_season_entry_ids: list[str]
    reason: str
    scorer_name: str | None = None


class SubmitDelistingRequest(BaseModel):
    season_entry_id: str
    season_player_id: str
    reason: str | None = None
    scorer_name: str | None = None


class WithdrawDelistingRequest(BaseModel):
    reason: str | None = None
    scorer_name: str | None = None


class TradeLegRequest(BaseModel):
    leg_type: str
    from_season_entry_id: str
    to_season_entry_id: str
    season_player_id: str | None = None
    draft_round: int | None = None


class ProposeTradeRequest(BaseModel):
    legs: list[TradeLegRequest]
    reason: str | None = None
    scorer_name: str | None = None


class DecideTradeRequest(BaseModel):
    approve: bool
    reason: str | None = None
    scorer_name: str | None = None


class LockDelistingsRequest(BaseModel):
    reason: str | None = None
    scorer_name: str | None = None


class GenerateSelectionsRequest(BaseModel):
    reason: str | None = None
    scorer_name: str | None = None


class PickRequest(BaseModel):
    season_entry_id: str
    season_player_id: str
    draft_pick_id: str | None = None
    scorer_name: str | None = None
    reason: str | None = None


class CorrectionRequest(BaseModel):
    draft_pick_id: str
    reason: str


class ReopenRequest(BaseModel):
    reason: str


class ClosePostDraftTradingRequest(BaseModel):
    reason: str | None = None
    scorer_name: str | None = None


def _status(request: Request, season_id: str) -> dict:
    midseason = request.app.state.midseason_draft
    draft = midseason.get_draft(season_id)
    if draft is None:
        return {"season_id": season_id, "draft": None}
    ladder = midseason.ladder_snapshot(season_id)
    return {
        "season_id": season_id,
        "draft": dataclasses.asdict(draft),
        "order": [
            {"position": position, "season_entry_id": entry_id, "source": source}
            for position, entry_id, source in midseason.draft_order(season_id)
        ],
        "ladder_snapshot": (
            {
                "snapshot_id": ladder.snapshot_id,
                "through_round": ladder.through_round,
                "created_at": ladder.created_at,
                "rows": [dataclasses.asdict(row) for row in ladder.rows],
            }
            if ladder
            else None
        ),
        "delistings": [dataclasses.asdict(item) for item in midseason.list_delistings(season_id)],
        "trades": [dataclasses.asdict(item) for item in midseason.list_trades(season_id)],
        "engine_status": (dataclasses.asdict(midseason.status(season_id)) if midseason.status(season_id) else None),
        "available_player_count": len(midseason.available_player_pool(season_id)),
    }


@router.get("/{season_id}/status")
def status(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    return _status(request, season_id)


@router.post("/{season_id}/confirm-ladder")
def confirm_ladder(
    season_id: str, payload: ConfirmLadderRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.confirm_ladder(
        season_id, payload.competition_id, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return _status(request, season_id)


@router.post("/{season_id}/override-order")
def override_order(
    season_id: str, payload: OverrideOrderRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.override_draft_order(
        season_id, payload.ordered_season_entry_ids, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return _status(request, season_id)


@router.post("/{season_id}/open-delisting-window")
def open_delisting_window(
    season_id: str, payload: OpenDelistingRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.open_delisting_window(
        season_id, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return _status(request, season_id)


@router.post("/{season_id}/delisting")
def submit_delisting(
    season_id: str, payload: SubmitDelistingRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.submit_delisting(
        season_id,
        payload.season_entry_id,
        payload.season_player_id,
        actor=_actor(principal, payload.scorer_name),
        reason=payload.reason,
    )
    return _status(request, season_id)


@router.post("/{season_id}/delisting/{delisting_id}/withdraw")
def withdraw_delisting(
    season_id: str,
    delisting_id: str,
    payload: WithdrawDelistingRequest,
    request: Request,
    principal: Principal = Depends(manage),
):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.withdraw_delisting(
        season_id, delisting_id, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return _status(request, season_id)


@router.post("/{season_id}/trade")
def propose_trade(
    season_id: str, payload: ProposeTradeRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    trade = request.app.state.midseason_draft.propose_trade(
        season_id,
        [leg.model_dump() for leg in payload.legs],
        actor=_actor(principal, payload.scorer_name),
        reason=payload.reason,
    )
    return {
        "trade": dataclasses.asdict(trade),
        "legs": [dataclasses.asdict(leg) for leg in request.app.state.midseason_draft.trade_legs(trade.trade_id)],
    }


@router.post("/{season_id}/trade/{trade_id}/decide")
def decide_trade(
    season_id: str, trade_id: str, payload: DecideTradeRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    trade = request.app.state.midseason_draft.decide_trade(
        season_id, trade_id, payload.approve, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return {"trade": dataclasses.asdict(trade)}


@router.post("/{season_id}/lock-delistings")
def lock_delistings(
    season_id: str, payload: LockDelistingsRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.lock_delistings(
        season_id, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return _status(request, season_id)


@router.post("/{season_id}/generate-selections")
def generate_selections(
    season_id: str, payload: GenerateSelectionsRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.generate_selection_table(
        season_id, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return _status(request, season_id)


@router.get("/{season_id}/available-players")
def available_players(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    return [dataclasses.asdict(item) for item in request.app.state.midseason_draft.available_player_pool(season_id)]


@router.get("/{season_id}/picks")
def picks(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    return [dataclasses.asdict(item) for item in request.app.state.midseason_draft.picks(season_id)]


@router.post("/{season_id}/pick")
def submit_pick(season_id: str, payload: PickRequest, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.execute_pick(
        season_id,
        payload.season_entry_id,
        payload.season_player_id,
        pick_id=payload.draft_pick_id,
        actor=_actor(principal, payload.scorer_name),
        reason=payload.reason or "mid-season draft selection",
    )
    return _status(request, season_id)


@router.post("/{season_id}/correct-selection")
def correct_selection(
    season_id: str, payload: CorrectionRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.correct_selection(
        season_id, payload.draft_pick_id, actor=_actor(principal), reason=payload.reason
    )
    return _status(request, season_id)


@router.post("/{season_id}/reopen-draft")
def reopen_draft(season_id: str, payload: ReopenRequest, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.reopen_draft(season_id, actor=_actor(principal), reason=payload.reason)
    return _status(request, season_id)


@router.post("/{season_id}/close-post-draft-trading")
def close_post_draft_trading(
    season_id: str, payload: ClosePostDraftTradingRequest, request: Request, principal: Principal = Depends(manage)
):
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.close_post_draft_trading(
        season_id, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return _status(request, season_id)
