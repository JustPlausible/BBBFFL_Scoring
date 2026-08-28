import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.afl_client import AflApiClient, AflApiError
from app.afl_resilience import ResilientAflClient, RetryPolicy
from app.audit import AuditEventRepository
from app.config import get_settings
from app.db import DecisionsRepository, connect
from app.migrations import migrate
from app.routes import admin, health, public
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


class ReplayModeNotWiredError(RuntimeError):
    """Raised at startup when `BBBFFL_AFL_MODE=replay` is declared but this
    build has no replay-backed `AflDataSource` to satisfy it yet (roadmap
    package 32 -- see `app/config.py`'s "AFL access mode" docs). Refusing
    to start here, before migrations run or `AflApiClient` is constructed
    below, is what keeps a declared replay/deterministic run from ever
    silently falling back to live afl-api access -- issue #38's explicit
    requirement. Settings validation alone (`get_settings()`) accepts a
    well-formed `replay` declaration; it is this application build, not
    the configuration, that cannot yet fulfill it."""


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    if settings.afl_mode == "replay":
        raise ReplayModeNotWiredError(
            "BBBFFL_AFL_MODE=replay is declared but not yet implemented: this build has no "
            "replay-backed AFL data source (roadmap package 32). Refusing to start rather than "
            "silently falling back to live afl-api access. Set BBBFFL_AFL_MODE=live (or leave "
            "it unset) until the replay harness lands."
        )

    # Migrations are the sole schema authority. Running them at startup is a
    # deployment convenience and is idempotent; production may run the same
    # command as a separate release step before starting the application.
    migrate(settings.database_url)
    database = connect(settings.database_url)

    afl_transport = AflApiClient(
        base_url=settings.afl_api_base_url,
        api_key=settings.afl_api_key,
        timeout=settings.afl_api_timeout_seconds,
        connect_timeout=settings.afl_api_connect_timeout_seconds,
        read_timeout=settings.afl_api_read_timeout_seconds,
        contract_version=settings.afl_api_contract_version,
    )
    # ResilientAflClient is a drop-in AflDataSource: it adds bounded
    # transient retry/backoff, per-endpoint stale-cache fallback, and
    # diagnostics around afl_transport without changing what any consumer
    # (app/service.py, app/lockouts.py, app/calculations.py) calls or gets
    # back. See app/afl_resilience.py and docs/afl-client-resilience.md.
    afl_client = ResilientAflClient(
        afl_transport,
        retry_policy=RetryPolicy(
            max_attempts=settings.afl_api_retry_max_attempts,
            base_delay_seconds=settings.afl_api_retry_base_delay_seconds,
            max_delay_seconds=settings.afl_api_retry_max_delay_seconds,
        ),
    )
    teams = get_teams(settings.teams_config_path)

    app.state.settings = settings
    app.state.database = database
    app.state.decisions = DecisionsRepository(database)
    app.state.audit_events = AuditEventRepository(database)
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
app.include_router(admin.router)
app.include_router(admin.page_router)
app.include_router(superscore_routes.router)
app.include_router(superscore_routes.page_router)


@app.exception_handler(AflApiError)
async def afl_api_error_handler(request: Request, exc: AflApiError) -> JSONResponse:
    logger.warning("afl-api unavailable: %s", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": "afl-api is currently unavailable. Scores will resume once it recovers."},
    )


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
