"""Process liveness (`/health`) and dependency readiness (`/health/ready`).

Issue #243 (production deployment/readiness baseline) splits these
deliberately:

- `GET /health` stays exactly what it always was -- "the process is up and
  serving requests" -- with no database or afl-api dependency. A container
  orchestrator (the Dockerfile's own `HEALTHCHECK`, or a Compose
  `depends_on: condition: service_healthy`) should keep using this for
  restart-loop/liveness decisions: a transient afl-api or database outage
  must never look like a crashed process and trigger a container restart,
  which would not fix the outage and would only add churn.
- `GET /health/ready` answers a different question -- "are the dependencies
  a live season actually needs right now available" -- and is meant for an
  external monitor/alerting path (see `docs/production-operations.md`) or a
  reverse-proxy upstream check, not for the container orchestrator's own
  restart decision.

Both database connectivity and, only when `settings.afl_mode == "live"`,
afl-api connectivity are checked. Replay/test runs (`afl_mode == "replay"`)
never make a live network call here -- this keeps the readiness check
itself deterministic and network-free in the hermetic test suite, matching
`app.replay`'s existing rule that replay never falls back to live afl-api
access. Each check is bounded by `settings.readiness_timeout_seconds`
(`BBBFFL_READINESS_TIMEOUT_SECONDS`) so a stuck dependency can never hang
the response indefinitely. Neither check mutates any state: the database
probe is a bare `SELECT 1` and the afl-api probe is
`AflApiClient.check_connectivity`'s unauthenticated `GET /api/{version}`
discovery call (see its own docstring) -- never a write, and never an
endpoint that pulls a real season/round/player dataset.

Failure detail is deliberately asymmetric: afl-api errors are already
secret-safe by construction (`app/afl_client.py`'s error classes never
include request headers or the API key -- see their docstrings and
`tests/test_afl_resilience.py::test_diagnostics_never_expose_the_api_key`),
so this reports their message verbatim. A database error's message is not
guaranteed secret-safe (a driver could embed connection details), so only
its exception type name is reported. Neither ever mutates or exposes a
setting's value.
"""

import asyncio
import logging
from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger("bbbffl.readiness")


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


async def _bounded_check(fn: Callable[[], None], timeout_seconds: float) -> Exception | None:
    """Runs `fn` (a blocking call) off the event loop, bounded by
    `timeout_seconds`. Returns `None` on success, else the exception (a
    timeout surfaces as `TimeoutError`) -- never raises."""
    try:
        await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout_seconds)
        return None
    except Exception as exc:  # noqa: BLE001 -- reported structurally below, never re-raised
        return exc


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    database = request.app.state.database
    timeout_seconds = settings.readiness_timeout_seconds

    checks: dict[str, dict[str, str]] = {}

    db_error = await _bounded_check(lambda: database.execute("SELECT 1"), timeout_seconds)
    checks["database"] = (
        {"status": "ok"} if db_error is None else {"status": "error", "detail": type(db_error).__name__}
    )

    if settings.afl_mode == "live":
        afl_client = request.app.state.afl_client
        afl_error = await _bounded_check(afl_client.check_connectivity, timeout_seconds)
        # afl_client.check_connectivity()'s errors are secret-safe by
        # construction (see this module's docstring) -- safe to report
        # verbatim, unlike the database check above.
        if afl_error is None:
            checks["afl_api"] = {"status": "ok"}
        else:
            checks["afl_api"] = {"status": "error", "detail": str(afl_error) or type(afl_error).__name__}
    else:
        checks["afl_api"] = {"status": "skipped", "detail": f"afl_mode={settings.afl_mode}"}

    failing = [name for name, check in checks.items() if check["status"] == "error"]
    if failing:
        logger.warning("readiness check failed: %s", ", ".join(failing))

    ready = not failing
    return JSONResponse(
        status_code=200 if ready else 503, content={"status": "ok" if ready else "error", "checks": checks}
    )
