"""Deterministic, network-free coverage for app/afl_resilience.py (roadmap
package 05, issue #37): timeout classification, bounded transient retry with
a fake clock/sleeper (never a real sleep), stale-cache fallback, cold-cache
outage, invalid/contract-incompatible responses staying visible, recovery
after an outage, and secret-safe diagnostics.
"""

import pytest

from app.afl_client import (
    AflApiConnectionError,
    AflApiError,
    AflApiHttpStatusError,
    AflApiTimeoutError,
)
from app.afl_resilience import (
    AflEvidenceUnavailableError,
    EndpointCachePolicy,
    FailureClass,
    ResilientAflClient,
    RetryPolicy,
    call_with_retry,
    classify_failure,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSleeper:
    """Records requested delays and advances a FakeClock instead of
    sleeping -- retry/backoff tests never wait in real time."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class ScriptedTransport:
    """A fake AflApiClient-shaped transport whose `get_matches` (and
    friends) replay a scripted sequence of outcomes -- either a return value
    or an exception to raise -- one per call."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = 0

    def _next(self):
        self.calls += 1
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get_current_season(self):
        return self._next()

    def get_round(self, season_id, round_number):
        return self._next()

    def get_rounds(self, season_id):
        return self._next()

    def get_matches(self, round_id):
        return self._next()

    def get_player(self, canonical_player_id):
        return self._next()

    def get_match_player_stats(self, match_id):
        return self._next()


# -- classify_failure ---------------------------------------------------


def test_classify_failure_distinguishes_connect_and_read_timeouts():
    assert classify_failure(AflApiTimeoutError("/x", phase="connect")) == FailureClass.CONNECT_TIMEOUT
    assert classify_failure(AflApiTimeoutError("/x", phase="read")) == FailureClass.READ_TIMEOUT


def test_classify_failure_treats_connection_failure_as_retryable():
    assert classify_failure(AflApiConnectionError("/x")) == FailureClass.CONNECTION_ERROR


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_classify_failure_treats_5xx_and_429_as_transient(status):
    assert classify_failure(AflApiHttpStatusError("/x", status_code=status)) == FailureClass.TRANSIENT_HTTP


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_classify_failure_treats_other_4xx_as_non_retryable_client_error(status):
    assert classify_failure(AflApiHttpStatusError("/x", status_code=status)) == FailureClass.CLIENT_HTTP


def test_classify_failure_treats_missing_field_as_contract_error():
    """A parsing failure (KeyError from AflApiClient's `entry["season_id"]`
    style access) is the "afl-api contract changed incompatibly" case -- it
    must never be treated as a transient, retryable failure."""
    assert classify_failure(KeyError("season_id")) == FailureClass.CONTRACT_ERROR
    assert classify_failure(TypeError("bad shape")) == FailureClass.CONTRACT_ERROR


def test_classify_failure_treats_generic_afl_api_error_as_contract_error():
    assert classify_failure(AflApiError("no current season")) == FailureClass.CONTRACT_ERROR


def test_classify_failure_defaults_unknown_exceptions_to_non_retryable():
    assert classify_failure(RuntimeError("something else")) == FailureClass.UNKNOWN


# -- call_with_retry / RetryPolicy --------------------------------------


def test_call_with_retry_succeeds_without_retry_on_first_try():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    result = call_with_retry(fn, policy=RetryPolicy(), sleeper=sleeper)
    assert result == "ok"
    assert calls["n"] == 1
    assert sleeper.calls == []


def test_call_with_retry_retries_transient_failure_then_succeeds():
    """Requirement: transient failure followed by successful retry."""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    outcomes = [AflApiConnectionError("/x"), "ok"]

    def fn():
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    result = call_with_retry(fn, policy=RetryPolicy(max_attempts=3), sleeper=sleeper)
    assert result == "ok"
    assert len(sleeper.calls) == 1  # exactly one bounded backoff sleep


def test_call_with_retry_exhausts_bounded_attempts_and_raises():
    """Requirement: exhausted transient retries -- bounded, no retry storm."""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise AflApiConnectionError("/x")

    with pytest.raises(AflApiConnectionError):
        call_with_retry(fn, policy=RetryPolicy(max_attempts=3), sleeper=sleeper)
    assert attempts["n"] == 3
    assert len(sleeper.calls) == 2  # bounded: attempts - 1 sleeps, never unbounded


def test_call_with_retry_never_retries_contract_errors():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise KeyError("season_id")

    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    with pytest.raises(KeyError):
        call_with_retry(fn, policy=RetryPolicy(max_attempts=5), sleeper=sleeper)
    assert attempts["n"] == 1
    assert sleeper.calls == []


def test_call_with_retry_never_retries_non_transient_http_status():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise AflApiHttpStatusError("/x", status_code=404)

    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    with pytest.raises(AflApiHttpStatusError):
        call_with_retry(fn, policy=RetryPolicy(max_attempts=5), sleeper=sleeper)
    assert attempts["n"] == 1


def test_retry_policy_delay_is_bounded_by_max_delay():
    policy = RetryPolicy(base_delay_seconds=1.0, multiplier=10.0, max_delay_seconds=3.0)
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 3.0  # would be 10.0 uncapped
    assert policy.delay_for(5) == 3.0


# -- ResilientAflClient: success / timeout / cache / evidence ------------


def test_successful_fresh_request_is_returned_and_marked_fresh():
    transport = ScriptedTransport([[1, 2, 3]])
    client = ResilientAflClient(transport, sleeper=FakeSleeper(FakeClock()))
    assert client.get_matches(1) == [1, 2, 3]
    report = client.evidence_report()
    assert report["dependency"] == "afl-api"
    assert report["endpoints"]["matches"]["status"] == "fresh"
    assert client.is_evidence_fresh() is True


def test_explicit_timeout_with_no_cache_raises_and_is_reported_unavailable():
    transport = ScriptedTransport([AflApiTimeoutError("/x", phase="read")] * 3)
    clock = FakeClock()
    client = ResilientAflClient(
        transport, clock=clock, sleeper=FakeSleeper(clock), retry_policy=RetryPolicy(max_attempts=3)
    )
    with pytest.raises(AflEvidenceUnavailableError):
        client.get_matches(1)
    report = client.evidence_report()
    assert report["endpoints"]["matches"]["status"] == "unavailable"
    assert report["endpoints"]["matches"]["last_failure_class"] == "read_timeout"
    assert client.is_evidence_fresh() is False


def test_transient_failure_then_successful_retry_returns_live_value():
    transport = ScriptedTransport([AflApiConnectionError("/x"), [42]])
    clock = FakeClock()
    client = ResilientAflClient(transport, clock=clock, sleeper=FakeSleeper(clock))
    assert client.get_matches(1) == [42]
    assert client.evidence_report()["endpoints"]["matches"]["status"] == "fresh"


def test_exhausted_transient_retries_raise_evidence_unavailable():
    transport = ScriptedTransport([AflApiConnectionError("/x")] * 3)
    clock = FakeClock()
    client = ResilientAflClient(
        transport, clock=clock, sleeper=FakeSleeper(clock), retry_policy=RetryPolicy(max_attempts=3)
    )
    with pytest.raises(AflEvidenceUnavailableError) as excinfo:
        client.get_matches(1)
    assert excinfo.value.endpoint == "matches"
    assert transport.calls == 3


def test_stale_cache_fallback_is_returned_within_policy_and_marked_stale():
    transport = ScriptedTransport([["live-value"], AflApiConnectionError("/x")])
    clock = FakeClock()
    client = ResilientAflClient(
        transport,
        clock=clock,
        sleeper=FakeSleeper(clock),
        retry_policy=RetryPolicy(max_attempts=1),  # one failing attempt, no interior backoff sleep
        cache_policies={"matches": EndpointCachePolicy(stale_ttl_seconds=100)},
    )
    assert client.get_matches(1) == ["live-value"]
    clock.advance(10)
    assert client.get_matches(1) == ["live-value"]  # served from stale cache
    report = client.evidence_report()
    assert report["endpoints"]["matches"]["status"] == "stale"
    assert report["endpoints"]["matches"]["cache_age_seconds"] == 10
    assert client.is_evidence_fresh() is False


def test_cold_cache_outage_is_distinguishable_from_stale_serve():
    """A first-ever call that fails transiently has no cache to fall back
    to -- this must raise, not silently return anything, and must be
    distinguishable (by status/failure) from the "stale served" case above."""
    transport = ScriptedTransport([AflApiConnectionError("/x")] * 3)
    clock = FakeClock()
    client = ResilientAflClient(
        transport, clock=clock, sleeper=FakeSleeper(clock), retry_policy=RetryPolicy(max_attempts=3)
    )
    with pytest.raises(AflEvidenceUnavailableError):
        client.get_matches(1)
    assert client.evidence_report()["endpoints"]["matches"]["status"] == "unavailable"


def test_stale_cache_is_not_offered_once_older_than_policy_window():
    transport = ScriptedTransport([["live-value"], AflApiConnectionError("/x")])
    clock = FakeClock()
    client = ResilientAflClient(
        transport,
        clock=clock,
        sleeper=FakeSleeper(clock),
        retry_policy=RetryPolicy(max_attempts=1),
        cache_policies={"matches": EndpointCachePolicy(stale_ttl_seconds=5)},
    )
    assert client.get_matches(1) == ["live-value"]
    clock.advance(6)  # older than the 5s stale window
    with pytest.raises(AflEvidenceUnavailableError):
        client.get_matches(1)


def test_recovery_after_outage_refreshes_cache_and_marks_fresh_again():
    transport = ScriptedTransport([["v1"], AflApiConnectionError("/x"), ["v2"]])
    clock = FakeClock()
    client = ResilientAflClient(
        transport,
        clock=clock,
        sleeper=FakeSleeper(clock),
        retry_policy=RetryPolicy(max_attempts=1),
        cache_policies={"matches": EndpointCachePolicy(stale_ttl_seconds=100)},
    )
    assert client.get_matches(1) == ["v1"]
    assert client.get_matches(1) == ["v1"]  # stale fallback
    assert client.evidence_report()["endpoints"]["matches"]["status"] == "stale"
    assert client.get_matches(1) == ["v2"]  # live succeeds again
    assert client.evidence_report()["endpoints"]["matches"]["status"] == "fresh"
    assert client.is_evidence_fresh() is True


def test_invalid_response_remains_an_error_even_with_a_fresh_cache_available():
    """Requirement: an invalid/schema-incompatible response must stay a
    visible error, never silently masked by a perfectly good stale cache."""
    transport = ScriptedTransport([["ok"], KeyError("season_id")])
    clock = FakeClock()
    client = ResilientAflClient(
        transport,
        clock=clock,
        sleeper=FakeSleeper(clock),
        cache_policies={"matches": EndpointCachePolicy(stale_ttl_seconds=10_000)},
    )
    assert client.get_matches(1) == ["ok"]
    with pytest.raises(KeyError):
        client.get_matches(1)
    report = client.evidence_report()
    assert report["endpoints"]["matches"]["status"] == "invalid"
    assert client.is_evidence_fresh() is False


def test_invalid_response_is_never_retried():
    transport = ScriptedTransport([KeyError("season_id"), ["would-succeed-if-retried"]])
    clock = FakeClock()
    client = ResilientAflClient(transport, clock=clock, sleeper=FakeSleeper(clock))
    with pytest.raises(KeyError):
        client.get_matches(1)
    assert transport.calls == 1


def test_non_retryable_client_http_error_is_never_retried_or_cached_over():
    transport = ScriptedTransport([AflApiHttpStatusError("/x", status_code=404)])
    clock = FakeClock()
    client = ResilientAflClient(transport, clock=clock, sleeper=FakeSleeper(clock))
    with pytest.raises(AflApiHttpStatusError):
        client.get_matches(1)
    assert client.evidence_report()["endpoints"]["matches"]["status"] == "unavailable"


def test_diagnostics_accurately_reflect_dependency_endpoint_and_correlation():
    transport = ScriptedTransport([["ok"]])
    clock = FakeClock()
    client = ResilientAflClient(transport, clock=clock, sleeper=FakeSleeper(clock))
    client.get_matches(1)
    endpoint_report = client.evidence_report()["endpoints"]["matches"]
    assert endpoint_report["endpoint"] == "matches"
    assert endpoint_report["last_correlation_id"]  # generated, non-empty
    assert client.evidence_report()["dependency"] == "afl-api"


def test_a_never_called_client_is_not_considered_evidence_fresh():
    transport = ScriptedTransport([])
    client = ResilientAflClient(transport, sleeper=FakeSleeper(FakeClock()))
    assert client.is_evidence_fresh() is False


def test_each_endpoint_caches_independently_by_key():
    transport = ScriptedTransport([["round-1-matches"], ["round-2-matches"]])
    client = ResilientAflClient(transport, sleeper=FakeSleeper(FakeClock()))
    assert client.get_matches(1) == ["round-1-matches"]
    assert client.get_matches(2) == ["round-2-matches"]


def test_close_delegates_to_transport_close():
    closed = {"value": False}

    class Transport(ScriptedTransport):
        def close(self):
            closed["value"] = True

    client = ResilientAflClient(Transport([]), sleeper=FakeSleeper(FakeClock()))
    client.close()
    assert closed["value"] is True


# -- Secret redaction -----------------------------------------------------


def test_evidence_batch_catches_a_stale_call_masked_by_a_later_fresh_one():
    """Regression for a review finding on this PR: is_evidence_fresh() alone
    only reflects each endpoint *label*'s single most recent call. Scoring a
    round with two matches fetches player-stats twice under the same
    "player_stats" label -- if the first falls back to stale and the second
    succeeds live, the plain per-endpoint status would show FRESH even
    though part of the result is stale. evidence_batch() must still catch
    it because it tracks every call made during the batch, not just the
    latest one per label."""
    transport = ScriptedTransport([["match-1-stats"], AflApiConnectionError("/x"), ["match-2-stats"]])
    clock = FakeClock()
    client = ResilientAflClient(
        transport,
        clock=clock,
        sleeper=FakeSleeper(clock),
        retry_policy=RetryPolicy(max_attempts=1),
        cache_policies={"player_stats": EndpointCachePolicy(stale_ttl_seconds=100)},
    )
    with client.evidence_batch() as batch:
        client.get_match_player_stats(1)  # fresh, primes the cache under key 1
        client.get_match_player_stats(1)  # fails live -> served stale from cache
        client.get_match_player_stats(2)  # fresh, a *different* cache key

    # The naive per-endpoint-label view would say "fresh" (endpoint
    # "player_stats"'s single most recent call succeeded) -- proving the
    # bug this batch exists to catch.
    assert client.evidence_report()["endpoints"]["player_stats"]["status"] == "fresh"
    assert client.is_evidence_fresh() is True

    # The batch, having observed all three calls, correctly reports False.
    assert batch.is_evidence_fresh() is False


def test_evidence_batch_reports_fresh_when_every_call_inside_it_is_fresh():
    transport = ScriptedTransport([["a"], ["b"]])
    client = ResilientAflClient(transport, sleeper=FakeSleeper(FakeClock()))
    with client.evidence_batch() as batch:
        client.get_matches(1)
        client.get_player(2)
    assert batch.is_evidence_fresh() is True


def test_evidence_batch_reports_not_fresh_when_untouched():
    client = ResilientAflClient(ScriptedTransport([]), sleeper=FakeSleeper(FakeClock()))
    with client.evidence_batch() as batch:
        pass
    assert batch.is_evidence_fresh() is False


def test_evidence_batch_does_not_observe_calls_made_outside_it():
    transport = ScriptedTransport([["before"], AflApiConnectionError("/x")])
    clock = FakeClock()
    client = ResilientAflClient(
        transport, clock=clock, sleeper=FakeSleeper(clock), retry_policy=RetryPolicy(max_attempts=1)
    )
    client.get_matches(1)  # fresh, outside any batch
    with client.evidence_batch() as batch:
        with pytest.raises(AflEvidenceUnavailableError):
            client.get_player(2)  # unavailable, inside the batch
    assert batch.is_evidence_fresh() is False


def test_stale_ttl_accounts_for_time_consumed_during_retries():
    """Regression for a review finding on this PR: the clock used to decide
    whether a cached value is still within its stale window must be sampled
    *after* retries/backoff, not before -- otherwise a slow retry sequence
    can serve a cache entry that is already older than its configured
    maximum age. Uses the default multi-attempt RetryPolicy so the fake
    sleeper's backoff delays actually advance the clock mid-call."""
    transport = ScriptedTransport(
        [
            ["live-value"],
            AflApiConnectionError("/x"),
            AflApiConnectionError("/x"),
            AflApiConnectionError("/x"),
        ]
    )
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    client = ResilientAflClient(
        transport,
        clock=clock,
        sleeper=sleeper,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=3.0, multiplier=1.0, max_delay_seconds=3.0),
        cache_policies={"matches": EndpointCachePolicy(stale_ttl_seconds=5)},
    )
    assert client.get_matches(1) == ["live-value"]
    clock.advance(4)  # entry is 4s old -- within the 5s window at call start
    # Three attempts fail, sleeping 3s after each of the first two (bounded
    # by max_attempts=3) -- 6s of retry time elapses during this one call,
    # so by the time the stale-fallback decision is made the entry is 10s
    # old, past the 5s window.
    with pytest.raises(AflEvidenceUnavailableError):
        client.get_matches(1)
    assert sum(sleeper.calls) == 6.0


def test_diagnostics_never_expose_the_api_key_or_headers():
    """Even when the underlying transport failure text could theoretically
    carry request context, AflApiTimeoutError/AflApiConnectionError/
    AflApiHttpStatusError messages are built only from a path/status code
    (see app/afl_client.py) -- never headers -- so a diagnostics report can
    never leak AFL_API_KEY. This asserts it end-to-end through the
    diagnostics report and the raised exception's own message."""
    secret = "super-secret-afl-api-key-do-not-leak"
    transport = ScriptedTransport([AflApiConnectionError("/api/v1/matches/1")] * 3)
    clock = FakeClock()
    client = ResilientAflClient(
        transport, clock=clock, sleeper=FakeSleeper(clock), retry_policy=RetryPolicy(max_attempts=3)
    )
    with pytest.raises(AflEvidenceUnavailableError) as excinfo:
        client.get_matches(1)
    report = client.evidence_report()
    serialized = repr(report) + str(excinfo.value)
    assert secret not in serialized
    assert "x-api-key" not in serialized.lower()
    assert "authorization" not in serialized.lower()
