import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.afl_client import AflApiClient, AflApiError
from app.afl_resilience import ResilientAflClient, RetryPolicy
from app.audit import AuditEventRepository
from app.auth import AuthenticationService, CredentialRepository, SessionRepository
from app.auth_rate_limit import LoginRateLimiter
from app.calculations import MatchupCalculationService
from app.competition_lifecycle import CompetitionLifecycleRepository, StaleRoundVersionError
from app.config import get_settings
from app.db import DecisionsRepository, connect
from app.draft import (
    DraftCorrectionError,
    DraftFinalizedError,
    DraftNotCompleteError,
    DraftOrderError,
    DraftPausedError,
    DraftPickCompletedError,
    DraftRepository,
    DraftStateError,
    DraftTurnError,
)
from app.identity import IdentityRepository
from app.ladder import LadderRepository
from app.lineups import LineupConflictError, WeeklyLineupRepository
from app.migrations import migrate
from app.player_pool import PlayerPoolRepository, PlayerUnavailableError, SquadCapacityError
from app.preseason import (
    PreseasonDraftNotFinalizedError,
    PreseasonRepository,
    PreseasonSnapshotError,
    PreseasonSquadValidationError,
    PreseasonStateError,
    PreseasonTradeValidationError,
    PreseasonWindowClosedError,
    PreseasonWindowExistsError,
)
from app.replay import ReplayAflDataSource
from app.round_review import InvalidOverridePositionError as RoundReviewInvalidPositionError
from app.round_review import InvalidSlotError as RoundReviewInvalidSlotError
from app.round_review import (
    MissingOverrideReasonError,
    RoundReviewRepository,
    SignoffValidationError,
    UnauthorisedActorError,
    UnknownEntryError,
    UnknownMatchupError,
    UnknownRoundError,
)
from app.routes import admin, health, public
from app.routes import auth as auth_routes
from app.routes import coach_lineup as coach_lineup_routes
from app.routes import draft as draft_routes
from app.routes import lineups as lineup_routes
from app.routes import preseason as preseason_routes
from app.routes import round_review as round_review_routes
from app.routes import superscore as superscore_routes
from app.scorer_decisions import (
    CompetitionFinalizedError,
    InvalidPositionError,
    InvalidSlotError,
    ResultNotReadyError,
    StaleAflEvidenceError,
    UnknownTeamError,
)
from app.service import PlayerIdentityCache
from app.superscore import competition_key as superscore_competition_key
from app.superscore import get_superscore_config
from app.teams import TeamConfigError, get_teams

logger = logging.getLogger("bbbffl.startup")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    # Migrations are the sole schema authority. Running them at startup is a
    # deployment convenience and is idempotent; production may run the same
    # command as a separate release step before starting the application.
    migrate(settings.database_url)
    database = connect(settings.database_url)

    afl_transport = (
        ReplayAflDataSource(settings.afl_replay_evidence_path)
        if settings.afl_mode == "replay"
        else AflApiClient(
            base_url=settings.afl_api_base_url,
            api_key=settings.afl_api_key,
            timeout=settings.afl_api_timeout_seconds,
            connect_timeout=settings.afl_api_connect_timeout_seconds,
            read_timeout=settings.afl_api_read_timeout_seconds,
            contract_version=settings.afl_api_contract_version,
        )
    )
    # ResilientAflClient is a drop-in AflDataSource: it adds bounded
    # transient retry/backoff, per-endpoint stale-cache fallback, and
    # diagnostics around afl_transport without changing what any consumer
    # (app/service.py, app/lockouts.py, app/calculations.py) calls or gets
    # back. See app/afl_resilience.py and docs/afl-client-resilience.md.
    afl_client = (
        afl_transport
        if settings.afl_mode == "replay"
        else ResilientAflClient(
            afl_transport,
            retry_policy=RetryPolicy(
                max_attempts=settings.afl_api_retry_max_attempts,
                base_delay_seconds=settings.afl_api_retry_base_delay_seconds,
                max_delay_seconds=settings.afl_api_retry_max_delay_seconds,
            ),
        )
    )
    teams = get_teams(settings.teams_config_path)

    app.state.settings = settings
    app.state.database = database
    app.state.decisions = DecisionsRepository(database)
    app.state.audit_events = AuditEventRepository(database)
    # Roadmap package 14's scorer-operated draft workflow (app/routes/draft.py)
    # is an operator surface over these authoritative repositories -- see
    # docs/draft-ledger.md and docs/scorer-draft-workflow.md.
    app.state.identities = IdentityRepository(database)
    app.state.lineups = WeeklyLineupRepository(database)
    # Roadmap package 19's coach authentication/session boundary (issue
    # #74, app/routes/auth.py) resolves logins directly to `identities`
    # above rather than a second coach/user model -- see app/auth.py's
    # module docstring and docs/coach-authentication.md. The rate limiter
    # is process-local (see app/auth_rate_limit.py) and lives for the
    # process's lifetime on app.state, like every other repository here.
    app.state.credentials = CredentialRepository(database)
    app.state.sessions = SessionRepository(database, session_lifetime_seconds=settings.session_lifetime_seconds)
    app.state.login_rate_limiter = LoginRateLimiter()
    app.state.auth_service = AuthenticationService(
        app.state.identities, app.state.credentials, app.state.sessions, app.state.login_rate_limiter
    )
    app.state.player_pool = PlayerPoolRepository(database)
    app.state.draft = DraftRepository(database)
    # Roadmap package 15's preseason trade/finalisation window (issue #54,
    # app/routes/preseason.py) is an operator surface over this same
    # authoritative repository -- see docs/preseason-trades.md.
    app.state.preseason = PreseasonRepository(database)
    # Roadmap package 28's scorer round-review/sign-off/correction workflow
    # (issue #58, app/routes/round_review.py) sits on top of the persisted
    # ordinary-round lifecycle (#32) and generalised match scoring (#35) --
    # see docs/scorer-round-review.md.
    app.state.lifecycle = CompetitionLifecycleRepository(database)
    app.state.ladder = LadderRepository(database)
    app.state.round_review = RoundReviewRepository(database)
    app.state.calculations = MatchupCalculationService(database, afl_client)
    app.state.afl_client = afl_client
    app.state.identity_cache = PlayerIdentityCache(afl_client)
    app.state.teams = teams

    # SuperScore is entirely opt-in (see config.py). A missing/unset path
    # means disabled, matching current behaviour exactly. A configured but
    # malformed file is logged and disabled rather than crashing startup --
    # a broken SuperScore trial config must never take down the live Grand
    # Final. It gets its own DecisionsRepository, scoped by a
    # season+round-derived competition_key so its DNP/interchange/override/
    # finalisation state can never collide with the Grand Final's (or with
    # another SuperScore round's), even sharing the same database file.
    app.state.superscore_config = None
    app.state.superscore_decisions = None
    if settings.superscore_config_path:
        try:
            superscore_config = get_superscore_config(settings.superscore_config_path)
            app.state.superscore_config = superscore_config
            app.state.superscore_decisions = DecisionsRepository(
                database, superscore_competition_key(superscore_config.season, superscore_config.afl_round)
            )
            logger.info(
                "SuperScore enabled (season=%s, round=%s, entries=%s)",
                superscore_config.season,
                superscore_config.afl_round,
                [e.team_key for e in superscore_config.entries],
            )
        except (TeamConfigError, OSError, ValueError) as exc:
            logger.error(
                "SuperScore config at %s failed to load; SuperScore disabled: %s",
                settings.superscore_config_path,
                exc,
            )

    logger.info(
        "BBBFFL Grand Final prototype starting up (environment=%s, afl_mode=%s, "
        "afl_api=%s, afl_api_contract_version=%s, teams=%s)",
        settings.environment,
        settings.afl_mode,
        settings.afl_api_base_url,
        settings.afl_api_contract_version,
        [t.team_key for t in teams],
    )
    try:
        yield
    finally:
        afl_client.close()
        database.close()


app = FastAPI(title="BBBFFL Grand Final Live Scoring", lifespan=lifespan)

app.include_router(health.router)
app.include_router(public.router)
app.include_router(auth_routes.router)
app.include_router(coach_lineup_routes.router)
app.include_router(lineup_routes.router)
app.include_router(admin.router)
app.include_router(admin.page_router)
app.include_router(superscore_routes.router)
app.include_router(superscore_routes.page_router)
app.include_router(draft_routes.router)
app.include_router(draft_routes.page_router)
app.include_router(preseason_routes.router)
app.include_router(round_review_routes.router)
app.include_router(round_review_routes.page_router)


@app.exception_handler(AflApiError)
async def afl_api_error_handler(request: Request, exc: AflApiError) -> JSONResponse:
    logger.warning("afl-api unavailable: %s", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": "afl-api is currently unavailable. Scores will resume once it recovers."},
    )


@app.exception_handler(LineupConflictError)
async def lineup_conflict_handler(request: Request, exc: LineupConflictError) -> JSONResponse:
    """An expected optimistic-concurrency loss, never an internal error."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# app.scorer_decisions raises plain domain exceptions rather than
# fastapi.HTTPException so it stays usable outside a request context (admin
# scripts, replay, tests). These handlers are the one place that translates
# each one to the HTTP status routes/admin.py and routes/superscore.py
# returned before that orchestration moved out of the route handlers.
@app.exception_handler(UnknownTeamError)
async def unknown_team_handler(request: Request, exc: UnknownTeamError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(InvalidSlotError)
async def invalid_slot_handler(request: Request, exc: InvalidSlotError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(InvalidPositionError)
async def invalid_position_handler(request: Request, exc: InvalidPositionError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(CompetitionFinalizedError)
async def competition_finalized_handler(request: Request, exc: CompetitionFinalizedError) -> JSONResponse:
    return JSONResponse(status_code=423, content={"detail": str(exc)})


@app.exception_handler(ResultNotReadyError)
async def result_not_ready_handler(request: Request, exc: ResultNotReadyError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(StaleAflEvidenceError)
async def stale_afl_evidence_handler(request: Request, exc: StaleAflEvidenceError) -> JSONResponse:
    logger.warning("finalisation refused: %s", exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# app.draft raises plain domain exceptions for the same reason
# app.scorer_decisions does (see the comment above) -- these handlers are
# the sole place that translates each one to an HTTP status for
# app/routes/draft.py. DraftFinalizedError gets 423 (Locked), matching
# CompetitionFinalizedError's convention above; every other draft-specific
# error is a 409 Conflict the operator resolves by refreshing the board
# (a stale turn, a pick completed/paused/corrected concurrently, a
# correction that is no longer the most recent pick) or a 400 for a
# structurally invalid request (a malformed/incomplete draft order).
@app.exception_handler(DraftOrderError)
async def draft_order_error_handler(request: Request, exc: DraftOrderError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(DraftFinalizedError)
async def draft_finalized_error_handler(request: Request, exc: DraftFinalizedError) -> JSONResponse:
    return JSONResponse(status_code=423, content={"detail": str(exc)})


@app.exception_handler(DraftTurnError)
async def draft_turn_error_handler(request: Request, exc: DraftTurnError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(DraftPickCompletedError)
async def draft_pick_completed_error_handler(request: Request, exc: DraftPickCompletedError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(DraftPausedError)
async def draft_paused_error_handler(request: Request, exc: DraftPausedError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(DraftNotCompleteError)
async def draft_not_complete_error_handler(request: Request, exc: DraftNotCompleteError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(DraftCorrectionError)
async def draft_correction_error_handler(request: Request, exc: DraftCorrectionError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(DraftStateError)
async def draft_state_error_handler(request: Request, exc: DraftStateError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(PlayerUnavailableError)
async def player_unavailable_error_handler(request: Request, exc: PlayerUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(SquadCapacityError)
async def squad_capacity_error_handler(request: Request, exc: SquadCapacityError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(KeyError)
async def key_error_handler(request: Request, exc: KeyError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": f"not found: {exc}"})


# A fallback net, not a substitute for a specific handler: FastAPI/Starlette
# dispatch a raised exception to the handler for the most-derived class in
# its MRO that is registered, so every subclass-specific handler above still
# wins over this one. This exists because a domain method's plain
# precondition failure (e.g. `DraftRepository.reopen`'s "reopening a
# finalized draft requires an explicit reason", or
# `PreseasonRepository.correct_opening_snapshot`'s "an opening-squad
# correction requires an explicit reason") is a genuine bad request, not an
# unexpected server error -- it must never surface as an opaque 500.
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# app.preseason raises plain domain exceptions for the same reason app.draft
# does (see the comment above its handlers) -- these are the sole place that
# translates each one to an HTTP status for app/routes/preseason.py.
# PreseasonWindowClosedError gets 423 (Locked), matching
# DraftFinalizedError's convention: the window is a stable, closed fact, not
# a conflict the operator resolves by retrying.
@app.exception_handler(PreseasonWindowClosedError)
async def preseason_window_closed_error_handler(request: Request, exc: PreseasonWindowClosedError) -> JSONResponse:
    return JSONResponse(status_code=423, content={"detail": str(exc)})


@app.exception_handler(PreseasonDraftNotFinalizedError)
async def preseason_draft_not_finalized_error_handler(
    request: Request, exc: PreseasonDraftNotFinalizedError
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(PreseasonWindowExistsError)
async def preseason_window_exists_error_handler(request: Request, exc: PreseasonWindowExistsError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(PreseasonSquadValidationError)
async def preseason_squad_validation_error_handler(
    request: Request, exc: PreseasonSquadValidationError
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "issues": exc.issues})


@app.exception_handler(PreseasonTradeValidationError)
async def preseason_trade_validation_error_handler(
    request: Request, exc: PreseasonTradeValidationError
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "issues": exc.issues})


@app.exception_handler(PreseasonSnapshotError)
async def preseason_snapshot_error_handler(request: Request, exc: PreseasonSnapshotError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(PreseasonStateError)
async def preseason_state_error_handler(request: Request, exc: PreseasonStateError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# app.round_review raises plain domain exceptions for the same reason
# app.scorer_decisions does (see its handlers above) -- these are the sole
# place that translates each one to an HTTP status for
# app/routes/round_review.py (roadmap package 28, issue #58).
@app.exception_handler(UnknownRoundError)
async def unknown_round_error_handler(request: Request, exc: UnknownRoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(UnknownMatchupError)
async def unknown_matchup_error_handler(request: Request, exc: UnknownMatchupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(UnknownEntryError)
async def unknown_entry_error_handler(request: Request, exc: UnknownEntryError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(RoundReviewInvalidSlotError)
async def round_review_invalid_slot_error_handler(request: Request, exc: RoundReviewInvalidSlotError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(RoundReviewInvalidPositionError)
async def round_review_invalid_position_error_handler(
    request: Request, exc: RoundReviewInvalidPositionError
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(MissingOverrideReasonError)
async def missing_override_reason_error_handler(request: Request, exc: MissingOverrideReasonError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(UnauthorisedActorError)
async def unauthorised_actor_error_handler(request: Request, exc: UnauthorisedActorError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(SignoffValidationError)
async def signoff_validation_error_handler(request: Request, exc: SignoffValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "blockers": exc.blockers, "round_blockers": exc.round_blockers},
    )


@app.exception_handler(StaleRoundVersionError)
async def stale_round_version_error_handler(request: Request, exc: StaleRoundVersionError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})
