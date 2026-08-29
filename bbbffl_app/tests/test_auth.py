"""Service-level coverage for app/auth.py (roadmap package 19, issue #74):
credential management, session issuance/rotation/expiry/revocation, and
authentication resolving to the existing persistent coach identity rather
than a second identity model.
"""

import pytest

from app.audit import AUTH_LOGIN_FAILED, AUTH_LOGIN_SUCCEEDED, AUTH_LOGOUT, ActorContext, AuditEventRepository
from app.auth import (
    AuthenticationService,
    CredentialRepository,
    InvalidCredentialsError,
    SessionRepository,
    WeakCredentialError,
)
from app.auth_rate_limit import LoginRateLimiter, RateLimitedError
from app.db import transaction
from app.identity import IdentityRepository
from tests.db_helpers import migrated_connection

PASSWORD = "correct horse battery staple"


@pytest.fixture
def conn():
    return migrated_connection()


@pytest.fixture
def identities(conn):
    return IdentityRepository(conn)


@pytest.fixture
def credentials(conn):
    return CredentialRepository(conn)


@pytest.fixture
def sessions(conn):
    return SessionRepository(conn, session_lifetime_seconds=3600)


@pytest.fixture
def rate_limiter():
    return LoginRateLimiter(max_attempts=5, lockout_seconds=300)


@pytest.fixture
def auth_service(identities, credentials, sessions, rate_limiter):
    return AuthenticationService(identities, credentials, sessions, rate_limiter)


@pytest.fixture
def coach(identities, credentials):
    created = identities.create_coach("Alex Coach", email="alex@example.com")
    credentials.set_password(created.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    return created


# -- 1. known coach authentication succeeds ---------------------------------


def test_known_coach_authentication_succeeds(auth_service, coach):
    result = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    assert result.coach.coach_id == coach.coach_id
    assert result.token


def test_login_is_case_insensitive_on_email(auth_service, coach):
    result = auth_service.login("ALEX@EXAMPLE.COM", PASSWORD, remote_addr="1.2.3.4")
    assert result.coach.coach_id == coach.coach_id


# -- 2. invalid credentials/token fail --------------------------------------


def test_wrong_password_fails(auth_service, coach):
    with pytest.raises(InvalidCredentialsError):
        auth_service.login("alex@example.com", "wrong password", remote_addr="1.2.3.4")


def test_unknown_email_fails_with_the_same_error_as_a_wrong_password(auth_service, coach):
    """See issue #74's "avoid leaking whether arbitrary coach identities
    exist" -- an unknown identifier and a known one with the wrong password
    must be indistinguishable to the caller."""
    with pytest.raises(InvalidCredentialsError):
        auth_service.login("nobody@example.com", "whatever", remote_addr="1.2.3.4")


def test_coach_with_no_credential_set_fails_login(auth_service, identities):
    identities.create_coach("No Password Yet", email="nopass@example.com")
    with pytest.raises(InvalidCredentialsError):
        auth_service.login("nopass@example.com", "anything", remote_addr="1.2.3.4")


def test_unknown_email_still_runs_the_dummy_password_check(auth_service, credentials, monkeypatch):
    """Regression for a timing side-channel: `AuthenticationService.login`
    must call `CredentialRepository.verify_password` unconditionally, even
    when no coach was resolved at all -- otherwise an unknown email returns
    near-instantly while a known email with the wrong password pays the
    full scrypt cost, making valid coach emails distinguishable by
    response time despite the identical error."""
    calls = []
    original = credentials.verify_password

    def spy(coach_id, password):
        calls.append(coach_id)
        return original(coach_id, password)

    monkeypatch.setattr(credentials, "verify_password", spy)

    with pytest.raises(InvalidCredentialsError):
        auth_service.login("nobody@example.com", "whatever", remote_addr="1.2.3.4")

    assert calls == [None]


def test_verify_password_with_no_coach_id_runs_the_dummy_check_and_fails(credentials):
    assert credentials.verify_password(None, "anything") is False


def test_set_password_rejects_a_too_short_password(credentials, coach):
    with pytest.raises(WeakCredentialError):
        credentials.set_password(coach.coach_id, "short", actor=ActorContext.anonymous_operator("admin"))


# -- 3. authentication resolves to the existing coach/person row -----------


def test_login_resolves_the_existing_coach_row_rather_than_creating_a_duplicate(auth_service, coach, conn):
    before_count = conn.execute("SELECT COUNT(*) AS n FROM coach").fetchone()["n"]

    result = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")

    after_count = conn.execute("SELECT COUNT(*) AS n FROM coach").fetchone()["n"]
    assert after_count == before_count
    assert result.coach.coach_id == coach.coach_id
    assert result.coach.display_name == coach.display_name


def test_resolve_returns_the_same_persistent_coach_identity(auth_service, coach):
    result = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    resolved = auth_service.resolve(result.token)
    assert resolved is not None
    assert resolved.coach_id == coach.coach_id


def test_resolve_returns_none_for_no_or_garbage_token(auth_service):
    assert auth_service.resolve(None) is None
    assert auth_service.resolve("") is None
    assert auth_service.resolve("not-a-real-token") is None


# -- 4. authenticated state survives normal navigation -----------------------


def test_resolve_succeeds_across_repeated_calls_with_the_same_token(auth_service, coach):
    """Simulates a browser navigating multiple pages with the same session
    cookie -- each request must independently re-resolve to the coach."""
    result = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    for _ in range(5):
        resolved = auth_service.resolve(result.token)
        assert resolved is not None
        assert resolved.coach_id == coach.coach_id


# -- 5. successful login rotates the session ---------------------------------


def test_login_always_issues_a_brand_new_session(auth_service, coach):
    first = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    second = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    assert first.session.session_id != second.session.session_id
    assert first.token != second.token


def test_reauthenticating_with_an_existing_session_revokes_it_and_rotates(auth_service, coach):
    first = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")

    second = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4", existing_token=first.token)

    assert second.session.session_id != first.session.session_id
    assert auth_service.resolve(first.token) is None  # old session cookie no longer works
    assert auth_service.resolve(second.token) is not None


# -- 6. logout invalidates/revokes the session -------------------------------


def test_logout_invalidates_the_session(auth_service, coach):
    result = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    auth_service.logout(result.token)
    assert auth_service.resolve(result.token) is None


def test_logout_is_idempotent(auth_service, coach):
    result = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    auth_service.logout(result.token)
    auth_service.logout(result.token)  # must not raise


def test_logout_with_no_session_does_nothing(auth_service):
    auth_service.logout("never-issued-token")  # must not raise


# -- 7. expired sessions are rejected ----------------------------------------


def test_expired_session_is_rejected(auth_service, sessions, coach, conn):
    result = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    # Force the session into the past directly -- SessionRepository has no
    # "expire now" method by design (expiry is time-based, not an action).
    with transaction(conn) as tx:
        tx.execute(
            "UPDATE coach_session SET expires_at = '2000-01-01T00:00:00+00:00' WHERE session_id = ?",
            (result.session.session_id,),
        )
    assert auth_service.resolve(result.token) is None


# -- 8. revoked sessions are rejected -----------------------------------------


def test_revoked_session_is_rejected(auth_service, sessions, coach):
    result = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    sessions.revoke(result.session.session_id, actor=ActorContext.coach(coach.coach_id))
    assert auth_service.resolve(result.token) is None


def test_revoking_an_already_revoked_session_is_a_no_op(sessions, coach):
    from app.audit import ActorContext as AC

    issued = sessions.create(coach.coach_id, actor=AC.coach(coach.coach_id))
    assert sessions.revoke(issued.session.session_id, actor=AC.coach(coach.coach_id)) is True
    assert sessions.revoke(issued.session.session_id, actor=AC.coach(coach.coach_id)) is False


def test_revoke_all_for_coach_revokes_every_active_session(sessions, coach):
    first = sessions.create(coach.coach_id, actor=ActorContext.coach(coach.coach_id))
    second = sessions.create(coach.coach_id, actor=ActorContext.coach(coach.coach_id))

    revoked_count = sessions.revoke_all_for_coach(coach.coach_id, actor=ActorContext.anonymous_operator("admin"))

    assert revoked_count == 2
    assert sessions.get_valid(first.token) is None
    assert sessions.get_valid(second.token) is None


def test_revoke_all_for_coach_does_not_touch_another_coachs_sessions(identities, credentials, sessions):
    coach_a = identities.create_coach("Coach A", email="a2@example.com")
    coach_b = identities.create_coach("Coach B", email="b2@example.com")
    issued_a = sessions.create(coach_a.coach_id, actor=ActorContext.coach(coach_a.coach_id))
    issued_b = sessions.create(coach_b.coach_id, actor=ActorContext.coach(coach_b.coach_id))

    sessions.revoke_all_for_coach(coach_a.coach_id, actor=ActorContext.anonymous_operator("admin"))

    assert sessions.get_valid(issued_a.token) is None
    assert sessions.get_valid(issued_b.token) is not None


def test_reset_password_revokes_existing_sessions_and_new_password_works(auth_service, sessions, coach):
    """Admin-assisted recovery path: resetting a password must invalidate
    any session issued before the reset, not just change future logins."""
    first_login = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")

    auth_service.reset_password(
        coach.coach_id, "brand-new-password-456", actor=ActorContext.anonymous_operator("admin")
    )

    assert auth_service.resolve(first_login.token) is None
    second_login = auth_service.login("alex@example.com", "brand-new-password-456", remote_addr="1.2.3.4")
    assert second_login.coach.coach_id == coach.coach_id


def test_reset_password_for_unknown_coach_id_raises_key_error(auth_service):
    with pytest.raises(KeyError):
        auth_service.reset_password(
            "does-not-exist", "brand-new-password-456", actor=ActorContext.anonymous_operator("admin")
        )


# -- 10. rate limiting bounds repeated failed authentication attempts -------


def test_repeated_failed_attempts_trigger_rate_limiting(identities, credentials, sessions):
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=300)
    service = AuthenticationService(identities, credentials, sessions, limiter)
    identities.create_coach("Someone", email="rl@example.com")

    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            service.login("rl@example.com", "wrong", remote_addr="9.9.9.9")

    with pytest.raises(RateLimitedError):
        service.login("rl@example.com", "wrong", remote_addr="9.9.9.9")


def test_rate_limiting_does_not_block_a_different_email_from_the_same_ip(identities, credentials, sessions):
    limiter = LoginRateLimiter(max_attempts=1, lockout_seconds=300)
    service = AuthenticationService(identities, credentials, sessions, limiter)
    identities.create_coach("Victim", email="victim@example.com")
    identities.create_coach("Neighbour", email="neighbour@example.com")
    credentials.set_password(
        identities.get_coach_by_email("neighbour@example.com").coach_id,
        PASSWORD,
        actor=ActorContext.anonymous_operator("admin"),
    )

    with pytest.raises(InvalidCredentialsError):
        service.login("victim@example.com", "wrong", remote_addr="7.7.7.7")
    # The neighbour's own login, from a *different* IP, must still work --
    # only "victim@example.com" (and 7.7.7.7) were penalised.
    result = service.login("neighbour@example.com", PASSWORD, remote_addr="8.8.8.8")
    assert result.coach.display_name == "Neighbour"


# -- 11. one coach cannot inherit/use another coach's session ---------------


def test_one_coachs_session_never_resolves_to_another_coach(identities, credentials, sessions, rate_limiter):
    service = AuthenticationService(identities, credentials, sessions, rate_limiter)
    coach_a = identities.create_coach("Coach A", email="a@example.com")
    coach_b = identities.create_coach("Coach B", email="b@example.com")
    credentials.set_password(coach_a.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    credentials.set_password(coach_b.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))

    result_a = service.login("a@example.com", PASSWORD, remote_addr="1.1.1.1")
    result_b = service.login("b@example.com", PASSWORD, remote_addr="2.2.2.2")

    assert result_a.token != result_b.token
    resolved_a = service.resolve(result_a.token)
    resolved_b = service.resolve(result_b.token)
    assert resolved_a.coach_id == coach_a.coach_id
    assert resolved_b.coach_id == coach_b.coach_id
    assert resolved_a.coach_id != resolved_b.coach_id


# -- audit trail: success/failure/logout are attributable and never carry --
# -- secret material ---------------------------------------------------------


def test_login_success_and_failure_and_logout_are_all_audited_without_secrets(auth_service, coach, conn):
    auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    with pytest.raises(InvalidCredentialsError):
        auth_service.login("alex@example.com", "wrong-one", remote_addr="1.2.3.4")
    result = auth_service.login("alex@example.com", PASSWORD, remote_addr="1.2.3.4")
    auth_service.logout(result.token)

    events = AuditEventRepository(conn)
    actions = [e.action for e in events.list_events()]
    assert AUTH_LOGIN_SUCCEEDED in actions
    assert AUTH_LOGIN_FAILED in actions
    assert AUTH_LOGOUT in actions

    for event in events.list_events():
        blob = str(event.before_state) + str(event.after_state) + str(event.payload) + str(event.reason)
        assert PASSWORD not in blob
        assert "wrong-one" not in blob
        assert result.token not in blob
