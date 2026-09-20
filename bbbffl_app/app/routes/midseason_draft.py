"""Scorer/Admin lifecycle plus separate coach draft-board workflow for the
mid-season draft (issues #164, #181).

A thin JSON operator surface over `app.midseason_draft.MidseasonDraftRepository`
-- every mutating endpoint here is a direct translation from an HTTP request
to one repository call; turn sequencing, ownership, audit and validation all
live in that repository (and, once selections are generated, in
`app.draft.DraftRepository`). No business logic lives in this module.

Two authority tiers (issue #181 extends the original Scorer/Admin/Replay-
Operator-only surface):

- `manage` (`midseason_draft.manage`): the full Scorer/Admin/Replay-Operator
  lifecycle surface -- confirm the ladder, run the delisting/trade window,
  lock, generate selections, and every exceptional correction. Unchanged
  from issue #164.
- `participate` (`midseason_draft.participate`): backs the distinct
  `/account/midseason-draft/...` Coach surface. Its own endpoints require
  an active Coach role and an entry in the requested season; the
  `/api/admin/...` and `/admin/...` surfaces require `manage` throughout.

Board/player-browsing responses are built from `app.draft_board`'s shared,
draft-kind-parameterised helpers -- the exact same functions
`app.routes.draft` renders the preseason board from (issue #181's "reuse the
existing pre-season draft board/selection machinery" direction) -- so this
module never re-implements pick/readiness/player-browser presentation.
"""

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext, AuditEventRepository
from app.authorization import (
    Principal,
    Role,
    require_capability,
    require_coach,
    require_entry_context,
    require_role_covers_season,
)
from app.config import BASE_DIR
from app.draft_board import build_board, build_readiness, entry_view, player_browse_view, resolve_my_entry_id

router = APIRouter(prefix="/api/admin/midseason-draft")
coach_router = APIRouter(prefix="/api/account/midseason-draft")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

manage = require_capability("midseason_draft.manage")
participate = require_capability("midseason_draft.participate")


def coach_participant(principal: Principal = Depends(participate)) -> Principal:
    return require_coach(principal)


def _actor(principal: Principal, legacy_name: str | None = None) -> ActorContext:
    # Codex review, PR #225 (P2): a genuine Coach self-service action
    # (currently only `submit_pick`, via `midseason_draft.participate`)
    # must use `actor_type="coach"` (`ActorContext.coach`), never
    # `anonymous_operator` -- that type is reserved for the shared-token/
    # delegated-role proxy surface (see app/audit.py's module docstring).
    # Every other caller of this helper is gated behind
    # `midseason_draft.manage`, which Coach never holds, so this branch
    # only ever fires for the one genuinely coach-reachable endpoint.
    if principal.role is Role.COACH and principal.coach_id is not None:
        return ActorContext.coach(principal.coach_id)
    return ActorContext(
        actor_type="anonymous_operator",
        actor_id=principal.coach_id or legacy_name,
        actor_role=principal.role.value,
    )


def _authorise(request: Request, principal: Principal, season_id: str) -> None:
    require_role_covers_season(request, principal, season_id)


def _coach_entry_id(request: Request, principal: Principal, season_id: str) -> str:
    entry_id = resolve_my_entry_id(request, principal, season_id)
    if entry_id is None:
        raise HTTPException(status_code=404, detail="Private resource not found")
    return entry_id


def _entry_label(request: Request, season_entry_id: str | None, cache: dict) -> dict | None:
    if season_entry_id is None:
        return None
    return entry_view(request, season_entry_id, cache)


def _player_label(request: Request, season_player_id: str | None, cache: dict) -> dict | None:
    if season_player_id is None:
        return None
    if season_player_id not in cache:
        player = request.app.state.player_pool.get_by_id(season_player_id)
        cache[season_player_id] = {
            "season_player_id": season_player_id,
            "display_name": player.display_name if player else "Unknown player",
            "afl_team_name": player.afl_team_name if player else None,
        }
    return cache[season_player_id]


class SetTriggerRoundRequest(BaseModel):
    trigger_round: int
    reason: str | None = None
    scorer_name: str | None = None


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


class ReverseTradeApprovalRequest(BaseModel):
    reason: str
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
    """Issue #181's human-readable presentation: every field an earlier
    caller already relied on (`season_entry_id`, `season_player_id`, raw
    dataclass fields) is kept verbatim -- this only *adds* `team_name`/
    `coach_display_name`/player-label fields alongside them, so a raw id
    remains available as secondary/audit detail exactly as the issue asks,
    without breaking any existing reader of this response."""
    midseason = request.app.state.midseason_draft
    draft = midseason.get_draft(season_id)
    if draft is None:
        return {"season_id": season_id, "draft": None}
    ladder = midseason.ladder_snapshot(season_id)
    entry_cache: dict = {}
    player_cache: dict = {}
    audit = AuditEventRepository(request.app.state.database)

    def _delisting_view(item):
        view = dataclasses.asdict(item)
        view["team"] = _entry_label(request, item.season_entry_id, entry_cache)
        view["player"] = _player_label(request, item.season_player_id, player_cache)
        return view

    def _trade_view(trade):
        view = dataclasses.asdict(trade)
        view["legs"] = []
        for leg in midseason.trade_legs(trade.trade_id):
            leg_view = dataclasses.asdict(leg)
            leg_view["from_team"] = _entry_label(request, leg.from_season_entry_id, entry_cache)
            leg_view["to_team"] = _entry_label(request, leg.to_season_entry_id, entry_cache)
            leg_view["player"] = _player_label(request, leg.season_player_id, player_cache)
            view["legs"].append(leg_view)
        events = audit.list_events(entity_type="midseason.trade", entity_id=trade.trade_id)

        def _audit_view(event):
            if event is None:
                return None
            actor_name = None
            if event.actor_id:
                coach = request.app.state.identities.get_coach(event.actor_id)
                actor_name = coach.display_name if coach else event.actor_id
            return {
                "actor_name": actor_name or "Shared-token operator",
                "actor_role": event.actor_role,
                "occurred_at": event.occurred_at,
                "reason": event.reason,
            }

        proposal = next((event for event in events if event.action == "midseason.trade.proposed"), None)
        decision = next(
            (event for event in events if event.action in ("midseason.trade.approved", "midseason.trade.rejected")),
            None,
        )
        view["proposal_audit"] = _audit_view(proposal)
        view["decision_audit"] = _audit_view(decision)
        return view

    config = request.app.state.database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
    ).fetchone()

    return {
        "season_id": season_id,
        "draft": dataclasses.asdict(draft),
        "order": [
            {
                "position": position,
                "season_entry_id": entry_id,
                "source": source,
                **_entry_label(request, entry_id, entry_cache),
            }
            for position, entry_id, source in midseason.draft_order(season_id)
        ],
        "ladder_snapshot": (
            {
                "snapshot_id": ladder.snapshot_id,
                "through_round": ladder.through_round,
                "created_at": ladder.created_at,
                "rows": [
                    {**dataclasses.asdict(row), **_entry_label(request, row.season_entry_id, entry_cache)}
                    for row in ladder.rows
                ],
            }
            if ladder
            else None
        ),
        "delistings": [_delisting_view(item) for item in midseason.list_delistings(season_id)],
        "trades": [_trade_view(item) for item in midseason.list_trades(season_id)],
        "trade_pick_rounds": list(range(1, config["squad_limit"] + 1)) if config else [],
        "engine_status": (dataclasses.asdict(midseason.status(season_id)) if midseason.status(season_id) else None),
        "available_player_count": len(midseason.available_player_pool(season_id)),
    }


@router.get("/{season_id}/status")
def status(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    return _status(request, season_id)


@router.post("/{season_id}/trigger-round")
def set_trigger_round(
    season_id: str, payload: SetTriggerRoundRequest, request: Request, principal: Principal = Depends(manage)
):
    """Record the BBBFFL round after which the mid-season draft occurs --
    required before `confirm-ladder` will accept the season. Separate from
    season setup proper so an already-established season (e.g. the 2026
    replay) can still configure it."""
    _authorise(request, principal, season_id)
    request.app.state.seasons.set_midseason_draft_trigger_round(
        season_id, payload.trigger_round, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return _status(request, season_id)


@router.get("/{season_id}/ordinary-competitions")
def ordinary_competitions(season_id: str, request: Request, principal: Principal = Depends(manage)):
    """Issue #226: human-readable competition resolution for the ladder
    preview/confirm step, instead of requiring the operator to already
    know and paste a raw `competition_id`. Every ordinary-stream
    competition belonging to this season -- normally exactly one, but
    never assumed to be, since `app.season.SeasonRepository.
    create_competition` places no uniqueness constraint on `stream_type`
    per season. The setup page auto-resolves when there is exactly one,
    offers a `<select>` of these labels when there is more than one, and
    refuses (fail-closed) to proceed at all when there are none -- see
    `midseason_draft_operations.html`'s `loadOrdinaryCompetitions`. Never
    writes anything, and never a second source of truth: `ladder-preview`/
    `confirm-ladder` still perform the exact same `stream_type='ordinary'`
    validation themselves regardless of what this list returns."""
    _authorise(request, principal, season_id)
    competitions = request.app.state.seasons.list_competitions(season_id)
    return [
        {"competition_id": competition.competition_id, "label": competition.label}
        for competition in competitions
        if competition.stream_type == "ordinary"
    ]


@router.get("/{season_id}/ladder-preview")
def ladder_preview(season_id: str, competition_id: str, request: Request, principal: Principal = Depends(manage)):
    """Issue #181: the calculated ladder through the configured trigger
    round, plus the reverse-ladder draft order it would seed, *before*
    `confirm-ladder` becomes irreversible. Reads the exact same
    `app.ladder.LadderRepository.snapshot` call `confirm_ladder` itself
    takes -- never a second ladder calculation -- and never writes
    anything: the live ladder and any eventual frozen snapshot are both
    completely untouched by this preview."""
    _authorise(request, principal, season_id)
    season = request.app.state.seasons.get_season(season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Unknown season")
    trigger = season.midseason_draft_trigger_round
    if trigger is None:
        raise HTTPException(status_code=400, detail="season has no configured mid-season draft trigger round")
    # Codex review, PR #225 (P2): `competition_id` is caller-controlled and
    # LadderRepository.snapshot derives its own season from it -- without
    # this check a season-scoped Scorer could preview a *different*
    # season's ladder by naming its competition_id. Same validation
    # `MidseasonDraftRepository.confirm_ladder` itself requires.
    competition = request.app.state.database.execute(
        "SELECT season_id, stream_type FROM competition_stream WHERE competition_id=?", (competition_id,)
    ).fetchone()
    if not competition or competition["season_id"] != season_id or competition["stream_type"] != "ordinary":
        raise HTTPException(
            status_code=400, detail="competition_id must name an ordinary competition belonging to this season"
        )
    ladder = request.app.state.ladder.snapshot(competition_id, trigger)
    entry_cache: dict = {}
    reverse_order = sorted(ladder.rows, key=lambda row: (-row.rank, row.season_entry_id))
    return {
        "trigger_round": trigger,
        "rows": [
            {**dataclasses.asdict(row), **_entry_label(request, row.season_entry_id, entry_cache)}
            for row in sorted(ladder.rows, key=lambda row: row.rank)
        ],
        "reverse_order_preview": [
            {"position": position, **_entry_label(request, row.season_entry_id, entry_cache)}
            for position, row in enumerate(reverse_order, 1)
        ],
    }


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


@router.post("/{season_id}/trade/{trade_id}/reverse")
def reverse_trade_approval(
    season_id: str,
    trade_id: str,
    payload: ReverseTradeApprovalRequest,
    request: Request,
    principal: Principal = Depends(manage),
):
    """Exceptional correction: undo an already-*approved* trade while the
    delisting window is still open (e.g. one whose approved pick leg turns
    out to be undeliverable) -- `decide_trade` only accepts a pending
    trade, so an approval has no other way back."""
    _authorise(request, principal, season_id)
    trade = request.app.state.midseason_draft.reverse_trade_approval(
        season_id, trade_id, actor=_actor(principal, payload.scorer_name), reason=payload.reason
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


@router.get("/{season_id}/players")
def player_pool(
    season_id: str,
    request: Request,
    q: str | None = None,
    availability: str | None = None,
    owner_season_entry_id: str | None = None,
    limit: int = 200,
    principal: Principal = Depends(manage),
):
    """The shared player browser (issue #181), annotated with
    current-season-to-date scoring context -- the mid-season counterpart of
    `app.routes.draft.player_pool`, built from the same
    `app.draft_board.player_browse_view` helper."""
    _authorise(request, principal, season_id)
    availability = availability or None
    if availability not in (None, "available", "owned", "unresolved"):
        raise HTTPException(status_code=400, detail="availability must be available, owned, or unresolved")
    return player_browse_view(
        request,
        season_id,
        draft_kind="midseason",
        query=q,
        availability=availability,
        limit=min(limit, 500),
        owner_season_entry_id=owner_season_entry_id,
    )


@router.get("/{season_id}/board")
def board(season_id: str, request: Request, principal: Principal = Depends(manage)):
    """The shared conduct-draft board (issue #181) once the mid-season pick
    table exists -- the same `app.draft_board.build_board` view model
    `app.routes.draft.board` renders for the preseason draft, scoped to
    `draft_kind="midseason"`."""
    _authorise(request, principal, season_id)
    return build_board(request, season_id, draft_kind="midseason")


@router.get("/{season_id}/readiness")
def readiness(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    return build_readiness(request, season_id, draft_kind="midseason")


@router.get("/{season_id}/picks")
def picks(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _authorise(request, principal, season_id)
    return [dataclasses.asdict(item) for item in request.app.state.midseason_draft.picks(season_id)]


@router.post("/{season_id}/pick")
def submit_pick(season_id: str, payload: PickRequest, request: Request, principal: Principal = Depends(manage)):
    """Returns the shared, participate-safe board (issue #181) -- never
    `_status`, which exposes every team's delistings and trade proposals/
    reasons and is deliberately gated behind the full `midseason_draft.
    manage` authority everywhere else (Codex review, PR #225, P1). Matches
    `app.routes.draft.submit_pick`'s own return value for the preseason
    board."""
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.execute_pick(
        season_id,
        payload.season_entry_id,
        payload.season_player_id,
        pick_id=payload.draft_pick_id,
        actor=_actor(principal, payload.scorer_name),
        reason=payload.reason or "mid-season draft selection",
    )
    return build_board(request, season_id, draft_kind="midseason")


@coach_router.get("/{season_id}/players")
def coach_player_pool(
    season_id: str,
    request: Request,
    q: str | None = None,
    availability: str | None = None,
    limit: int = 200,
    principal: Principal = Depends(coach_participant),
):
    _coach_entry_id(request, principal, season_id)
    return player_pool(season_id, request, q, availability, None, limit, principal)


@coach_router.get("/{season_id}/board")
def coach_board(season_id: str, request: Request, principal: Principal = Depends(coach_participant)):
    _coach_entry_id(request, principal, season_id)
    return build_board(request, season_id, draft_kind="midseason")


@coach_router.post("/{season_id}/pick")
def coach_submit_pick(
    season_id: str,
    payload: PickRequest,
    request: Request,
    principal: Principal = Depends(coach_participant),
):
    own_entry_id = _coach_entry_id(request, principal, season_id)
    if payload.season_entry_id != own_entry_id:
        raise HTTPException(status_code=404, detail="Private resource not found")
    require_entry_context(request, principal, payload.season_entry_id)
    request.app.state.midseason_draft.execute_pick(
        season_id,
        own_entry_id,
        payload.season_player_id,
        pick_id=payload.draft_pick_id,
        actor=_actor(principal),
        reason=payload.reason or "mid-season draft selection",
    )
    return build_board(request, season_id, draft_kind="midseason")


@router.post("/{season_id}/reconcile-completion")
def reconcile_completion(season_id: str, request: Request, principal: Principal = Depends(manage)):
    """Retry automatic completion after an interruption between the final
    selection's own commit and finalising/transitioning the draft -- safe
    to call any number of times, including when there is nothing to do."""
    _authorise(request, principal, season_id)
    request.app.state.midseason_draft.reconcile_completion(season_id, actor=_actor(principal))
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


@page_router.get("/admin/midseason-draft/{season_id}", response_class=HTMLResponse)
def midseason_operations_page(season_id: str, request: Request, principal: Principal = Depends(manage)):
    """The dedicated mid-season draft operations view (issue #181):
    Scorer/Admin-only guided lifecycle (ladder confirmation, delisting/
    trade window, lock, generate selections) -- see
    `app.midseason_draft`'s module docstring for the exact state sequence
    this page walks. Conducting the generated draft itself happens on the
    shared board at `/admin/midseason-draft/{season_id}/conduct`."""
    _authorise(request, principal, season_id)
    return templates.TemplateResponse(request, "midseason_draft_operations.html", {"season_id": season_id})


@page_router.get("/admin/midseason-draft/{season_id}/conduct", response_class=HTMLResponse)
def midseason_conduct_page(season_id: str, request: Request, principal: Principal = Depends(manage)):
    """The operator draft-selection board; Coach access uses the separate
    `/account/midseason-draft/{season_id}` surface below."""
    _authorise(request, principal, season_id)
    return templates.TemplateResponse(
        request,
        "draft.html",
        {
            "season_id": season_id,
            "draft_kind": "midseason",
            "api_base": "/api/admin/midseason-draft",
            "my_season_entry_id": resolve_my_entry_id(request, principal, season_id),
            "coach_view": False,
        },
    )


@page_router.get("/account/midseason-draft/{season_id}", response_class=HTMLResponse)
def coach_midseason_draft_page(season_id: str, request: Request, principal: Principal = Depends(coach_participant)):
    entry_id = _coach_entry_id(request, principal, season_id)
    team = request.app.state.identities.get_public_team(entry_id)
    return templates.TemplateResponse(
        request,
        "draft.html",
        {
            "season_id": season_id,
            "draft_kind": "midseason",
            "api_base": "/api/account/midseason-draft",
            "my_season_entry_id": entry_id,
            "coach_view": True,
            "coach_team_name": team.team_name if team else "Coach",
        },
    )
