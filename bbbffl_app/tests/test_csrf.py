"""Coverage for app/csrf.py's signed double-submit-cookie token (roadmap
package 19, issue #74)."""

from app.csrf import issue_token, verify_token

SECRET = "test-secret-value"


def test_a_freshly_issued_token_verifies_against_itself():
    token = issue_token(SECRET, now=1000.0)
    assert verify_token(SECRET, cookie_value=token, submitted_value=token, now=1000.0) is True


def test_missing_cookie_or_missing_submitted_value_fails():
    token = issue_token(SECRET, now=1000.0)
    assert verify_token(SECRET, cookie_value=None, submitted_value=token, now=1000.0) is False
    assert verify_token(SECRET, cookie_value=token, submitted_value=None, now=1000.0) is False
    assert verify_token(SECRET, cookie_value="", submitted_value="", now=1000.0) is False


def test_cookie_and_submitted_value_must_match_exactly():
    """The double-submit half of the defence: an attacker's cross-site page
    can trigger the cookie being sent, but cannot itself read the cookie's
    value to also supply as the form field (same-origin policy) -- so a
    request where the two differ must never verify."""
    cookie_token = issue_token(SECRET, now=1000.0)
    other_token = issue_token(SECRET, now=1000.0)
    assert cookie_token != other_token
    assert verify_token(SECRET, cookie_value=cookie_token, submitted_value=other_token, now=1000.0) is False


def test_a_token_signed_with_a_different_secret_is_rejected():
    """A party who can merely set a same-named cookie (without knowing the
    server's secret) cannot forge a token that also passes verification --
    the signed half of the defence, on top of double-submit."""
    token = issue_token("a-different-secret", now=1000.0)
    assert verify_token(SECRET, cookie_value=token, submitted_value=token, now=1000.0) is False


def test_a_tampered_nonce_is_rejected():
    token = issue_token(SECRET, now=1000.0)
    nonce, issued_at, signature = token.split(".")
    tampered = f"{nonce}x.{issued_at}.{signature}"
    assert verify_token(SECRET, cookie_value=tampered, submitted_value=tampered, now=1000.0) is False


def test_an_expired_token_is_rejected():
    token = issue_token(SECRET, now=1000.0)
    # Just inside the window is fine; just past it is rejected.
    assert verify_token(SECRET, cookie_value=token, submitted_value=token, now=1000.0 + 3600) is True
    assert verify_token(SECRET, cookie_value=token, submitted_value=token, now=1000.0 + 3601) is False


def test_a_token_from_the_future_is_rejected():
    """Defends against a clock-skew/replay edge case where `issued_at`
    claims a time later than "now" -- such a token must never verify."""
    token = issue_token(SECRET, now=2000.0)
    assert verify_token(SECRET, cookie_value=token, submitted_value=token, now=1000.0) is False


def test_malformed_token_does_not_raise():
    assert (
        verify_token(SECRET, cookie_value="not.a.valid.token.shape", submitted_value="not.a.valid.token.shape") is False
    )
    assert verify_token(SECRET, cookie_value="short", submitted_value="short") is False
