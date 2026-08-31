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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_role_covers_season
from app.config import BASE_DIR
from app.public_rounds import authoritative_player_names, authoritative_submissions
from app.round_review import attempt_correction, attempt_signoff, build_round_review
from app.routes.admin import require_admin, require_scorer

router = APIRouter(prefix="/api/admin/round-review")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _actor(principal: Principal, operator_name: str | None) -> ActorContext:
    """Provenance authority comes only from the resolved credential.

    ``operator_name`` remains the existing human-readable label because the
    prototype token is shared and has no persistent operator identity.  It can
    never elevate or otherwise select the audited role.
    """
    actor_id = principal.coach_id if principal.coach_id is not None else operator_name
    return ActorContext(actor_type="anonymous_operator", actor_id=actor_id, actor_role=principal.role.value)


def _authorise_round(request: Request, principal: Principal, round_id: str):
    """Resolve the target round server-side before applying season scope."""
    try:
        round_ = request.app.state.lifecycle.get_round(round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown BBBFFL round") from exc
    require_role_covers_season(request, principal, round_.season_id)
    return round_


def _authorise_matchup(request: Request, principal: Principal, matchup_id: str) -> None:
    row = request.app.state.database.execute(
        "SELECT bbbffl_round_id FROM bbbffl_matchup WHERE matchup_id=?", (matchup_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown matchup")
    _authorise_round(request, principal, row["bbbffl_round_id"])


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
    actor_role: str | None = None  # legacy input accepted but deliberately ignored
    scorer_name: str | None = None


class SignoffRequest(BaseModel):
    reason: str | None = None
    scorer_name: str | None = None


class TransitionRequest(BaseModel):
    target: str
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
    result = dataclasses.asdict(review)
    round_ = state.lifecycle.get_round(round_id)
    result["identity"] = dataclasses.asdict(round_)
    result["replay"] = {
        "enabled": state.settings.afl_mode == "replay",
        "classification": "replay evidence" if state.settings.afl_mode == "replay" else "live evidence",
    }
    # A submitted team exists independently of its calculated snapshot.  Use
    # the exact effective immutable submission reader used by the public Round
    # Centre so the scorer never has to infer a lineup from calculation data.
    submissions = authoritative_submissions(state.database, round_)
    player_ids = [slot["season_player_id"] for slots in submissions.values() for slot in slots]
    player_names = authoritative_player_names(state.database, player_ids)
    for matchup in result["matchups"]:
        for side_name in ("home", "away"):
            side = matchup[side_name]
            submitted = submissions.get(side["season_entry_id"])
            side["submitted_lineup"] = (
                {
                    "submission_version": submitted[0]["version"],
                    "players": [
                        {
                            "position": slot["position"],
                            "season_player_id": slot["season_player_id"],
                            "player_name": player_names.get(slot["season_player_id"]),
                        }
                        for slot in submitted
                    ],
                }
                if submitted
                else None
            )
        history = state.lifecycle.result_history(matchup["matchup_id"])
        matchup["official_history"] = [dataclasses.asdict(item) for item in history]
        official = state.lifecycle.effective_result(matchup["matchup_id"])
        matchup["official_result"] = dataclasses.asdict(official) if official else None
    result["ladder"] = None
    if round_.state == "final":
        result["ladder"] = dataclasses.asdict(state.ladder.snapshot(round_.competition_id, round_.fixture_round_number))
    return result


@router.get("")
def list_round_reviews(request: Request, principal: Principal = Depends(require_scorer)):
    result = []
    for item in request.app.state.lifecycle.list_ordinary_rounds():
        try:
            require_role_covers_season(request, principal, item.season_id)
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
        result.append(dataclasses.asdict(item))
    return result


@router.get("/{round_id}")
def get_round_review(round_id: str, request: Request, principal: Principal = Depends(require_scorer)):
    _authorise_round(request, principal, round_id)
    return _round_review_view(request, round_id)


@router.post("/{round_id}/calculate")
def calculate_round_review(round_id: str, request: Request, principal: Principal = Depends(require_scorer)):
    """Run `MatchupCalculationService.calculate_round` on demand and return
    the refreshed review -- the browser Round Centre's only way to see
    calculated scores/DNP evidence before deciding whether to sign off.
    `/signoff` also recalculates immediately before publishing (see below),
    but until this existed there was no way to trigger a first calculation
    at all through the API/UI, so a scorer could never review a round's
    scores ahead of that one, all-or-nothing sign-off attempt."""
    _authorise_round(request, principal, round_id)
    request.app.state.calculations.calculate_round(round_id)
    return _round_review_view(request, round_id)


@router.post("/{round_id}/transition")
def transition_round_review(
    round_id: str, payload: TransitionRequest, request: Request, principal: Principal = Depends(require_scorer)
):
    """Advance the round's ordinary lifecycle state (`app.competition_
    lifecycle.LEGAL_TRANSITIONS`: upcoming -> open -> live -> review).
    `attempt_signoff` already requires `state == "review"`, but nothing
    else exposed a way to reach it -- this is the scorer-facing wiring for
    that existing, otherwise-unreachable transition."""
    _authorise_round(request, principal, round_id)
    request.app.state.lifecycle.transition(
        round_id, payload.target, actor=_actor(principal, payload.scorer_name), reason=payload.reason
    )
    return _round_review_view(request, round_id)


@router.post("/{round_id}/dnp")
def record_dnp_ruling(
    round_id: str, payload: DnpRulingRequest, request: Request, principal: Principal = Depends(require_scorer)
):
    _authorise_round(request, principal, round_id)
    request.app.state.round_review.record_dnp_ruling(
        payload.matchup_id,
        payload.season_entry_id,
        payload.slot,
        payload.dnp,
        expected_review_version=payload.expected_review_version,
        actor=_actor(principal, payload.scorer_name),
        reason=payload.reason,
        round_id=round_id,
    )
    return _round_review_view(request, round_id)


@router.post("/{round_id}/interchange")
def record_interchange_ruling(
    round_id: str, payload: InterchangeRulingRequest, request: Request, principal: Principal = Depends(require_scorer)
):
    _authorise_round(request, principal, round_id)
    request.app.state.round_review.record_interchange_ruling(
        payload.matchup_id,
        payload.season_entry_id,
        payload.target_position,
        expected_review_version=payload.expected_review_version,
        actor=_actor(principal, payload.scorer_name),
        reason=payload.reason,
        round_id=round_id,
    )
    return _round_review_view(request, round_id)


@router.post("/{round_id}/override")
def record_override(
    round_id: str, payload: OverrideRequest, request: Request, principal: Principal = Depends(require_scorer)
):
    _authorise_round(request, principal, round_id)
    if payload.actor_role not in (None, "scorer", "replay_operator", "admin"):
        raise HTTPException(status_code=403, detail="actor_role cannot grant authority")
    request.app.state.round_review.record_override(
        payload.matchup_id,
        payload.season_entry_id,
        payload.position,
        payload.override_score,
        payload.calculated_score,
        payload.reason,
        expected_review_version=payload.expected_review_version,
        actor=_actor(principal, payload.scorer_name),
        round_id=round_id,
    )
    return _round_review_view(request, round_id)


@router.post("/{round_id}/signoff")
def signoff(round_id: str, payload: SignoffRequest, request: Request, principal: Principal = Depends(require_scorer)):
    _authorise_round(request, principal, round_id)
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
            actor=_actor(principal, payload.scorer_name),
            reason=payload.reason,
            evidence_fresh=evidence_fresh,
        )
    return dataclasses.asdict(result)


@router.get("/matchup/{matchup_id}/history")
def matchup_history(matchup_id: str, request: Request, principal: Principal = Depends(require_admin)):
    _authorise_matchup(request, principal, matchup_id)
    history = request.app.state.lifecycle.result_history(matchup_id)
    return [dataclasses.asdict(result) for result in history]


@router.post("/matchup/{matchup_id}/correct")
def correct_matchup(
    matchup_id: str, payload: CorrectionRequest, request: Request, principal: Principal = Depends(require_admin)
):
    _authorise_matchup(request, principal, matchup_id)
    state = request.app.state
    afl_client = state.afl_client
    # Same fresh-evidence discipline as /signoff above: a correction must
    # not freeze a new official version from a calculation that predates
    # AFL facts which have since changed. Recomputing just this matchup
    # (not the whole round) keeps a single-matchup correction cheap.
    evidence_batch = getattr(afl_client, "evidence_batch", None)
    scope = evidence_batch() if callable(evidence_batch) else nullcontext(afl_client)
    with scope as evidence:
        state.calculations.calculate_matchup(matchup_id)
        is_evidence_fresh = getattr(evidence, "is_evidence_fresh", None)
        evidence_fresh = is_evidence_fresh() if callable(is_evidence_fresh) else True
        result = attempt_correction(
            state.lifecycle,
            state.round_review,
            state.identities,
            matchup_id,
            actor=_actor(principal, payload.scorer_name),
            reason=payload.reason,
            evidence_fresh=evidence_fresh,
        )
    return dataclasses.asdict(result)


@page_router.get("/scorer/round-centre", response_class=HTMLResponse)
@page_router.get("/scorer/round-centre/{round_id}", response_class=HTMLResponse)
def round_centre_page(request: Request, round_id: str | None = None):
    """Browser shell; private reads and all mutations remain API-authorised."""
    return templates.TemplateResponse(request, "round_centre.html", {"round_id": round_id})
