"""Scorer round-review, sign-off and correction operator surface (roadmap
package 28, issue #58) -- extends the scorer/admin surface the draft and
preseason routers already established (app/routes/draft.py, app/routes/
preseason.py) rather than creating an unrelated third admin interface.

Every rule -- whether a matchup is eligible for sign-off, whether a ruling/
override is legal right now, whether the round can be published or
corrected -- lives in `app.round_review`/`app.competition_lifecycle`, each
call wrapped in its own transaction. Nothing here decides any of that: each
mutating endpoint is a thin translation from an HTTP request to one
application-service call, and every read rebuilds its response from the
database. Like every other route module, this one never imports the season-
model application-service/domain modules directly (see
tests/test_architecture.py) -- it only calls methods on the already-
constructed repositories `app.main`'s lifespan hook attaches to
`request.app.state`, and lets the domain exceptions those calls raise
propagate to the handlers `app.main` registers for them.
"""

import dataclasses
from contextlib import nullcontext

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.audit import ActorContext
from app.round_review import attempt_correction, attempt_signoff, build_round_review
from app.routes.admin import require_admin

router = APIRouter(prefix="/api/admin/round-review")


def _actor(scorer_name: str | None, actor_role: str = "scorer") -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=scorer_name, actor_role=actor_role)


class DnpRulingRequest(BaseModel):
    matchup_id: str
    season_entry_id: str
    slot: str
    dnp: bool
    expected_review_version: int
    reason: str | None = None
    scorer_name: str | None = None


class InterchangeRulingRequest(BaseModel):
    matchup_id: str
    season_entry_id: str
    target_position: str | None = None
    expected_review_version: int
    reason: str | None = None
    scorer_name: str | None = None


class OverrideRequest(BaseModel):
    matchup_id: str
    season_entry_id: str
    position: str
    override_score: float | None = None
    calculated_score: float | None = None
    reason: str | None = None
    expected_review_version: int
    actor_role: str = "scorer"
    scorer_name: str | None = None


class SignoffRequest(BaseModel):
    reason: str | None = None
    scorer_name: str | None = None


class CorrectionRequest(BaseModel):
    reason: str
    scorer_name: str | None = None


def _round_review_view(request: Request, round_id: str, *, evidence_fresh: bool = True) -> dict:
    state = request.app.state
    review = build_round_review(
        state.lifecycle, state.round_review, state.identities, round_id, evidence_fresh=evidence_fresh
    )
    return dataclasses.asdict(review)


@router.get("/{round_id}", dependencies=[Depends(require_admin)])
def get_round_review(round_id: str, request: Request):
    return _round_review_view(request, round_id)


@router.post("/{round_id}/dnp", dependencies=[Depends(require_admin)])
def record_dnp_ruling(round_id: str, payload: DnpRulingRequest, request: Request):
    request.app.state.round_review.record_dnp_ruling(
        payload.matchup_id,
        payload.season_entry_id,
        payload.slot,
        payload.dnp,
        expected_review_version=payload.expected_review_version,
        actor=_actor(payload.scorer_name),
        reason=payload.reason,
    )
    return _round_review_view(request, round_id)


@router.post("/{round_id}/interchange", dependencies=[Depends(require_admin)])
def record_interchange_ruling(round_id: str, payload: InterchangeRulingRequest, request: Request):
    request.app.state.round_review.record_interchange_ruling(
        payload.matchup_id,
        payload.season_entry_id,
        payload.target_position,
        expected_review_version=payload.expected_review_version,
        actor=_actor(payload.scorer_name),
        reason=payload.reason,
    )
    return _round_review_view(request, round_id)


@router.post("/{round_id}/override", dependencies=[Depends(require_admin)])
def record_override(round_id: str, payload: OverrideRequest, request: Request):
    request.app.state.round_review.record_override(
        payload.matchup_id,
        payload.season_entry_id,
        payload.position,
        payload.override_score,
        payload.calculated_score,
        payload.reason,
        expected_review_version=payload.expected_review_version,
        actor=_actor(payload.scorer_name, payload.actor_role),
    )
    return _round_review_view(request, round_id)


@router.post("/{round_id}/signoff", dependencies=[Depends(require_admin)])
def signoff(round_id: str, payload: SignoffRequest, request: Request):
    state = request.app.state
    afl_client = state.afl_client
    # Recompute every matchup's calculated snapshot immediately before
    # validating readiness, under the same fresh-evidence scope
    # routes/admin.py's Grand Final finalize endpoint uses -- so sign-off
    # reflects the current AFL facts, not whatever was last calculated,
    # and fails closed (per app.round_review's evidence_fresh check)
    # rather than freezing a result behind stale/unavailable afl-api data.
    evidence_batch = getattr(afl_client, "evidence_batch", None)
    scope = evidence_batch() if callable(evidence_batch) else nullcontext(afl_client)
    with scope as evidence:
        state.calculations.calculate_round(round_id)
        is_evidence_fresh = getattr(evidence, "is_evidence_fresh", None)
        evidence_fresh = is_evidence_fresh() if callable(is_evidence_fresh) else True
        result = attempt_signoff(
            state.lifecycle,
            state.round_review,
            state.identities,
            round_id,
            actor=_actor(payload.scorer_name),
            reason=payload.reason,
            evidence_fresh=evidence_fresh,
        )
    return dataclasses.asdict(result)


@router.get("/matchup/{matchup_id}/history", dependencies=[Depends(require_admin)])
def matchup_history(matchup_id: str, request: Request):
    history = request.app.state.lifecycle.result_history(matchup_id)
    return [dataclasses.asdict(result) for result in history]


@router.post("/matchup/{matchup_id}/correct", dependencies=[Depends(require_admin)])
def correct_matchup(matchup_id: str, payload: CorrectionRequest, request: Request):
    state = request.app.state
    result = attempt_correction(
        state.lifecycle,
        state.round_review,
        state.identities,
        matchup_id,
        actor=_actor(payload.scorer_name, "admin"),
        reason=payload.reason,
    )
    return dataclasses.asdict(result)
