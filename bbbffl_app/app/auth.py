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

## Multi-role / acting context (roadmap package #107, issue #107)

Real people can hold more than one BBBFFL authority: a coach may also
Score; a league officer may hold Secretary/League Manager and
Administrator authority; a 2026 replay operator needs Secretary, Scorer,
Administrator and Replay Operator all at once. `RoleGrantRepository` records
which of `GRANTABLE_ROLES` a coach identity has been explicitly granted (on
top of the "Coach" authority `app.identity.IdentityRepository.
coach_has_current_entry` already gives them implicitly through
`season_entry_coach_history` -- "Coach" is therefore never itself a grantable
role). `ActingContextService` is the one reusable place that turns those
grants into "which role is this session currently acting as" and "which
season entry may it represent" -- see docs/acting-context.md. It is meant to
be reused exactly the way `app.authorization.resolve_principal` already
reuses `AuthenticationService`/`SessionRepository`, not re-derived per page.

Session-scoped active-context state (`coach_session.active_role`/
`represented_season_entry_id`, migration 0022) follows the exact same
server-authoritative design as the session itself: nothing about a client
request -- a role name, a team ID, a query parameter -- confers authority by
itself. Every resolution re-checks the stored value against `role_grant` (a
grant revoked mid-session can never keep conferring authority merely
because the session row still names it), and switching role/represented
entry never changes `coach_session.coach_id` -- the authenticated actor is
untouched by any of this, by construction: `ActingContextService` has no
operation that writes `coach_id` anywhere.
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
    CONTEXT_REPRESENTED_ENTRY_SET,
    CONTEXT_ROLE_ACTIVATED,
    ENTITY_TYPE_AUTH_ATTEMPT,
    ENTITY_TYPE_AUTH_SESSION,
    ENTITY_TYPE_ROLE_GRANT,
    ROLE_GRANT_CREATED,
    ROLE_GRANT_REVOKED,
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

# Additional authority roles a coach identity may be explicitly granted
# (roadmap package #107, issue #107) -- deliberately excludes "coach"
# (implicit through `IdentityRepository.coach_has_current_entry`/
# `season_entry_coach_history`, never granted) and "spectator" (the
# unauthenticated default, never a role anyone holds). Kept as plain
# strings independent of `app.authorization.Role` -- the same decoupling
# `app.lineup_proxy.PROXY_ACTOR_ROLES` already uses -- so this foundation-
# adjacent module never needs to import the HTTP-boundary module.
GRANTABLE_ROLES = frozenset({"scorer", "secretary", "admin", "replay_operator"})

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
    # Roadmap package #107 (issue #107): server-authoritative active-context
    # state, added by migration 0022. `active_role=None` means "Coach" (the
    # default every session had before this package, and still the default
    # for one that has never switched -- see module docstring). Neither
    # field is trusted at face value: `app.authorization.resolve_principal`
    # re-validates both against `role_grant`/`season_entry` on every
    # resolution via `app.auth.ActingContextService`.
    active_role: str | None = None
    represented_season_entry_id: str | None = None


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

    def verify_password(self, coach_id: str | None, password: str) -> bool:
        """`coach_id=None` covers "no coach resolved at all" (an unknown
        login identifier) as well as "a real coach with no credential row
        yet" -- both take the same dummy-hash decoy path below, so an
        unknown email and a known email with the wrong password cost the
        same amount of time. Callers must never skip this call for the
        unknown-coach case (see `AuthenticationService.login`) -- doing so
        would reopen exactly the timing side-channel this decoy exists to
        close."""
        row = (
            self.database.execute(
                "SELECT password_hash FROM coach_credential WHERE coach_id = ?", (coach_id,)
            ).fetchone()
            if coach_id is not None
            else None
        )
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
                     rotated_from_session_id, active_role, represented_season_entry_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
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
            "SELECT session_id, coach_id, created_at, expires_at, last_seen_at, revoked_at, rotated_from_session_id, "
            "active_role, represented_season_entry_id "
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
            conn.execute(
                "UPDATE coach_session SET last_seen_at = ? WHERE session_id = ?", (_now_iso(), session.session_id)
            )
        return session

    def revoke(
        self, session_id: str, *, actor: ActorContext, action: str = AUTH_LOGOUT, reason: str | None = None
    ) -> bool:
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

    def revoke_all_for_coach(self, coach_id: str, *, actor: ActorContext, reason: str | None = None) -> int:
        """Revoke every currently-valid session for `coach_id` -- used by
        `AuthenticationService.reset_password` so a session token issued
        (or stolen) before a credential reset cannot keep authenticating
        past it. Returns the number of sessions revoked; 0 if none were
        active, which is not an error."""
        with transaction(self.database) as conn:
            rows = conn.execute(
                "SELECT session_id FROM coach_session WHERE coach_id = ? AND revoked_at IS NULL"
                + _for_update_suffix(self.database),
                (coach_id,),
            ).fetchall()
            now = _now_iso()
            for row in rows:
                conn.execute("UPDATE coach_session SET revoked_at = ? WHERE session_id = ?", (now, row["session_id"]))
                append_event(
                    conn,
                    actor=actor,
                    action=AUTH_SESSION_REVOKED,
                    entity_type=ENTITY_TYPE_AUTH_SESSION,
                    entity_id=row["session_id"],
                    reason=reason,
                )
        return len(rows)

    def set_active_role(
        self, session_id: str, role: str | None, *, actor: ActorContext, reason: str | None = None
    ) -> None:
        """Set this session's active authority role (roadmap package #107).
        `role=None` means "Coach" -- the default. Switching role always
        clears any `represented_season_entry_id`: a represented team
        selected under one delegated role must never silently carry over
        into a different role's context. Callers must already have
        validated `role` against `ActingContextService.available_roles` --
        this method trusts its caller the same way `SessionRepository.
        create` trusts `AuthenticationService.login` to have already
        verified the password."""
        with transaction(self.database) as conn:
            conn.execute(
                "UPDATE coach_session SET active_role = ?, represented_season_entry_id = NULL WHERE session_id = ?",
                (role, session_id),
            )
            append_event(
                conn,
                actor=actor,
                action=CONTEXT_ROLE_ACTIVATED,
                entity_type=ENTITY_TYPE_AUTH_SESSION,
                entity_id=session_id,
                reason=reason,
                after_state={"active_role": role or "coach"},
            )

    def set_represented_entry(
        self, session_id: str, season_entry_id: str | None, *, actor: ActorContext, reason: str | None = None
    ) -> None:
        """Set (or clear, with `season_entry_id=None`) this session's
        represented season entry. Callers must already have validated the
        entry against `ActingContextService.can_represent` -- see
        `set_active_role`'s docstring for the same trust boundary."""
        with transaction(self.database) as conn:
            conn.execute(
                "UPDATE coach_session SET represented_season_entry_id = ? WHERE session_id = ?",
                (season_entry_id, session_id),
            )
            append_event(
                conn,
                actor=actor,
                action=CONTEXT_REPRESENTED_ENTRY_SET,
                entity_type=ENTITY_TYPE_AUTH_SESSION,
                entity_id=session_id,
                reason=reason,
                after_state={"represented_season_entry_id": season_entry_id},
            )


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
        # Always calls through to verify_password, even for an unknown
        # email (coach is None) -- CredentialRepository.verify_password's
        # dummy-hash decoy only closes the timing side-channel between
        # "unknown email" and "known email, wrong password" if this call
        # happens unconditionally on both paths.
        password_ok = self.credentials.verify_password(coach.coach_id if coach else None, password)
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

    def reset_password(
        self, coach_id: str, new_password: str, *, actor: ActorContext, reason: str | None = None
    ) -> Coach:
        """Admin-assisted password set/reset (see module docstring,
        "recovery/re-entry"). Raises `KeyError` if `coach_id` does not name
        a real coach -- callers get the same not-found behaviour as an
        unknown email lookup, rather than an uncaught foreign-key
        `IntegrityError` from the credential insert. Also revokes every
        currently-valid session for this coach: a session token issued
        before the reset (or one that leaked) must not keep authenticating
        past it."""
        coach = self.identities.get_coach(coach_id)
        if coach is None:
            raise KeyError(coach_id)
        self.credentials.set_password(coach_id, new_password, actor=actor, reason=reason)
        self.sessions.revoke_all_for_coach(coach_id, actor=actor, reason=reason or "credential_reset")
        return coach

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


@dataclass(frozen=True)
class RoleGrant:
    grant_id: str
    coach_id: str
    role: str
    season_id: str | None
    granted_at: str
    granted_by_actor_type: str
    granted_by_actor_id: str | None
    revoked_at: str | None
    reason: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class InvalidRoleError(ValueError):
    """`role` is not one of `GRANTABLE_ROLES`."""


class RoleGrantRepository:
    """Persistent record of the additional authority roles (`GRANTABLE_ROLES`)
    an authenticated coach identity holds, beyond the implicit "Coach"
    authority `IdentityRepository.coach_has_current_entry` already gives
    them (roadmap package #107, issue #107; migration 0022). A `season_id`
    scopes a grant to season entries within that one season -- this is how
    a Replay Operator grant is confined to the ten 2026 replay entries and
    can never represent a live/current-season entry (see
    `ActingContextService.can_represent`); `season_id=None` grants the role
    across every season, the ordinary case for Scorer/Secretary/Admin.

    Granting/revoking are both administrator-only operations at the HTTP
    boundary (see `app.routes.context`) -- this repository itself enforces
    only that `role` is one of `GRANTABLE_ROLES`, exactly like `app.identity.
    IdentityRepository` enforces domain invariants while its callers own
    who may invoke it."""

    def __init__(self, database: DatabaseConnection):
        self.database = database

    def grant(
        self,
        coach_id: str,
        role: str,
        *,
        actor: ActorContext,
        season_id: str | None = None,
        reason: str | None = None,
    ) -> RoleGrant:
        if role not in GRANTABLE_ROLES:
            raise InvalidRoleError(f"{role!r} is not a grantable role; must be one of {sorted(GRANTABLE_ROLES)}")
        now = _now_iso()
        item = RoleGrant(
            grant_id=_id(),
            coach_id=coach_id,
            role=role,
            season_id=season_id,
            granted_at=now,
            granted_by_actor_type=actor.actor_type,
            granted_by_actor_id=actor.actor_id,
            revoked_at=None,
            reason=reason,
        )
        with transaction(self.database) as conn:
            conn.execute(
                "INSERT INTO role_grant (grant_id, coach_id, role, season_id, granted_at, "
                "granted_by_actor_type, granted_by_actor_id, revoked_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.grant_id,
                    item.coach_id,
                    item.role,
                    item.season_id,
                    item.granted_at,
                    item.granted_by_actor_type,
                    item.granted_by_actor_id,
                    item.revoked_at,
                    item.reason,
                ),
            )
            append_event(
                conn,
                actor=actor,
                action=ROLE_GRANT_CREATED,
                entity_type=ENTITY_TYPE_ROLE_GRANT,
                entity_id=item.grant_id,
                reason=reason,
                after_state={"coach_id": coach_id, "role": role, "season_id": season_id},
            )
        return item

    def revoke(self, grant_id: str, *, actor: ActorContext, reason: str | None = None) -> bool:
        """Idempotent: revoking an unknown or already-revoked grant returns
        `False` without appending an event -- matching `SessionRepository.
        revoke`'s convention."""
        with transaction(self.database) as conn:
            row = conn.execute(
                "SELECT grant_id, coach_id, role, season_id FROM role_grant WHERE grant_id = ? AND revoked_at IS NULL"
                + _for_update_suffix(self.database),
                (grant_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE role_grant SET revoked_at = ? WHERE grant_id = ?", (_now_iso(), grant_id))
            append_event(
                conn,
                actor=actor,
                action=ROLE_GRANT_REVOKED,
                entity_type=ENTITY_TYPE_ROLE_GRANT,
                entity_id=grant_id,
                reason=reason,
                before_state={"coach_id": row["coach_id"], "role": row["role"], "season_id": row["season_id"]},
            )
        return True

    def list_active_for_coach(self, coach_id: str) -> list[RoleGrant]:
        rows = self.database.execute(
            "SELECT * FROM role_grant WHERE coach_id = ? AND revoked_at IS NULL ORDER BY granted_at", (coach_id,)
        ).fetchall()
        return [RoleGrant(**dict(row)) for row in rows]

    def list_all_for_coach(self, coach_id: str) -> list[RoleGrant]:
        """Every grant, including revoked ones -- the administrator-facing
        history view (`app.routes.context`), never used for authorization
        decisions (see `list_active_for_coach`/`is_role_granted`)."""
        rows = self.database.execute(
            "SELECT * FROM role_grant WHERE coach_id = ? ORDER BY granted_at", (coach_id,)
        ).fetchall()
        return [RoleGrant(**dict(row)) for row in rows]

    def is_role_granted(self, coach_id: str, role: str) -> bool:
        """Whether `role` is actively granted to this coach at all, in any
        season scope -- used to decide whether a role belongs in a
        session's *switcher* (see `ActingContextService.available_roles`).
        Does not itself authorise representing any particular entry; see
        `role_covers_season`."""
        return any(g.role == role for g in self.list_active_for_coach(coach_id))

    def role_covers_season(self, coach_id: str, role: str, season_id: str | None) -> bool:
        """Whether an active grant of `role` authorises acting within
        `season_id` -- a `NULL`-scoped grant covers every season; a
        season-scoped grant (the Replay Operator case) covers only that
        one. Used by `ActingContextService.can_represent`/
        `representable_entries` to decide which season entries a
        delegated role may represent."""
        return any(
            g.role == role and (g.season_id is None or g.season_id == season_id)
            for g in self.list_active_for_coach(coach_id)
        )


class UnauthorizedContextSwitchError(ValueError):
    """The requested active role or represented season entry is not
    authorised for this coach identity -- `app.main` maps this to HTTP 403
    (see `app.routes.context`), the same "authenticated but not permitted"
    outcome `app.authorization`'s other `require_*` checks return."""


class ActingContextService:
    """The one reusable active-context boundary roadmap package #107
    (issue #107) introduces over `RoleGrantRepository`/`SessionRepository`/
    `IdentityRepository`: which roles a signed-in coach identity may
    activate, and which season entry a delegated role may represent --
    kept entirely separate from *who* they are. `app.authorization.
    resolve_principal` calls this on every request to compute the
    authoritative `Principal`; `app.routes.context` calls it to validate
    and perform a context switch. No other module should re-derive this
    logic -- see this module's docstring and docs/acting-context.md.
    """

    def __init__(self, identities: IdentityRepository, role_grants: RoleGrantRepository, sessions: SessionRepository):
        self.identities = identities
        self.role_grants = role_grants
        self.sessions = sessions

    def available_roles(self, coach_id: str) -> frozenset[str]:
        """Every role this coach identity may currently activate: "coach"
        if they currently represent any season entry
        (`IdentityRepository.coach_has_current_entry`), plus every
        `GRANTABLE_ROLES` value with an active grant."""
        roles = {role for role in GRANTABLE_ROLES if self.role_grants.is_role_granted(coach_id, role)}
        if self.identities.coach_has_current_entry(coach_id):
            roles.add("coach")
        return frozenset(roles)

    def can_represent(self, coach_id: str, role: str, season_entry_id: str) -> bool:
        """Whether the currently-active delegated `role` may represent
        `season_entry_id`. "Coach" never represents another entry (it
        always resolves the coach's own, automatically -- see
        `resolve_represented_entry`); an unknown entry is never
        representable (fails closed, the same enumeration-safe posture
        `app.authorization.require_owned_season_entry` uses for private
        coach resources)."""
        if role == "coach" or role not in GRANTABLE_ROLES:
            return False
        entry = self.identities.get_public_team(season_entry_id)
        if entry is None:
            return False
        return self.role_grants.role_covers_season(coach_id, role, entry.season_id)

    def representable_entries(self, coach_id: str, role: str, season_id: str) -> list:
        """Every season entry `role` may represent within `season_id` --
        the represented-team selector's data source (`app.routes.context`).
        Empty (never an error) if the role does not cover this season at
        all, so a Replay Operator scoped to the 2026 replay season simply
        sees no entries when asked about 2027, rather than learning
        anything about who is in it."""
        if role == "coach" or role not in GRANTABLE_ROLES:
            return []
        if not self.role_grants.role_covers_season(coach_id, role, season_id):
            return []
        return self.identities.list_entries(season_id)

    def resolve_active_role(self, coach_id: str, stored_active_role: str | None) -> str:
        """Self-healing resolution used on every request: a stored role no
        longer actively granted (e.g. revoked mid-session) always falls
        back to `"coach"` -- the one role a coach session cannot lose --
        rather than continuing to confer authority the session row merely
        still *names*. `stored_active_role=None` (never switched) also
        resolves to `"coach"`, preserving this package's pre-existing
        default exactly."""
        role = stored_active_role or "coach"
        if role == "coach":
            return "coach"
        if self.role_grants.is_role_granted(coach_id, role):
            return role
        return "coach"

    def resolve_represented_entry(self, coach_id: str, active_role: str, stored_entry_id: str | None) -> str | None:
        """Companion to `resolve_active_role`: a represented entry is only
        ever trusted for a delegated (non-"coach") active role, and only
        while still authorised -- an entry chosen under a grant later
        revoked, or simply never chosen, resolves to `None` rather than
        silently granting stale access."""
        if active_role == "coach" or stored_entry_id is None:
            return None
        if self.can_represent(coach_id, active_role, stored_entry_id):
            return stored_entry_id
        return None

    def activate_role(
        self, *, coach_id: str, session_id: str, role: str, actor: ActorContext, reason: str | None = None
    ) -> None:
        """Switch `session_id`'s active role to `role` ("coach" or any
        `GRANTABLE_ROLES` value). Raises `UnauthorizedContextSwitchError`
        if `role` is unknown or not currently available to `coach_id` --
        never partially applies a rejected switch."""
        if role != "coach" and role not in GRANTABLE_ROLES:
            raise UnauthorizedContextSwitchError(f"unknown role {role!r}")
        if role not in self.available_roles(coach_id):
            raise UnauthorizedContextSwitchError(f"role {role!r} has not been granted to this coach")
        self.sessions.set_active_role(session_id, None if role == "coach" else role, actor=actor, reason=reason)

    def set_represented_entry(
        self,
        *,
        coach_id: str,
        session_id: str,
        active_role: str,
        season_entry_id: str | None,
        actor: ActorContext,
        reason: str | None = None,
    ) -> None:
        """Switch (or, with `season_entry_id=None`, clear) `session_id`'s
        represented season entry. Raises `UnauthorizedContextSwitchError`
        if `active_role` is "coach" (coach mode never represents another
        entry -- see `resolve_represented_entry`) or the role is not
        authorised to represent that entry."""
        if season_entry_id is not None and not self.can_represent(coach_id, active_role, season_entry_id):
            raise UnauthorizedContextSwitchError("the active role may not represent that season entry")
        if season_entry_id is not None and active_role == "coach":
            raise UnauthorizedContextSwitchError("coach mode does not support representing another season entry")
        self.sessions.set_represented_entry(session_id, season_entry_id, actor=actor, reason=reason)
