# Resilient `afl-api` client, cache and diagnostics

**Issue:** [#37 — Add resilient AFL client cache and diagnostics](https://github.com/JustPlausible/BBBFFL_Scoring/issues/37)
(roadmap work package **05**, `docs/roadmap/2027-season-roadmap.md`)

This document describes the resilience boundary built around
[`docs/afl-api-v1-contract.md`](afl-api-v1-contract.md)'s validated,
pinned `afl-api` `/api/v1` consumer contract. `afl-api` remains BBBFFL's
sole authority for AFL facts; nothing here adds a second copy of that
authority, scrapes AFL/Champion Data directly, or reaches into afl-api's
internal persistence. What this package adds is resilience *around* that
one authority: bounded timeouts, bounded transient retry, a
non-authoritative stale-fallback cache with explicit freshness metadata,
and diagnostics -- so a transient afl-api blip or outage degrades BBBFFL's
behaviour predictably instead of hanging, crashing, or silently treating
stale facts as current.

## Architecture

```
AflApiClient            -- validated transport (app/afl_client.py)
  |                          parses the pinned /api/v1 contract; raises
  |                          typed errors (timeout/connection/HTTP status);
  |                          never retries, never caches.
  v
ResilientAflClient       -- retry/timeout policy + cache + evidence state
  |                          (app/afl_resilience.py). Same call surface as
  |                          AflApiClient -- a drop-in AflDataSource.
  v
AflDiagnosticsRegistry   -- structured, secret-safe diagnostic state
                             (app/afl_diagnostics.py), read by
                             GET /api/admin/afl-diagnostics.
```

Every production BBBFFL read of AFL facts goes through this one chain --
`app/main.py` constructs exactly one `AflApiClient` (the transport) wrapped
in exactly one `ResilientAflClient`, and stores it as `app.state.afl_client`.
`app/service.py` (`AflDataSource` protocol, `PlayerIdentityCache`),
`app/lockouts.py` (`RoundMatchFactsProvider`) and `app/calculations.py`
(`MatchupCalculationService`) all depend on that same call surface
(`get_current_season`/`get_round`/`get_rounds`/`get_matches`/`get_player`/
`get_match_player_stats`) without knowing or caring whether the concrete
object is a bare `AflApiClient` or a `ResilientAflClient` wrapping one --
this is what makes the wrapper a genuine drop-in rather than a parallel code
path. There is no other place in the application that makes an HTTP request
to afl-api; `scripts/afl_contract_diagnostic.py` is a separate, explicitly
opt-in live-integration diagnostic (never part of the hermetic test suite
or normal request handling) and is unaffected by this package.

## Timeout policy

`AflApiClient` (the transport) takes an explicit `httpx.Timeout` built from
three settings (`app/config.py`):

| Setting | Meaning | Default |
| --- | --- | --- |
| `AFL_API_TIMEOUT_SECONDS` | Overall default, used for any phase not overridden below. | `10` |
| `AFL_API_CONNECT_TIMEOUT_SECONDS` | How long to wait for the TCP/TLS handshake. | unset -> falls back to the overall default |
| `AFL_API_READ_TIMEOUT_SECONDS` | How long to wait for the response body once connected. | unset -> falls back to the overall default |

There is no configuration under which a request can wait indefinitely. A
timeout raises a typed `AflApiTimeoutError` carrying `phase` ("connect" or
"read"), distinct from `AflApiConnectionError` (the connection failed
outright -- refused, DNS failure, reset) and `AflApiHttpStatusError`
(afl-api responded with a non-2xx status, carrying `status_code`). All
three are `AflApiError` subclasses, so any existing code that already
catches `AflApiError` (e.g. `app/main.py`'s global exception handler)
continues to work unchanged.

None of these three exception types ever include request headers in their
message -- only the request path and, where relevant, the status code or
timeout phase -- so `AFL_API_KEY` can never leak into a log line, an
exception message, or a diagnostic report built from `str(exc)`. See
`tests/test_afl_client.py::test_no_error_message_ever_includes_the_api_key`
and `tests/test_afl_resilience.py::test_diagnostics_never_expose_the_api_key_or_headers`.

## Retry/backoff policy

`ResilientAflClient` (`app/afl_resilience.py`) retries a request against a
`RetryPolicy`: bounded attempts (`AFL_API_RETRY_MAX_ATTEMPTS`, default 3,
counting the first attempt), bounded exponential backoff capped at
`AFL_API_RETRY_MAX_DELAY_SECONDS` (default 2s) starting from
`AFL_API_RETRY_BASE_DELAY_SECONDS` (default 0.2s). There is no jitter --
the bound comes from `max_attempts` and `max_delay_seconds`, not
randomisation, so retry behaviour is exactly reproducible in tests. Delay
is applied via an injectable `Sleeper`; tests supply a fake that advances a
fake `Clock` instead of calling `time.sleep`, so the whole suite is
network- and wall-clock-free (`tests/test_afl_resilience.py`).

### Retryable failure classes

| Class | Cause |
| --- | --- |
| `CONNECT_TIMEOUT` | afl-api never accepted the connection within the connect timeout. |
| `READ_TIMEOUT` | Connected, but the response did not arrive within the read timeout. |
| `CONNECTION_ERROR` | DNS failure, refused connection, reset, TLS failure. |
| `TRANSIENT_HTTP` | afl-api responded 408, 429, 500, 502, 503 or 504. |

### Non-retryable failure classes (fail immediately, never masked by cache)

| Class | Cause |
| --- | --- |
| `CLIENT_HTTP` | Any other non-2xx status (404, 401, 403, 422, ...) -- a request problem, not a transient upstream blip. |
| `CONTRACT_ERROR` | A required field/wrapper is missing or the wrong shape (`KeyError`/`TypeError`/`ValueError` from `AflApiClient`'s parsing, or a plain `AflApiError` such as "no season with `is_current=true`"). This is exactly the "afl-api's v1 contract changed incompatibly" case `docs/afl-api-v1-contract.md` requires to stay visible. |
| `UNKNOWN` | Anything else -- treated conservatively as non-retryable. |

A contract/schema incompatibility is **never** retried and **never** served
from a stale cache, no matter how fresh that cache is -- it always
propagates as a live, visible error. Retrying it would not help (the
response shape will not change on the next attempt), and masking it behind
a stale cache would hide a real incompatibility indefinitely, which is
exactly what issue #37 prohibits.

## Cache and freshness semantics

`ResilientAflClient` always attempts a live request first -- the cache is
never consulted to *skip* a live read, only to survive a *failed* one, so
resilience never comes at the cost of serving avoidably stale data while
afl-api is healthy. Each endpoint category has its own stale-tolerance
window (`EndpointCachePolicy.stale_ttl_seconds`), sized to how quickly that
kind of AFL fact actually changes:

| Endpoint category | Stale window | Rationale |
| --- | --- | --- |
| `current_season` | 1 hour | Changes only at season rollover. |
| `round` / `rounds` | 1 hour | Round identity/numbering is static once published. |
| `player` | 24 hours | Player identity/current club changes slowly. |
| `matches` | 2 minutes | Match status/schedule can change during live play. |
| `player_stats` | 90 seconds | Live scoring data; a long stale window would misrepresent the game. |

These are the resilience/read-optimisation cache the issue asks for --
policy differs by endpoint semantics rather than treating every AFL
resource as equally volatile.

### Explicit evidence state

Every read is tagged with one of four statuses
(`app.afl_diagnostics.EvidenceStatus`):

- **`fresh`** -- a live afl-api response was just parsed successfully.
- **`stale`** -- the live read failed transiently; a cached value within
  that endpoint's stale window was returned instead. The caller gets the
  same domain object it would have gotten from a live call (so existing
  consumers are unaffected), but the client's diagnostics record that this
  particular read was not fresh.
- **`unavailable`** -- no live read succeeded and no usable cached value
  exists (a **cold-cache outage**, e.g. the very first request after
  startup fails), or the cached value is older than that endpoint's policy
  allows. `ResilientAflClient` raises `AflEvidenceUnavailableError` (an
  `AflApiError` subclass) in this case -- the caller gets a visible error,
  not silently wrong data.
- **`invalid`** -- the response was structurally/semantically incompatible
  with the pinned contract. Always raised as a live error (see above);
  never served from cache.

`ResilientAflClient.is_evidence_fresh()` returns `True` only when every
endpoint the client has ever been asked for was `fresh` on its most recent
read. This is what stale AFL data cannot silently masquerade as fresh
evidence: any consumer that cares about that distinction (see below) can
ask.

`is_evidence_fresh()` alone tracks freshness per *endpoint label*
("matches", "player_stats", ...), not per distinct fact -- if a round has
two matches, fetching player-stats for both makes two calls under the same
"player_stats" label, and a later fresh call's record would overwrite an
earlier stale one. `ResilientAflClient.evidence_batch()` (a context
manager) fixes this by tracking every distinct call made while it is open,
so one stale fact mixed in among several fresh ones still fails the batch:

```python
with afl_client.evidence_batch() as evidence:
    result = build_matchup_state(afl_client, teams, decisions, identity_cache)
    finalize(result, decisions, note, afl_client=evidence)  # checks evidence.is_evidence_fresh()
```

Both admin routes (`app/routes/admin.py`, `app/routes/superscore.py`) use
this pattern, falling back to the plain client (via `contextlib.nullcontext`)
for any AFL client that does not support batching -- `finalize()` already
tolerates a client with no `is_evidence_fresh()` at all.

## How stale evidence is represented, and how finalisation fails closed

`app/scorer_decisions.py::finalize` -- the function both the Grand Final
(`app/routes/admin.py`) and SuperScore (`app/routes/superscore.py`) admin
routes call to freeze an official result -- accepts an optional
`afl_client` parameter. When supplied and it exposes `is_evidence_fresh()`,
`finalize()` calls it immediately before writing the frozen snapshot: if
any AFL fact the just-computed result depends on was served stale or is
currently unavailable, finalisation is refused with `StaleAflEvidenceError`
(mapped to HTTP `503` by `app/main.py`) rather than freezing a result that
might not reflect afl-api's current truth. Both admin routes pass an
`EvidenceBatch` (see above) scoped to the `build_matchup_state`/
`build_superscore_state` call that produced the result, not the client's
own `is_evidence_fresh()` directly, so a stale fact fetched under the same
endpoint label as a later fresh one is still caught.

This check is purely additive and backward compatible: `afl_client`
defaults to `None`, and any caller that omits it (every existing direct
call to `finalize()` in tests, and any duck-typed AFL client without
`is_evidence_fresh()`, such as the hermetic `FakeAflClient` used throughout
the test suite) skips the check entirely and keeps its exact prior
behaviour.

A cold-cache outage is handled even earlier: `build_matchup_state`/
`build_superscore_state` call the AFL client directly, so an unavailable
endpoint raises `AflEvidenceUnavailableError` before a `MatchupResult`/
`SuperScoreResult` is even produced -- finalisation is never reached at
all. `finalize()`'s freshness check specifically covers the remaining case:
a *stale-but-successfully-returned* result whose evidence is not
confirmed current.

### Deliberate limitation / follow-up

Roadmap package 05 (this issue) scopes only the AFL client boundary and the
Grand Final/SuperScore vertical's existing finalisation path
(`app.scorer_decisions.finalize`), the one authoritative "commit" path
currently wired to an HTTP route. The newer persisted competition
architecture's calculation/publication path
(`app.calculations.MatchupCalculationService` ->
`app.competition_lifecycle.CompetitionLifecycleRepository.publish_results`)
is not yet wired to any route -- `publish_results` takes already-computed
scores as a parameter and does not itself call the AFL client. When a
later roadmap package (28, "scorer round review, sign-off and correction
workflow") wires `MatchupCalculationService`'s output into
`publish_results`, that caller must consult the same
`ResilientAflClient.is_evidence_fresh()` guard (or an equivalent freshness
check against the round's `bbbffl_round_lifecycle` state) before treating a
calculation as ready for official publication, following the same
fail-closed pattern documented here.

## Diagnostics

`GET /api/admin/afl-diagnostics` (protected by the same `X-Admin-Token` as
every other `/api/admin/*` route) returns a structured, secret-safe
snapshot:

```json
{
  "dependency": "afl-api",
  "endpoints": {
    "matches": {
      "endpoint": "matches",
      "status": "stale",
      "last_success_at": "2026-08-27T10:15:00+00:00",
      "last_failure_at": "2026-08-27T10:16:12+00:00",
      "last_failure_class": "connection_error",
      "last_correlation_id": "5b1e...",
      "last_detail": "AflEvidenceUnavailableError: ...",
      "cache_age_seconds": 72.3
    }
  }
}
```

Every field is one of: a fixed internal endpoint label, a status enum, a
timestamp, a failure-class enum, a correlation ID this process generated
itself, or a short exception type+message built only from a request
path/status code -- never a request header, so `AFL_API_KEY` (or any other
credential) can never appear in this report. There is deliberately no
redaction step because there is nothing secret to redact; this is pinned by
`tests/test_afl_diagnostics.py` and
`tests/test_afl_resilience.py::test_diagnostics_never_expose_the_api_key_or_headers`.

A correlation ID is generated fresh for every upstream call attempt (reusing
`app.audit.new_correlation_id`, the same UUID4 generator the audit-event
boundary uses) purely for internal log/diagnostic correlation -- it is not
sent to afl-api as a request header, since that is not part of the pinned
`/api/v1` contract.

This is deliberately an in-memory, per-process registry, not a persisted
table: BBBFFL runs as one process (see `AflApiClient`'s own docstring), and
this state only needs to answer "what is the current health of the afl-api
dependency" for the life of that process -- it is not season/audit history.
No migration was needed for this package; see "Compatibility" in the
package 05 PR description for why persistence was deliberately not added.

## Testing

`tests/test_afl_resilience.py`, `tests/test_afl_diagnostics.py`, and the
additions to `tests/test_afl_client.py` and `tests/test_scorer_decisions.py`
cover, entirely offline and without real time:

- a successful fresh request;
- an explicit connect timeout and an explicit read timeout, each raising a
  distinctly-phased `AflApiTimeoutError`;
- a transient failure followed by a successful retry;
- exhausted transient retries (bounded, never a retry storm);
- a stale-cache fallback within an endpoint's policy window;
- a stale cache falling out of policy once older than its window;
- a cold-cache outage, distinguishable from a stale-served response;
- recovery after an outage refreshing the cache back to `fresh`;
- an invalid/schema-incompatible response remaining a visible error, never
  retried and never masked by an available stale cache;
- diagnostics accurately reflecting dependency, endpoint, failure class and
  correlation ID;
- an `evidence_batch()` catching a stale call masked by a later fresh one
  under the same endpoint label, which `is_evidence_fresh()` alone misses;
- the stale-window check charging elapsed retry/backoff time against a
  cache entry's age, not just the time before retries started;
- credential/API-key redaction (by construction, exercised end-to-end);
- `scorer_decisions.finalize` failing closed on stale/unavailable evidence,
  succeeding on fresh evidence, and staying backward compatible for every
  caller that does not opt in;
- the same fail-closed behaviour end-to-end through the real
  `/api/admin/finalize` HTTP route (`tests/test_api.py`).
