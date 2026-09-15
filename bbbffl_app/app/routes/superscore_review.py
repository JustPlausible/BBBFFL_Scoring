"""HTTP surface for SuperScore's entry-scoped DNP/Interchange/override
review boundary (`app.superscore_review.SuperScoreReviewRepository`, issue
#192 gap #2) -- before issue #208 this had no route at all, so a Scorer
could never actually record a SuperScore ruling through the browser.
Mirrors `app/routes/round_review.py`'s matchup-keyed DNP/Interchange/
override endpoints, adapted to SuperScore's `(bbbffl_round_id,
season_entry_id, slot)` key shape: SuperScore has no `bbbffl_matchup` row
to key a ruling by at all (see `app.superscore_review`'s module docstring).
Every write below is the existing repository's own write -- this route
never re-derives a ruling/eligibility rule of its own."""

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_role_covers_season, require_round_reviewer
from app.csrf import verify_token
from app.superscore_review import (
    InvalidOverridePositionError,
    InvalidSlotError,
    MissingOverrideReasonError,
    StaleReviewVersionError,
    SuperScoreReviewRepository,
    UnauthorisedActorError,
    UnknownReviewStateError,
)

router = APIRouter(prefix="/api/scorer/superscore")


class DnpRulingRequest(BaseModel):
    slot: str
    dnp: bool
    expected_review_version: int
    reason: str | None = None


class InterchangeRulingRequest(BaseModel):
    target_position: str | None = None
    expected_review_version: int
    reason: str | None = None


class OverrideRequest(BaseModel):
    position: str
    override_score: float | None = None
    calculated_score: float | None = None
    reason: str | None = None
    expected_review_version: int


def _actor(principal: Principal) -> ActorContext:
    return ActorContext("anonymous_operator", principal.coach_id, principal.role.value)


def _csrf(request: Request, principal: Principal) -> None:
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(403, "Invalid CSRF token")


def _authorise_round(request: Request, principal: Principal, round_id: str) -> str:
    """Resolve and season-scope a SuperScore round; returns its
    `season_id`. Refuses (404) a round that does not belong to a
    `superscore`-typed `competition_stream` -- the same fence
    `app/routes/superscore_results.py`'s `_round` helper applies."""
    row = request.app.state.database.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=? AND c.stream_type='superscore'",
        (round_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown SuperScore round")
    require_role_covers_season(request, principal, row["season_id"])
    return row["season_id"]


def _view(request: Request, round_id: str, season_entry_id: str) -> dict:
    repo = SuperScoreReviewRepository(request.app.state.database)
    interchange = repo.get_interchange_ruling(round_id, season_entry_id)
    return {
        "bbbffl_round_id": round_id,
        "season_entry_id": season_entry_id,
        "review_version": repo.get_review_version(round_id, season_entry_id),
        "slot_rulings": {
            slot: dataclasses.asdict(ruling)
            for slot, ruling in repo.get_slot_rulings(round_id, season_entry_id).items()
        },
        "interchange_ruling": dataclasses.asdict(interchange) if interchange is not None else None,
        "overrides": {
            position: dataclasses.asdict(override)
            for position, override in repo.get_overrides(round_id, season_entry_id).items()
        },
    }


@router.get("/rounds/{round_id}/entries/{season_entry_id}")
def view_entry_review(
    round_id: str, season_entry_id: str, request: Request, principal: Principal = Depends(require_round_reviewer)
):
    _authorise_round(request, principal, round_id)
    return _view(request, round_id, season_entry_id)


@router.post("/rounds/{round_id}/entries/{season_entry_id}/dnp")
def record_dnp(
    round_id: str,
    season_entry_id: str,
    payload: DnpRulingRequest,
    request: Request,
    principal: Principal = Depends(require_round_reviewer),
):
    _authorise_round(request, principal, round_id)
    _csrf(request, principal)
    repo = SuperScoreReviewRepository(request.app.state.database)
    try:
        repo.record_dnp_ruling(
            round_id,
            season_entry_id,
            payload.slot,
            payload.dnp,
            expected_review_version=payload.expected_review_version,
            actor=_actor(principal),
            reason=payload.reason,
        )
    except InvalidSlotError as exc:
        raise HTTPException(422, str(exc)) from exc
    except UnknownReviewStateError as exc:
        raise HTTPException(404, str(exc)) from exc
    except StaleReviewVersionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(request, round_id, season_entry_id)


@router.post("/rounds/{round_id}/entries/{season_entry_id}/interchange")
def record_interchange(
    round_id: str,
    season_entry_id: str,
    payload: InterchangeRulingRequest,
    request: Request,
    principal: Principal = Depends(require_round_reviewer),
):
    _authorise_round(request, principal, round_id)
    _csrf(request, principal)
    repo = SuperScoreReviewRepository(request.app.state.database)
    try:
        repo.record_interchange_ruling(
            round_id,
            season_entry_id,
            payload.target_position,
            expected_review_version=payload.expected_review_version,
            actor=_actor(principal),
            reason=payload.reason,
        )
    except InvalidSlotError as exc:
        raise HTTPException(422, str(exc)) from exc
    except UnknownReviewStateError as exc:
        raise HTTPException(404, str(exc)) from exc
    except StaleReviewVersionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(request, round_id, season_entry_id)


@router.post("/rounds/{round_id}/entries/{season_entry_id}/override")
def record_override(
    round_id: str,
    season_entry_id: str,
    payload: OverrideRequest,
    request: Request,
    principal: Principal = Depends(require_round_reviewer),
):
    _authorise_round(request, principal, round_id)
    _csrf(request, principal)
    repo = SuperScoreReviewRepository(request.app.state.database)
    try:
        repo.record_override(
            round_id,
            season_entry_id,
            payload.position,
            payload.override_score,
            payload.calculated_score,
            payload.reason,
            expected_review_version=payload.expected_review_version,
            actor=_actor(principal),
        )
    except InvalidOverridePositionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except MissingOverrideReasonError as exc:
        raise HTTPException(422, str(exc)) from exc
    except UnauthorisedActorError as exc:
        raise HTTPException(403, str(exc)) from exc
    except UnknownReviewStateError as exc:
        raise HTTPException(404, str(exc)) from exc
    except StaleReviewVersionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(request, round_id, season_entry_id)
