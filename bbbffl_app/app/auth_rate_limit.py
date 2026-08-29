"""Bounded login-attempt rate limiting (roadmap package 19, issue #74).

A small, deterministic, process-local counter -- not a distributed
rate-limiting subsystem. Zero third-party dependencies (see
`tests/test_architecture.py`'s FOUNDATION group).

## Process-local limitation

State lives only in this process's memory (a plain dict guarded by a
`threading.Lock`, since synchronous FastAPI handlers can run concurrently in
Starlette's thread pool -- see `app/db.py`'s `DatabaseConnection` docstring
for the same concern). It is **not** shared across multiple application
instances/workers and is reset on every process restart. For this
deployment's scale (roadmap package 19: ~10 coaches plus scorer/admin users,
a single home-server-style process) that is an accepted, documented
trade-off, not an oversight -- see docs/coach-authentication.md. A future
multi-instance deployment would need to move this state into the database
or a shared cache; nothing here assumes that never happens, but building it
now would be exactly the kind of "distributed enterprise rate-limiting
subsystem" issue #74 explicitly says is out of scope.

## Design

`app.auth.AuthenticationService` calls `check()` before verifying a
password/token and `record_failure()`/`record_success()` after, keyed by
*two* independent identifiers per attempt (the normalised login identifier,
e.g. lower-cased email, and the caller's remote address) so that neither an
attacker guessing many emails from one IP nor an attacker hammering one
known email from many IPs is unbounded. A successful login clears that key's
counter immediately, so a legitimate coach who mistypes a password a few
times is never penalised once they get it right. A lockout is time-bounded
(`lockout_seconds`), never permanent, per issue #74's "avoid permanently
locking legitimate users out" constraint.
"""

import threading
import time


class RateLimitedError(Exception):
    """Raised by `check()` when `key` is currently locked out."""

    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__(f"too many attempts; retry after {self.retry_after_seconds:.0f}s")


class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, lockout_seconds: float = 300.0, clock=time.monotonic):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (consecutive_failure_count, locked_until_monotonic_or_None)
        self._state: dict[str, tuple[int, float | None]] = {}

    def check(self, key: str) -> None:
        """Raise `RateLimitedError` if `key` is currently locked out.
        A lockout that has already expired is cleared as a side effect, so
        the very next attempt starts a fresh count rather than being
        permanently shadowed by a stale entry."""
        with self._lock:
            count, locked_until = self._state.get(key, (0, None))
            if locked_until is None:
                return
            now = self._clock()
            if now < locked_until:
                raise RateLimitedError(locked_until - now)
            self._state[key] = (0, None)

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, _locked_until = self._state.get(key, (0, None))
            count += 1
            locked_until = self._clock() + self._lockout_seconds if count >= self._max_attempts else None
            self._state[key] = (count, locked_until)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)
