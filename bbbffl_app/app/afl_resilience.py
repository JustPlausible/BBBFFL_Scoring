"""Resilient boundary around `app.afl_client.AflApiClient` (roadmap package
05, issue #37): bounded transient retry/backoff, per-endpoint caching with
explicit freshness/provenance metadata, and an explicit evidence-state model.

`afl-api` remains the sole authority for AFL facts -- nothing here invents a
second copy of AFL truth. What this module adds is resilience *around* that
one authority: a request that would otherwise hang or fail outright on a
transient blip gets a bounded number of retries; a request that still fails
can fall back to a recent cached value *only when the failure is genuinely
transient and the cache is not too old for that kind of fact*; and every
result -- live or cached -- carries enough metadata (`EvidenceStatus`,
fetched-at, failure class) that a caller can tell a fresh read from a stale
one, and a stale one from a cold-cache outage. A contract/schema
incompatibility (a required field missing, a wrapper key renamed) is never
retried and never masked by a stale cache -- it always propagates as a live
error, exactly as `AflApiClient` already guarantees on its own (see its
module docstring and `docs/afl-api-v1-contract.md`).

Composable pieces, each independently testable without real time or a real
network:

  - `Clock` / `Sleeper` -- injectable time sources. Production uses
    `SystemClock`/`RealSleeper`; tests use fakes so retry/backoff/staleness
    tests never sleep in real time.
  - `RetryPolicy` + `classify_failure` -- bounded attempts, bounded delay,
    and the retryable/non-retryable failure-class split documented below.
  - `EvidenceStatus` + `CacheEntry` -- the cache is explicitly not an
    authority: every cached value is tagged with when it was fetched, and
    every read is tagged fresh/stale/unavailable/invalid.
  - `AflDiagnosticsRegistry` (see `app/afl_diagnostics.py`) -- structured,
    secret-free diagnostic state per endpoint.
  - `ResilientAflClient` -- the composition of the above around a transport
    object shaped like `AflApiClient` (get_current_season/get_round/
    get_rounds/get_matches/get_player/get_match_player_stats). It implements
    the same duck-typed surface `app.service.AflDataSource` already expects,
    so it is a drop-in replacement for `AflApiClient` everywhere the app
    passes one around -- no consuming service changes shape.

## Retryable vs non-retryable failure classes

Retryable (bounded transient retry applies):

  - `CONNECT_TIMEOUT` -- afl-api never accepted the connection in time.
  - `READ_TIMEOUT` -- connected, but the response did not arrive in time.
  - `CONNECTION_ERROR` -- DNS/refused/reset/TLS failure reaching afl-api.
  - `TRANSIENT_HTTP` -- afl-api responded 408/429/500/502/503/504.

Never retried (fails immediately, and never served from stale cache):

  - `CLIENT_HTTP` -- any other non-2xx status (e.g. 404/401/403): a request
    problem, not a transient upstream blip.
  - `CONTRACT_ERROR` -- a required field/wrapper is missing or the wrong
    shape (surfaces as `KeyError`/`TypeError`/`ValueError` from
    `AflApiClient`'s parsing, or a generic `AflApiError` raised by it for a
    malformed payload). This is exactly the "afl-api's v1 contract changed
    incompatibly" case `docs/afl-api-v1-contract.md` requires to stay
    visible -- retrying it would not help, and masking it behind a stale
    cache would hide a real incompatibility indefinitely.
  - `UNKNOWN` -- anything else. Treated conservatively as non-retryable.

## Cache/staleness policy

Every endpoint category gets its own `stale_ttl_seconds` (see
`DEFAULT_CACHE_POLICIES`): a cached fact is only offered as a stale fallback
while it is younger than that window, appropriate to how quickly that kind
of AFL fact actually changes (season/round/player identity move slowly;
live match/player-stats data does not). The client always attempts a live
request first -- the cache is never consulted to *skip* a live read, only to
survive a failed one -- so "resilience" never comes at the cost of serving
avoidably-stale data when afl-api is healthy.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterator, Protocol, TypeVar

from app.afl_client import (
    AflApiConnectionError,
    AflApiError,
    AflApiHttpStatusError,
    AflApiTimeoutError,
)
from app.afl_diagnostics import AflDiagnosticsRegistry, EvidenceStatus
from app.audit import new_correlation_id

T = TypeVar("T")


class AflEvidenceUnavailableError(AflApiError):
    """Every retry attempt for `endpoint` failed and no usable (fresh-enough)
    cached value exists -- a cold-cache outage. Distinct from a single
    `AflApiTimeoutError`/`AflApiConnectionError`: this is what
    `ResilientAflClient` raises once its retry/cache policy has been
    exhausted, not the first raw transport failure."""

    def __init__(self, endpoint: str, *, cause: Exception | None = None) -> None:
        super().__init__(
            f"afl-api evidence for {endpoint!r} is unavailable: no live response and no "
            "usable cached value within policy"
        )
        self.endpoint = endpoint
        self.__cause__ = cause


class FailureClass(str, Enum):
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    CONNECTION_ERROR = "connection_error"
    TRANSIENT_HTTP = "transient_http"
    CLIENT_HTTP = "client_http"
    CONTRACT_ERROR = "contract_error"
    UNKNOWN = "unknown"


# 408 (request timeout) and 429 (rate limited) are upstream-side signals that
# a retry may succeed, exactly like a 5xx -- none of them indicate the
# request itself was wrong.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

RETRYABLE_FAILURE_CLASSES = frozenset(
    {
        FailureClass.CONNECT_TIMEOUT,
        FailureClass.READ_TIMEOUT,
        FailureClass.CONNECTION_ERROR,
        FailureClass.TRANSIENT_HTTP,
    }
)


def classify_failure(exc: BaseException) -> FailureClass:
    """Maps a raised exception to a `FailureClass`. Only ever inspects the
    exception's type/status -- never its request/headers -- so this can
    never leak a credential even if a caller logged its result."""
    if isinstance(exc, AflApiTimeoutError):
        return FailureClass.CONNECT_TIMEOUT if exc.phase == "connect" else FailureClass.READ_TIMEOUT
    if isinstance(exc, AflApiConnectionError):
        return FailureClass.CONNECTION_ERROR
    if isinstance(exc, AflApiHttpStatusError):
        return (
            FailureClass.TRANSIENT_HTTP
            if exc.status_code in _TRANSIENT_HTTP_STATUSES
            else FailureClass.CLIENT_HTTP
        )
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return FailureClass.CONTRACT_ERROR
    if isinstance(exc, AflApiError):
        # Any other AflApiError subclass not covered above (e.g. the plain
        # AflApiError get_current_season raises for "no is_current season")
        # is a data problem, not a network one -- treat it like a contract
        # incompatibility: visible immediately, never retried or masked.
        return FailureClass.CONTRACT_ERROR
    return FailureClass.UNKNOWN


class Clock(Protocol):
    def now(self) -> float: ...


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()


class RealSleeper:
    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded attempts with bounded exponential backoff. `max_attempts`
    counts the *first* attempt, so `max_attempts=3` means at most two
    retries. Deterministic (no jitter) -- bounding via `max_attempts` and
    `max_delay_seconds` is what prevents a retry storm, not randomisation."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.2
    max_delay_seconds: float = 2.0
    multiplier: float = 2.0

    def delay_for(self, attempt: int) -> float:
        """Delay to wait *after* the given 1-indexed attempt fails."""
        raw = self.base_delay_seconds * (self.multiplier ** (attempt - 1))
        return min(raw, self.max_delay_seconds)


def call_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    sleeper: Sleeper,
    classify: Callable[[BaseException], FailureClass] = classify_failure,
    on_attempt: Callable[[int, FailureClass, BaseException], None] | None = None,
) -> T:
    """Runs `fn`, retrying up to `policy.max_attempts` total attempts for a
    retryable failure class, sleeping `policy.delay_for(attempt)` between
    attempts via the injected `sleeper` (never a real `time.sleep` in
    tests). A non-retryable failure (contract/schema incompatibility, a
    non-transient HTTP status, or anything unrecognised) propagates
    immediately on its first occurrence."""
    attempt = 1
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- classified below, not swallowed
            failure_class = classify(exc)
            if on_attempt is not None:
                on_attempt(attempt, failure_class, exc)
            if failure_class not in RETRYABLE_FAILURE_CLASSES or attempt >= policy.max_attempts:
                raise
            sleeper.sleep(policy.delay_for(attempt))
            attempt += 1


@dataclass(frozen=True)
class EndpointCachePolicy:
    """How long a cached value for one endpoint category remains usable as a
    *stale fallback* once a live read fails. Sized to how quickly that kind
    of AFL fact actually changes -- see the module docstring."""

    stale_ttl_seconds: float


DEFAULT_CACHE_POLICIES: dict[str, EndpointCachePolicy] = {
    "current_season": EndpointCachePolicy(stale_ttl_seconds=3600),
    "round": EndpointCachePolicy(stale_ttl_seconds=3600),
    "rounds": EndpointCachePolicy(stale_ttl_seconds=3600),
    "matches": EndpointCachePolicy(stale_ttl_seconds=120),
    "player": EndpointCachePolicy(stale_ttl_seconds=86400),
    "player_stats": EndpointCachePolicy(stale_ttl_seconds=90),
}


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    fetched_at: float


class EvidenceBatch:
    """Observes every `ResilientAflClient` call made while active (see
    `ResilientAflClient.evidence_batch`) and answers whether *all of them*
    were FRESH.

    `ResilientAflClient.is_evidence_fresh()` alone only reflects each
    endpoint *label*'s single most recent call -- if a build fetches
    player-stats for two different matches and the first falls back to a
    stale cache while the second succeeds live, the second call's FRESH
    record overwrites the first's STALE one under the shared "player_stats"
    label, so `is_evidence_fresh()` would wrongly report True even though
    part of the result is stale. A batch instead tracks every distinct call
    made during it (regardless of endpoint or cache key), so one stale fact
    among many still fails the batch. Exposes the same `is_evidence_fresh()`
    name as `ResilientAflClient` so `app.scorer_decisions.finalize`'s
    duck-typed check works against either."""

    def __init__(self) -> None:
        self._touched = False
        self._all_fresh = True

    def _observe(self, status: EvidenceStatus) -> None:
        self._touched = True
        if status != EvidenceStatus.FRESH:
            self._all_fresh = False

    def is_evidence_fresh(self) -> bool:
        return self._touched and self._all_fresh


class AflTransport(Protocol):
    """The subset of `AflApiClient` this module wraps -- also satisfied by
    any fake transport a test supplies."""

    def get_current_season(self) -> Any: ...

    def get_round(self, season_id: int, round_number: int) -> Any: ...

    def get_rounds(self, season_id: int) -> Any: ...

    def get_matches(self, round_id: int) -> Any: ...

    def get_player(self, canonical_player_id: int) -> Any: ...

    def get_match_player_stats(self, match_id: int) -> Any: ...


class ResilientAflClient:
    """Wraps an `AflTransport` with retry/backoff, per-endpoint caching, and
    diagnostics. Exposes exactly the same call surface as `AflApiClient`
    (get_current_season/get_round/get_rounds/get_matches/get_player/
    get_match_player_stats), so it is a drop-in replacement anywhere the app
    passes an AFL client around -- `app/service.py`'s `AflDataSource`
    protocol, `app/lockouts.py`, `app/calculations.py`, and
    `app/service.py`'s `PlayerIdentityCache` all keep working unchanged.
    """

    def __init__(
        self,
        transport: AflTransport,
        *,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        retry_policy: RetryPolicy | None = None,
        cache_policies: dict[str, EndpointCachePolicy] | None = None,
        diagnostics: AflDiagnosticsRegistry | None = None,
    ) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()
        self._sleeper = sleeper or RealSleeper()
        self._retry_policy = retry_policy or RetryPolicy()
        self._cache_policies = {**DEFAULT_CACHE_POLICIES, **(cache_policies or {})}
        self._cache: dict[tuple[str, Any], CacheEntry] = {}
        self.diagnostics = diagnostics or AflDiagnosticsRegistry()
        self._active_batch: EvidenceBatch | None = None

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()

    # -- AflDataSource-compatible surface ---------------------------------

    def get_current_season(self):
        return self._call("current_season", None, self._transport.get_current_season)

    def get_round(self, season_id: int, round_number: int):
        return self._call(
            "round", (season_id, round_number), lambda: self._transport.get_round(season_id, round_number)
        )

    def get_rounds(self, season_id: int):
        return self._call("rounds", season_id, lambda: self._transport.get_rounds(season_id))

    def get_matches(self, round_id: int):
        return self._call("matches", round_id, lambda: self._transport.get_matches(round_id))

    def get_player(self, canonical_player_id: int):
        return self._call(
            "player", canonical_player_id, lambda: self._transport.get_player(canonical_player_id)
        )

    def get_match_player_stats(self, match_id: int):
        return self._call(
            "player_stats", match_id, lambda: self._transport.get_match_player_stats(match_id)
        )

    # -- Evidence/diagnostics surface --------------------------------------

    def is_evidence_fresh(self) -> bool:
        """True only if every endpoint this client has ever been asked for
        was FRESH on its most recent read -- i.e. nothing served during the
        life of this client is currently masking a stale or failed upstream
        read. Used by authoritative, state-changing workflows (see
        `app.scorer_decisions.finalize`) to fail closed rather than finalise
        against evidence that might not reflect afl-api's current truth."""
        return self.diagnostics.all_fresh()

    def evidence_report(self) -> dict[str, dict]:
        """Secret-safe structured diagnostic snapshot -- see
        `app.afl_diagnostics.AflDiagnosticsRegistry.as_dict`."""
        return self.diagnostics.as_dict()

    @contextmanager
    def evidence_batch(self) -> Iterator[EvidenceBatch]:
        """Scopes freshness checking to exactly the calls made inside this
        `with` block -- see `EvidenceBatch`. Prefer this over
        `is_evidence_fresh()` around a "build a result, then maybe finalise
        it" sequence (see `app/routes/admin.py`'s finalize handler), since
        `is_evidence_fresh()` alone can miss a stale fact mixed in among
        several fresh ones fetched under the same endpoint label. The app
        only ever opens one such sequence at a time, but nesting is still
        well-defined: only the innermost open batch observes calls made
        while it is open, and the outer batch (unaware of those calls)
        becomes active again once the inner one exits.
        """
        batch = EvidenceBatch()
        previous = self._active_batch
        self._active_batch = batch
        try:
            yield batch
        finally:
            self._active_batch = previous

    # -- internals ----------------------------------------------------------

    def _call(self, endpoint: str, cache_key: Any, fetch: Callable[[], T]) -> T:
        correlation_id = new_correlation_id()
        policy = self._cache_policies.get(endpoint, EndpointCachePolicy(stale_ttl_seconds=0))
        try:
            value = call_with_retry(fetch, policy=self._retry_policy, sleeper=self._sleeper)
        except Exception as exc:  # noqa: BLE001 -- reclassified and re-raised below
            # Sampled *after* every retry/backoff attempt, not before --
            # retries can consume real time, and the stale-window check
            # below must charge that elapsed time against the cache entry's
            # age rather than pretending the whole retry sequence was
            # instantaneous (a P2 review finding on this PR).
            now = self._clock.now()
            failure_class = classify_failure(exc)
            if failure_class in RETRYABLE_FAILURE_CLASSES:
                entry = self._cache.get((endpoint, cache_key))
                if entry is not None and (now - entry.fetched_at) <= policy.stale_ttl_seconds:
                    self._record(endpoint, EvidenceStatus.STALE, correlation_id, failure_class=failure_class,
                                 cache_age_seconds=now - entry.fetched_at, detail=_safe_detail(exc))
                    return entry.value
                self._record(endpoint, EvidenceStatus.UNAVAILABLE, correlation_id, failure_class=failure_class,
                             detail=_safe_detail(exc))
                raise AflEvidenceUnavailableError(endpoint, cause=exc) from exc
            # Non-retryable: a contract/schema incompatibility or a genuine
            # client-side error. Never consult the cache here -- an
            # incompatible upstream response must stay visible, not be
            # hidden behind stale-but-structurally-valid cached data.
            status = EvidenceStatus.INVALID if failure_class == FailureClass.CONTRACT_ERROR else EvidenceStatus.UNAVAILABLE
            self._record(endpoint, status, correlation_id, failure_class=failure_class, detail=_safe_detail(exc))
            raise
        else:
            # Sampled after the live fetch completes (see the comment in the
            # except branch above) -- fetched_at reflects when the value was
            # actually obtained, not when this call started.
            now = self._clock.now()
            self._cache[(endpoint, cache_key)] = CacheEntry(value=value, fetched_at=now)
            self._record(endpoint, EvidenceStatus.FRESH, correlation_id)
            return value

    def _record(
        self,
        endpoint: str,
        status: EvidenceStatus,
        correlation_id: str,
        *,
        failure_class: FailureClass | None = None,
        detail: str | None = None,
        cache_age_seconds: float | None = None,
    ) -> None:
        self.diagnostics.record(
            endpoint,
            status,
            failure_class=failure_class,
            correlation_id=correlation_id,
            detail=detail,
            cache_age_seconds=cache_age_seconds,
        )
        if self._active_batch is not None:
            self._active_batch._observe(status)


def _safe_detail(exc: BaseException) -> str:
    """A short, human-readable failure description for diagnostics/logs.
    Built only from the exception's type name and `str()` -- every exception
    type this module classifies (`AflApiTimeoutError`/`AflApiConnectionError`/
    `AflApiHttpStatusError`/`KeyError`/`TypeError`/`ValueError`/plain
    `AflApiError`) constructs its message from the request path and/or
    status code only, never from request headers, so this can never surface
    an API key or other credential -- see `test_afl_resilience.py`'s
    redaction coverage."""
    return f"{type(exc).__name__}: {exc}"
