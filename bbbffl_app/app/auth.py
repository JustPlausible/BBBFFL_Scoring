"""Coach authentication and server-side sessions (roadmap package 19,
issue #74).

## Mechanism: managed password credentials

See docs/coach-authentication.md for the full rationale. In short: managed
password credentials (this module), not emailed one-time links, because
this deployment (~10 coaches plus scorer/admin users, a single home-server
process) has no email-sending infrastructure today and issue #74 asks for
whichever option "creates the least operational complexity in the existing
application". Passwords are hashed with `app.password_hashing` (stdlib
`hashlib.scrypt`, no third-party dependency); recovery is an admin-assisted
reset (`CredentialRepository.set_password`, gated by the existing
`require_admin` dependency in `app/routes/admin.py`) rather than a
self-service emailed reset link, which would need the same email
infrastructure this package deliberately avoids.

## No second identity model

`AuthenticationService` resolves a login directly to the existing
persistent `coach` row (`app.identity.IdentityRepository`, roadmap package
10). `coach_credential`/`coach_session` (migration 0021) both key off
`coach.coach_id` -- never off email, display name, or any season-entry/team
identity -- so a coach with several historical season entries still has
exactly one login identity, and season-entry/team identity stays entirely
separate (see app/identity.py's module docstring).

## Sessions

`coach_session` rows are server-authoritative: the cookie a browser holds
is an opaque bearer token whose SHA-256 digest (`token_hash`) is the only
thing stored -- the raw token is never persisted, mirroring password
hashing. Every session has an explicit `expires_at` and a nullable
`revoked_at` (logout/revocation without deleting the row). Authentication
success **always** creates a brand-new session (`SessionRepository.create`)
-- there is no pre-authentication session row that gets "upgraded" in
place, so session fixation is structurally impossible, not just
discouraged. `AuthenticationService.login` additionally revokes any
existing valid session presented alongside a fresh login (e.g. a coach
re-authenticating in the same browser), so re-login always rotates to a new
identifier rather than extending the old one.

## Actor, never the coach for proxy actions

`ActorContext.coach(coach_id)` is used only for a coach's own authenticated
action (login, logout). A scorer/admin proxy action on a coach's behalf
continues to use `ActorContext.anonymous_operator(role=...)` exactly as
before (see `app/lineup_proxy.py`) -- this module changes nothing about
that boundary; see docs/coach-authentication.md, "Scorer/admin proxy
provenance is unchanged".
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.audit import (
    AUTH_LOGIN_FAILED,
    AUTH_LOGIN_SUCCEEDED,
    AUTH_LOGOUT,
    AUTH_SESSION_REVOKED,
    ENTITY_TYPE_AUTH_ATTEMPT,
    ENTITY_TYPE_AUTH_SESSION,
    ActorContext,
    append_event,
)
from app.auth_rate_limit import LoginRateLimiter
from app.db import DatabaseConnection, _for_update_suffix, transaction
from app.identity import Coach, IdentityRepository
from app.password_hashing import hash_password
from app.password_hashing import verify_password as _verify_password_hash

MIN_PASSWORD_LENGTH = 8
DEFAULT_SESSION_LIFETIME_SECONDS = 12 * 3600  # 12 hours

# A precomputed hash of a value nobody will ever submit, verified whenever
# no real credential exists to check against -- so "unknown email" and
# "known email, wrong password" take (close to) the same amount of time,
# per issue #74's "avoid leaking whether arbitrary coach identities exist
# where reasonably practical".
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def _id() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class InvalidCredentialsError(Exception):
    """Login failed: unknown identifier, wrong password, or no credential
    set for an otherwise-real coach. Deliberately one exception/one message
    for all three -- see module docstring."""


class WeakCredentialError(ValueError):
    """`set_password` was given a password shorter than `MIN_PASSWORD_LENGTH`."""


@dataclass(frozen=True)
class Session:
    session_id: str
    coach_id: str
    created_at: str
    expires_at: str
    last_seen_at: str
    revoked_at: str | None
    rotated_from_session_id: str | None


@dataclass(frozen=True)
class IssuedSession:
    """Returned only at the moment a session is created -- `token` is the
    raw bearer value to set as the cookie; it is never retrievable again
    once this call returns (only `token_hash` is persisted)."""

    session: Session
    token: str


class CredentialRepository:
    """Managed password credentials, one per coach (`coach_credential`)."""

    def __init__(self, database: DatabaseConnection):
        self.database = database

    def set_password(
        self,
        coach_id: str,
        password: str,
        *,
        actor: ActorContext,
        reason: str | None = None,
    ) -> None:
        """Create or reset the coach's password -- the admin-assisted
        recovery/re-entry path (see module docstring). Never logs or
        audits the password/hash itself, only that a change occurred."""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise WeakCredentialError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        password_hash = hash_password(password)
        now = _now_iso()
        with transaction(self.database) as conn:
            existing = conn.execute(
                "SELECT coach_id FROM coach_credential WHERE coach_id = ?" + _for_update_suffix(self.database),
                (coach_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE coach_credential SET password_hash = ?, updated_at = ? WHERE coach_id = ?",
                    (password_hash, now, coach_id),
                )
                change = "reset"
            else:
                conn.execute(
                    "INSERT INTO coach_credential (coach_id, password_hash, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (coach_id, password_hash, now, now),
                )
                change = "created"
            append_event(
                conn,
                actor=actor,
                action="auth.credential.changed",
                entity_type="auth.credential",
                entity_id=coach_id,
                reason=reason,
                after_state={"change": change},
            )

    def verify_password(self, coach_id: str, password: str) -> bool:
        row = self.database.execute(
            "SELECT password_hash FROM coach_credential WHERE coach_id = ?", (coach_id,)
        ).fetchone()
        if row is None:
            _verify_password_hash(password, _DUMMY_PASSWORD_HASH)  # constant-time-ish decoy, see module docstring
            return False
        return _verify_password_hash(password, row["password_hash"])


class SessionRepository:
    """Server-authoritative session state (`coach_session`)."""

    def __init__(self, database: DatabaseConnection, session_lifetime_seconds: int = DEFAULT_SESSION_LIFETIME_SECONDS):
        self.database = database
        self.session_lifetime_seconds = session_lifetime_seconds

    def create(
        self,
        coach_id: str,
        *,
        actor: ActorContext,
        action: str = AUTH_LOGIN_SUCCEEDED,
        reason: str | None = None,
        rotated_from_session_id: str | None = None,
        correlation_id: str | None = None,
    ) -> IssuedSession:
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        now = _now()
        expires_at = now + timedelta(seconds=self.session_lifetime_seconds)
        session = Session(
            session_id=_id(),
            coach_id=coach_id,
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            last_seen_at=now.isoformat(),
            revoked_at=None,
            rotated_from_session_id=rotated_from_session_id,
        )
        with transaction(self.database) as conn:
            conn.execute(
                """
                INSERT INTO coach_session
                    (session_id, token_hash, coach_id, created_at, expires_at, last_seen_at, revoked_at,
                     rotated_from_session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    token_hash,
                    session.coach_id,
                    session.created_at,
                    session.expires_at,
                    session.last_seen_at,
                    session.revoked_at,
                    session.rotated_from_session_id,
                ),
            )
            append_event(
                conn,
                actor=actor,
                action=action,
                entity_type=ENTITY_TYPE_AUTH_SESSION,
                entity_id=session.session_id,
                correlation_id=correlation_id,
                reason=reason,
                after_state={"coach_id": coach_id},
            )
        return IssuedSession(session=session, token=token)

    def get_valid(self, raw_token: str) -> Session | None:
        """The session for `raw_token`, or None if it does not exist, is
        revoked, or has expired. Touches `last_seen_at` as a side effect
        (bookkeeping only -- not audited, exactly like a domain read is
        never audited; see app/audit.py's module docstring)."""
        token_hash = _hash_token(raw_token)
        row = self.database.execute(
            "SELECT session_id, coach_id, created_at, expires_at, last_seen_at, revoked_at, rotated_from_session_id "
            "FROM coach_session WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        session = Session(**dict(row))
        if session.revoked_at is not None:
            return None
        if session.expires_at <= _now_iso():
            return None
        # A plain bookkeeping touch, deliberately outside transaction() and
        # never audited -- exactly like DecisionsRepository's ordinary
        # reads, this is not a materially state-changing mutation. Must
        # still go through an explicit begin()/commit (app.db.transaction),
        # since a bare DatabaseConnection.execute() write is rolled back
        # when its short-lived connection closes (see app/db.py's
        # DatabaseConnection docstring: it never holds a long-lived
        # connection, and SQLAlchemy 2.0 Core does not autocommit).
        with transaction(self.database) as conn:
            conn.execute("UPDATE coach_session SET last_seen_at = ? WHERE session_id = ?", (_now_iso(), session.session_id))
        return session

    def revoke(self, session_id: str, *, actor: ActorContext, action: str = AUTH_LOGOUT, reason: str | None = None) -> bool:
        """Revoke one session by its (non-secret) `session_id`. Returns
        False, without appending an event, if the session does not exist
        or is already revoked -- idempotent, so logging out twice or
        revoking an already-expired session is never an error."""
        with transaction(self.database) as conn:
            row = conn.execute(
                "SELECT session_id FROM coach_session WHERE session_id = ? AND revoked_at IS NULL"
                + _for_update_suffix(self.database),
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE coach_session SET revoked_at = ? WHERE session_id = ?", (_now_iso(), session_id))
            append_event(
                conn,
                actor=actor,
                action=action,
                entity_type=ENTITY_TYPE_AUTH_SESSION,
                entity_id=session_id,
                reason=reason,
            )
        return True

    def revoke_by_token(
        self, raw_token: str, *, actor: ActorContext, action: str = AUTH_LOGOUT, reason: str | None = None
    ) -> bool:
        session = self.get_valid(raw_token)
        if session is None:
            return False
        return self.revoke(session.session_id, actor=actor, action=action, reason=reason)


@dataclass(frozen=True)
class LoginResult:
    coach: Coach
    session: Session
    token: str


class AuthenticationService:
    """The application-service boundary `app/routes/auth.py` calls --
    orchestrates rate limiting, credential verification, session
    issuance/rotation and audit attribution. Never reached from any other
    route: `app/routes/admin.py`'s scorer/admin surface and its
    `require_admin`/`X-Admin-Token` gate are entirely untouched (see module
    docstring, "Actor, never the coach for proxy actions")."""

    def __init__(
        self,
        identities: IdentityRepository,
        credentials: CredentialRepository,
        sessions: SessionRepository,
        rate_limiter: LoginRateLimiter,
    ):
        self.identities = identities
        self.credentials = credentials
        self.sessions = sessions
        self.rate_limiter = rate_limiter

    def login(self, email: str, password: str, *, remote_addr: str, existing_token: str | None = None) -> LoginResult:
        """Authenticate `email`/`password`. Raises `InvalidCredentialsError`
        (generic -- never distinguishes "no such coach" from "wrong
        password") or `RateLimitedError` (from `app.auth_rate_limit`) on
        failure. On success, revokes `existing_token`'s session if it
        currently resolves to a valid one (rotation on re-login -- see
        module docstring) and always issues a brand-new session."""
        identifier_key = f"login:{email.strip().lower()}"
        ip_key = f"ip:{remote_addr}"
        self.rate_limiter.check(identifier_key)
        self.rate_limiter.check(ip_key)

        coach = self.identities.get_coach_by_email(email)
        password_ok = self.credentials.verify_password(coach.coach_id, password) if coach else False
        if not coach or not password_ok:
            self.rate_limiter.record_failure(identifier_key)
            self.rate_limiter.record_failure(ip_key)
            self._record_login_failure(coach.coach_id if coach else None)
            raise InvalidCredentialsError("invalid email or password")

        self.rate_limiter.record_success(identifier_key)
        self.rate_limiter.record_success(ip_key)

        rotated_from = None
        if existing_token:
            existing_session = self.sessions.get_valid(existing_token)
            if existing_session is not None:
                self.sessions.revoke(
                    existing_session.session_id,
                    actor=ActorContext.coach(coach.coach_id),
                    action=AUTH_SESSION_REVOKED,
                    reason="rotated_on_login",
                )
                rotated_from = existing_session.session_id

        issued = self.sessions.create(
            coach.coach_id, actor=ActorContext.coach(coach.coach_id), rotated_from_session_id=rotated_from
        )
        return LoginResult(coach=coach, session=issued.session, token=issued.token)

    def logout(self, raw_token: str) -> None:
        """Idempotent: logging out with no/invalid/already-revoked session
        does nothing (no audit event, no error) -- see `SessionRepository.revoke`."""
        session = self.sessions.get_valid(raw_token)
        if session is None:
            return
        self.sessions.revoke(session.session_id, actor=ActorContext.coach(session.coach_id))

    def resolve(self, raw_token: str | None) -> Coach | None:
        """The authenticated coach for a request's session cookie, or None
        if there isn't a valid one. The one request-context resolver every
        route that needs "who is signed in" should call, rather than
        parsing cookies or querying `coach_session` directly (see module
        docstring)."""
        if not raw_token:
            return None
        session = self.sessions.get_valid(raw_token)
        if session is None:
            return None
        return self.identities.get_coach(session.coach_id)

    def _record_login_failure(self, coach_id: str | None) -> None:
        with transaction(self.sessions.database) as conn:
            append_event(
                conn,
                actor=ActorContext.unauthenticated(coach_id),
                action=AUTH_LOGIN_FAILED,
                entity_type=ENTITY_TYPE_AUTH_ATTEMPT,
                entity_id=coach_id or "unknown",
                reason="invalid_credentials",
            )
