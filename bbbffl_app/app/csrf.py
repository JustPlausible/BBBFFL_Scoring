"""CSRF protection for coach-facing browser forms (roadmap package 19, issue
#74): a signed double-submit-cookie token.

Zero third-party dependencies (see `tests/test_architecture.py`'s
FOUNDATION group) -- `app.routes.auth` is the only caller, and it already
has the request/response objects; this module is pure token issue/verify
logic over strings it is handed.

## Design

Each form-rendering GET (the sign-in page, the authenticated account page)
calls `issue_token(secret)` once and both (a) sets the returned value as a
cookie and (b) renders it into the page's form as a hidden field. The
corresponding POST calls `verify_token(secret, cookie_value, submitted_value)`.

A cross-site attacker's page can trigger a request that automatically
carries the victim's cookies, but same-origin policy stops it reading the
cookie's *value* to also set as the form field -- so it cannot construct a
request where the submitted value matches. This is the standard
double-submit-cookie defence; it does not depend on session state, which is
why it also covers the pre-authentication login form (there is no session
yet to bind a synchroniser token to).

The token additionally carries an HMAC over a random nonce and an issue
timestamp, keyed by the deployment's `BBBFFL_SESSION_SECRET`
(`app.config.Settings.session_secret`): this bounds how long a leaked token
(e.g. via a referrer header or a log line) stays usable, and stops a party
who can merely *set* a same-named cookie (without knowing the secret) from
forging a token that also passes verification.
"""

import hmac
import secrets
import time
from hashlib import sha256

_MAX_AGE_SECONDS = 3600


def issue_token(secret: str, *, now: float | None = None) -> str:
    """A fresh token: set as both the CSRF cookie's value and a form's
    hidden field. `now` is injectable for deterministic tests."""
    nonce = secrets.token_urlsafe(24)
    issued_at = str(int(now if now is not None else time.time()))
    signature = _sign(secret, nonce, issued_at)
    return f"{nonce}.{issued_at}.{signature}"


def verify_token(secret: str, cookie_value: str | None, submitted_value: str | None, *, now: float | None = None) -> bool:
    """True only if a cookie value and a separately submitted form value
    were both present, identical, well-formed, correctly signed for
    `secret`, and issued within `_MAX_AGE_SECONDS`. Never raises -- any
    malformed input is just an invalid token."""
    if not cookie_value or not submitted_value:
        return False
    if not hmac.compare_digest(cookie_value, submitted_value):
        return False
    try:
        nonce, issued_at, signature = cookie_value.split(".")
        issued_at_int = int(issued_at)
    except ValueError:
        return False
    expected = _sign(secret, nonce, issued_at)
    if not hmac.compare_digest(expected, signature):
        return False
    current = now if now is not None else time.time()
    return 0 <= current - issued_at_int <= _MAX_AGE_SECONDS


def _sign(secret: str, nonce: str, issued_at: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"{nonce}.{issued_at}".encode("utf-8"), sha256).hexdigest()
