import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.afl_client import AflApiClient, AflApiError
from app.config import get_settings
from app.db import DecisionsRepository, connect, init_db
from app.routes import admin, health, public, superscore as superscore_routes
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

    conn = connect(settings.database_path)
    init_db(conn)

    afl_client = AflApiClient(
        base_url=settings.afl_api_base_url,
        api_key=settings.afl_api_key,
        timeout=settings.afl_api_timeout_seconds,
    )
    teams = get_teams(settings.teams_config_path)

    app.state.settings = settings
    app.state.db_conn = conn
    app.state.decisions = DecisionsRepository(conn)
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
                conn, superscore_competition_key(superscore_config.season, superscore_config.afl_round)
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
        "BBBFFL Grand Final prototype starting up (teams=%s, afl_api=%s)",
        [t.team_key for t in teams],
        settings.afl_api_base_url,
    )
    try:
        yield
    finally:
        afl_client.close()
        conn.close()


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
