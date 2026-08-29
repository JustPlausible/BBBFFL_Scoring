"""Coverage for app/auth_rate_limit.py's process-local bounded login-attempt
limiter (roadmap package 19, issue #74)."""

import pytest

from app.auth_rate_limit import LoginRateLimiter, RateLimitedError


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_check_allows_attempts_below_the_threshold():
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=60)
    limiter.check("coach@example.com")
    limiter.record_failure("coach@example.com")
    limiter.check("coach@example.com")
    limiter.record_failure("coach@example.com")
    limiter.check("coach@example.com")  # still below max_attempts=3


def test_reaching_max_attempts_locks_out_the_key():
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=60)
    for _ in range(3):
        limiter.check("coach@example.com")
        limiter.record_failure("coach@example.com")

    with pytest.raises(RateLimitedError):
        limiter.check("coach@example.com")


def test_lockout_is_bounded_not_permanent():
    """A locked-out key becomes usable again once lockout_seconds elapses
    -- see issue #74's "avoid permanently locking legitimate users out"."""
    clock = FakeClock(start=0.0)
    limiter = LoginRateLimiter(max_attempts=2, lockout_seconds=60, clock=clock)
    for _ in range(2):
        limiter.check("coach@example.com")
        limiter.record_failure("coach@example.com")
    with pytest.raises(RateLimitedError):
        limiter.check("coach@example.com")

    clock.advance(61)
    limiter.check("coach@example.com")  # no longer locked out


def test_successful_login_resets_the_counter():
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=60)
    limiter.record_failure("coach@example.com")
    limiter.record_failure("coach@example.com")
    limiter.record_success("coach@example.com")

    # Two more failures after a reset must not trip the lockout that would
    # have applied to four consecutive failures.
    limiter.check("coach@example.com")
    limiter.record_failure("coach@example.com")
    limiter.check("coach@example.com")


def test_keys_are_independent():
    """A lockout on one key (e.g. one coach's email) must never affect a
    different key (a different coach, or the per-IP counter)."""
    limiter = LoginRateLimiter(max_attempts=1, lockout_seconds=60)
    limiter.check("victim@example.com")
    limiter.record_failure("victim@example.com")
    with pytest.raises(RateLimitedError):
        limiter.check("victim@example.com")

    limiter.check("someone-else@example.com")  # unaffected


def test_retry_after_seconds_reflects_remaining_lockout():
    clock = FakeClock(start=0.0)
    limiter = LoginRateLimiter(max_attempts=1, lockout_seconds=30, clock=clock)
    limiter.check("coach@example.com")
    limiter.record_failure("coach@example.com")
    clock.advance(10)

    with pytest.raises(RateLimitedError) as excinfo:
        limiter.check("coach@example.com")
    assert 19 <= excinfo.value.retry_after_seconds <= 20
