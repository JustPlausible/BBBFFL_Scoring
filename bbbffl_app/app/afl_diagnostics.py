"""Structured, secret-safe diagnostic state for the `afl-api` dependency
(roadmap package 05, issue #37).

Deliberately a plain in-memory registry, not a persisted table: BBBFFL runs
as one process (see `app.afl_client.AflApiClient`'s own docstring), and this
state exists to answer "what is the current health of the afl-api
dependency" for the lifetime of that process -- it is not season/audit
history and does not need to survive a restart. `app.afl_resilience.
ResilientAflClient` is the only writer; routes/scripts/tests only ever read
it back through `as_dict()`/`snapshot()`.

Every field recorded here is one of: an endpoint name (a fixed internal
label, e.g. "matches"), a status enum, a timestamp, a failure-class enum, a
correlation id (a UUID4 this process generated itself), or a short
exception type+message built only from a request path/status code (see
`app.afl_resilience._safe_detail`). None of those can ever contain a
credential, so `as_dict()` is safe to expose on an operator-facing endpoint
without redaction logic of its own -- there is simply nothing secret to
redact. `tests/test_afl_diagnostics.py` and
`tests/test_afl_resilience.py::test_diagnostics_never_expose_the_api_key`
pin this by construction.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Deliberately not imported: `app.afl_resilience.FailureClass` is the actual
# runtime type of `last_failure_class` below, but importing it here would
# create app.afl_diagnostics <-> app.afl_resilience import cycle (this
# module is the lower-level one -- ResilientAflClient depends on the
# registry, not the other way around). Annotated as `Any`; `.value` is
# still accessed safely in `as_dict()` since every value actually stored is
# an Enum member.


class EvidenceStatus(str, Enum):
    """How a caller should regard one piece of AFL evidence just returned."""

    # A live afl-api response was just parsed successfully.
    FRESH = "fresh"
    # The live read failed transiently; a cached value within this
    # endpoint's stale-tolerance window was returned instead.
    STALE = "stale"
    # No live read succeeded and no usable cached value exists (a cold-cache
    # outage), or the cached value is older than policy allows.
    UNAVAILABLE = "unavailable"
    # The response was structurally/semantically incompatible with the
    # pinned afl-api v1 contract (see docs/afl-api-v1-contract.md) -- never
    # served from cache, always visible.
    INVALID = "invalid"


@dataclass(frozen=True)
class EndpointDiagnostics:
    endpoint: str
    status: EvidenceStatus
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_failure_class: Any = None
    last_correlation_id: str | None = None
    last_detail: str | None = None
    cache_age_seconds: float | None = None

    def as_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "status": self.status.value,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_failure_class": getattr(self.last_failure_class, "value", self.last_failure_class),
            "last_correlation_id": self.last_correlation_id,
            "last_detail": self.last_detail,
            "cache_age_seconds": self.cache_age_seconds,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AflDiagnosticsRegistry:
    """Thread-safe (a single lock; call volume is low) per-endpoint
    diagnostic state for one AFL client instance."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, EndpointDiagnostics] = {}

    def record(
        self,
        endpoint: str,
        status: EvidenceStatus,
        *,
        failure_class: Any = None,
        correlation_id: str | None = None,
        detail: str | None = None,
        cache_age_seconds: float | None = None,
    ) -> None:
        now = _now_iso()
        with self._lock:
            previous = self._entries.get(endpoint)
            if status == EvidenceStatus.FRESH:
                updated = EndpointDiagnostics(
                    endpoint=endpoint,
                    status=status,
                    last_success_at=now,
                    last_failure_at=previous.last_failure_at if previous else None,
                    last_failure_class=previous.last_failure_class if previous else None,
                    last_correlation_id=correlation_id,
                    last_detail=None,
                    cache_age_seconds=None,
                )
            else:
                base = previous or EndpointDiagnostics(endpoint=endpoint, status=status)
                updated = replace(
                    base,
                    status=status,
                    last_failure_at=now,
                    last_failure_class=failure_class,
                    last_correlation_id=correlation_id,
                    last_detail=detail,
                    cache_age_seconds=cache_age_seconds,
                )
            self._entries[endpoint] = updated

    def snapshot(self) -> dict[str, EndpointDiagnostics]:
        with self._lock:
            return dict(self._entries)

    def all_fresh(self) -> bool:
        """True iff every endpoint ever recorded is currently FRESH, and at
        least one endpoint has been recorded at all -- an AFL client that has
        never been asked for anything is not evidence of freshness."""
        entries = self.snapshot()
        if not entries:
            return False
        return all(entry.status == EvidenceStatus.FRESH for entry in entries.values())

    def as_dict(self) -> dict:
        """The full, secret-safe diagnostic report -- see module docstring
        for why no redaction step is needed."""
        return {
            "dependency": "afl-api",
            "endpoints": {name: entry.as_dict() for name, entry in self.snapshot().items()},
        }
