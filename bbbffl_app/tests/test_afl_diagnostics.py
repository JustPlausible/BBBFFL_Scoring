"""Unit coverage for app/afl_diagnostics.py's registry -- the structured,
secret-safe diagnostic state app/afl_resilience.py's ResilientAflClient
writes to and app/routes/admin.py's afl-diagnostics endpoint reads back."""

from app.afl_diagnostics import AflDiagnosticsRegistry, EvidenceStatus


def test_fresh_record_clears_status_but_keeps_prior_failure_history():
    registry = AflDiagnosticsRegistry()
    registry.record("matches", EvidenceStatus.UNAVAILABLE, failure_class="connection_error", detail="boom")
    registry.record("matches", EvidenceStatus.FRESH)
    entry = registry.snapshot()["matches"]
    assert entry.status == EvidenceStatus.FRESH
    assert entry.last_success_at is not None
    # Prior failure info is retained for operator visibility even after
    # recovery -- "most recent refresh/failure information" per issue #37.
    assert entry.last_failure_class == "connection_error"
    assert entry.last_failure_at is not None


def test_failure_record_keeps_prior_success_timestamp():
    registry = AflDiagnosticsRegistry()
    registry.record("matches", EvidenceStatus.FRESH)
    first_success = registry.snapshot()["matches"].last_success_at
    registry.record("matches", EvidenceStatus.UNAVAILABLE, failure_class="connection_error")
    entry = registry.snapshot()["matches"]
    assert entry.status == EvidenceStatus.UNAVAILABLE
    assert entry.last_success_at == first_success


def test_all_fresh_is_false_when_nothing_has_ever_been_recorded():
    registry = AflDiagnosticsRegistry()
    assert registry.all_fresh() is False


def test_all_fresh_requires_every_recorded_endpoint_to_be_fresh():
    registry = AflDiagnosticsRegistry()
    registry.record("matches", EvidenceStatus.FRESH)
    registry.record("player_stats", EvidenceStatus.FRESH)
    assert registry.all_fresh() is True
    registry.record("player_stats", EvidenceStatus.STALE, failure_class="connection_error")
    assert registry.all_fresh() is False


def test_as_dict_is_json_shaped_and_names_the_dependency():
    registry = AflDiagnosticsRegistry()
    registry.record("matches", EvidenceStatus.FRESH, correlation_id="corr-1")
    report = registry.as_dict()
    assert report["dependency"] == "afl-api"
    assert report["endpoints"]["matches"]["status"] == "fresh"
    assert report["endpoints"]["matches"]["last_correlation_id"] == "corr-1"


def test_as_dict_never_contains_credential_looking_keys():
    registry = AflDiagnosticsRegistry()
    registry.record("matches", EvidenceStatus.UNAVAILABLE, failure_class="connection_error", detail="boom")
    serialized = repr(registry.as_dict()).lower()
    for forbidden in ("api_key", "x-api-key", "authorization", "password", "secret", "token"):
        assert forbidden not in serialized
