# Coach authentication and sessions

**Roadmap:** implements work package **19** from
[`roadmap/2027-season-roadmap.md`](roadmap/2027-season-roadmap.md) (issue #74).

**Implementation:** `bbbffl_app/app/auth.py` (credentials, sessions,
`AuthenticationService`), `bbbffl_app/app/password_hashing.py` (scrypt
hashing), `bbbffl_app/app/csrf.py` (CSRF tokens), `bbbffl_app/app/auth_rate_limit.py`
(login rate limiting), `bbbffl_app/app/routes/auth.py` (browser routes).
**Schema:** `bbbffl_app/migrations/versions/0021_coach_authentication.py`.

## Goal and scope

Provide the smallest production-suitable browser authentication and
server-side session layer for the ~10 BBBFFL coaches plus scorer/admin
users, resolving directly to the persistent coach identity roadmap package
10 (issue #20) already introduced -- **not** a second coach/user model.
This package is deliberately narrow: a sign-in page, sign-in submission,
sign-out, and a landing page confirming which coach is authenticated. It
does **not** build the coach dashboard, weekly selection UI, or the full
role/permission matrix -- those are roadmap packages 25 and 20
respectively.

## Authentication mechanism: managed passwords

Issue #74 asks for whichever of two options creates the least operational
complexity: managed password credentials, or emailed one-time links.
**This package chose managed password credentials.**

Rationale:

- The deployment has no email-sending infrastructure today (no SMTP
  relay/provider configured anywhere in this codebase or its
  configuration boundary). An emailed one-time-link flow would need to
  introduce and operate that infrastructure -- exactly the "unnecessary
  infrastructure for this package" issue #74 says to avoid, and the
  isolation issue #74 suggests (a delivery interface with a real and a
  test/no-op provider) only defers that cost, it doesn't remove it.
- Password hashing needs nothing beyond the Python standard library
  (`hashlib.scrypt`, see `app/password_hashing.py`) -- zero new runtime
  dependencies, consistent with this application's existing "typed
  settings boundary, no secrets-management product" posture (issue #38).
- Recovery, the one place a one-time-link flow would have an advantage
  (self-service reset via email), is handled here by an **admin-assisted
  reset** instead: `POST /api/admin/coach-credential`, gated by the
  existing `X-Admin-Token` scorer/admin surface (`app/routes/admin.py`).
  For ~10 coaches on a single home-server deployment, an admin resetting a
  password on request is simpler to operate than standing up transactional
  email, and needs no new infrastructure at all.

Neither OAuth nor an enterprise IAM product was considered further: issue
#74 explicitly asks for one of the two simple mechanisms above unless there
is a clear technical reason they can't satisfy the requirements, and
managed passwords satisfy every acceptance criterion without one.

## No second identity model

`AuthenticationService` resolves a login **directly** to the existing
persistent `coach` row (`app/identity.py`, roadmap package 10):
`coach_credential.coach_id` and `coach_session.coach_id` are both foreign
keys onto `coach.coach_id` -- never onto email, display name, or any
season-entry/team identity. A coach with several historical season entries
(migration 0005's `season_entry`/`season_entry_coach_history`) still has
exactly one login identity; login identity and season-entry/team identity
remain entirely separate concepts, and login is never based on the
mutable, season-scoped team name (`season_entry_team_name_history`).

`app.identity.IdentityRepository.get_coach_by_email` (case-insensitive) is
the one lookup this package added to resolve a submitted email to a coach;
`migrations/versions/0021_coach_authentication.py` adds the case-insensitive
uniqueness constraint that lookup depends on (`coach.email` may still be
unset for a coach with no login access -- that constraint is a *partial*
index, `WHERE email IS NOT NULL`).

## Schema changes (migration 0021)

Two new tables, both referencing `coach.coach_id`:

- **`coach_credential`** -- one managed password hash per coach
  (`coach_id` is the primary key, so a coach has at most one active
  credential). `password_hash` is produced by `app.password_hashing.hash_password`
  (`hashlib.scrypt`, a fresh random salt per password, cost parameters
  encoded alongside the hash so a future parameter increase needs no
  migration). Never plaintext, never reversible encryption.
- **`coach_session`** -- server-authoritative session state. The bearer
  token a browser holds in its cookie is never stored: `token_hash` is a
  SHA-256 digest of it (the same defence-in-depth reasoning as hashing
  passwords), so reading the database cannot produce a usable session
  token. `session_id` is a separate, non-secret identifier safe to use in
  audit events/logs. `expires_at` bounds lifetime; `revoked_at` supports
  logout/revocation without deleting the row.

Plus a raw partial expression index, `ix_coach_email_ci`, giving
`coach.email` case-insensitive uniqueness where set.

## Session design

- **Server-authoritative:** every request re-resolves the session by
  looking up `coach_session` (via the hashed bearer token) -- there is no
  privileged state trusted from the cookie alone beyond that opaque token.
- **Explicit expiry:** `expires_at`, bounded by `BBBFFL_SESSION_LIFETIME_SECONDS`
  (default 12 hours; see [`settings.md`](settings.md)).
- **Logout/revocation:** `SessionRepository.revoke` sets `revoked_at`;
  `AuthenticationService.logout` is idempotent (logging out twice, or an
  already-expired/revoked session, is not an error).
- **Rotation on authentication success:** `AuthenticationService.login`
  **always** creates a brand-new session (`SessionRepository.create`) --
  there is no pre-authentication session row that gets "upgraded" in
  place, so session fixation is structurally impossible rather than merely
  discouraged. If the browser presents an existing valid session cookie
  alongside a fresh login (e.g. a coach re-authenticating in the same
  browser), that old session is revoked and a new one issued -- re-login
  always rotates to a new identifier.
- **Ownership boundary:** a session resolves to exactly the coach it was
  issued for; nothing here derives one coach's session from another's.

## Cookies

Two cookies, both `HttpOnly`, `SameSite=Lax`, and `Secure` exactly when
`settings.is_production` (i.e. `BBBFFL_ENVIRONMENT=production` -- see
[`settings.md`](settings.md)) -- so local/development HTTP access keeps
working without configuration, while a production HTTPS deployment gets
`Secure` automatically, not via a separate flag to remember to set:

- `bbbffl_session` -- the opaque bearer token, `Max-Age` matching
  `BBBFFL_SESSION_LIFETIME_SECONDS`.
- `bbbffl_csrf` -- the CSRF double-submit cookie (below), `Max-Age` 3600s,
  reissued on every form-rendering GET.

## CSRF and session fixation

`app/csrf.py` implements a **signed double-submit-cookie** token: each
form-rendering GET (`GET /login`, `GET /account`) issues a fresh token,
sets it as the `bbbffl_csrf` cookie, and renders the same value into the
page's form as a hidden field. The corresponding POST (`/login`,
`/logout`) verifies that the cookie value and the submitted field are
present, identical, and carry a valid HMAC signature (keyed by
`BBBFFL_SESSION_SECRET`) issued within the last hour.

This defends state-changing submissions even before a session exists (the
login form itself): a cross-site attacker's page can trigger the cookie
being sent automatically, but same-origin policy stops it *reading* the
cookie's value to also supply as the hidden field, so it cannot construct
a matching request. The HMAC signature additionally stops a party who can
merely set a same-named cookie (without the server's secret) from forging
a token that also verifies, and bounds how long a leaked token (e.g. via a
referrer header or a log line) stays usable.

Session fixation is covered by the session-rotation design above (a
pre-authentication session/token is never reused as the authenticated
one), and is exercised directly by
`tests/test_auth.py::test_reauthenticating_with_an_existing_session_revokes_it_and_rotates`.
CSRF rejection is exercised by `tests/test_auth_api.py`'s
`test_login_without_a_csrf_token_is_rejected`,
`test_login_with_a_csrf_token_not_matching_the_cookie_is_rejected`,
`test_login_with_no_csrf_cookie_at_all_is_rejected` and
`test_logout_without_a_valid_csrf_token_does_not_revoke_the_session` --
i.e. tested explicitly rather than assumed from framework defaults (there
is no framework-level CSRF protection here: FastAPI/Starlette provide
none out of the box).

## Rate limiting / abuse controls

`app/auth_rate_limit.py`'s `LoginRateLimiter` is a small, deterministic,
**process-local** in-memory counter -- not a distributed rate-limiting
subsystem, which issue #74 explicitly says is unnecessary at this scale.
`AuthenticationService.login` checks and updates it keyed by **both** the
normalised login identifier (lower-cased email) and the caller's remote
address, so neither "guess many emails from one IP" nor "hammer one known
email from many IPs" is unbounded. A lockout is time-bounded (default: 5
consecutive failures locks that key out for 5 minutes) -- never permanent
-- and a successful login clears the counter immediately, so a coach who
mistypes a password a few times is never penalised once they get it
right. An unknown email and a known email with the wrong password produce
the *same* error, consume the *same* rate-limit bucket shape, and cost the
*same* amount of time to reject: `AuthenticationService.login` always
calls `CredentialRepository.verify_password` -- for an unresolved coach it
still runs the full `scrypt` verification against a fixed dummy hash
(`app/auth.py`'s `_DUMMY_PASSWORD_HASH`) rather than short-circuiting, so
the expensive path isn't skipped only when no coach was found. Together
this means repeated attempts cannot be used to enumerate which emails
belong to real coaches, whether by response content, rate-limit behaviour,
or response time.

**Limitation:** because the limiter's state lives only in this process's
memory, it is not shared across multiple application instances/workers and
resets on every process restart. This is an accepted, documented trade-off
for this deployment's scale (a single home-server-style process); a future
multi-instance deployment would need to move this state into the database
or a shared cache.

## Recovery / re-entry

`POST /api/admin/coach-credential` (gated by the existing `X-Admin-Token`
`require_admin` dependency in `app/routes/admin.py`) lets an admin set or
reset a coach's password, identified by either `coach_id` or `email`. This
is the practical recovery path for a coach who has forgotten their
password: contact the league admin, who resets it through this endpoint
(or a short script/`curl` invocation using it) and relays the new password
out of band. It requires no email infrastructure and reuses an access
control surface that already exists.

The route delegates to `AuthenticationService.reset_password`, which does
two things atomically from the caller's perspective: sets the new password
hash, then revokes every currently-valid session for that coach
(`SessionRepository.revoke_all_for_coach`). This matters for the case the
reset exists to cover -- a suspected compromised credential or device --
where leaving old sessions alive would let a stolen cookie keep
authenticating for up to `BBBFFL_SESSION_LIFETIME_SECONDS` regardless of
the reset. A `coach_id` that does not name a real coach raises `KeyError`
(mapped to HTTP 404 by `app/main.py`'s existing handler) before any write
is attempted, rather than surfacing as an uncaught foreign-key violation.

## Scorer/admin proxy provenance is unchanged

This package changes nothing about `app/routes/admin.py`'s shared
`X-Admin-Token` surface or `app/lineup_proxy.py`'s scorer/admin proxy
actions:

- `require_admin` still gates every `/api/admin/*` endpoint on the admin
  token alone -- a coach's own session grants **no** admin access, and the
  admin token remains a wholly separate authority context from coach
  login (never "the permanent coach authentication mechanism").
- Proxy lineup operations (`app.lineup_proxy.LineupProxyService`) still
  attribute every write to `ActorContext.anonymous_operator(role="scorer"|"admin")`
  -- **never** to the receiving coach, and never to the new `coach` actor
  type this package introduces. `ActorContext.coach(coach_id)` is used
  *only* for a coach's own authenticated action (login, logout); nothing
  in this package touches `app/lineup_proxy.py`'s `_ensure_operator` check.
- `tests/test_auth_api.py::test_admin_dnp_action_is_still_attributed_to_the_anonymous_operator_not_a_coach`
  and `test_admin_routes_are_unaffected_by_coach_authentication` exercise
  this directly.

## Deterministic replay is independent of coach authentication

Nothing in the replay harness (`app/replay.py`, `app/replay_checkpoint.py`,
`scripts/replay_2026_*.py`) imports or depends on `app.auth` --
`tests/test_architecture.py`'s import-graph tests would fail if it did
(`REPLAY` is forbidden from depending on the Grand Final vertical, routes,
or the composition root, and `app.auth` is never on that path). The 2026
replay continues to use scorer/proxy operations without authenticating ten
historical coaches, exactly as before this package.

## Actor/audit conventions

Two new `app.audit.ActorContext` actor types (see
[`audit-events.md`](audit-events.md)):

- `coach` -- a real, authenticated coach acting as themselves (login,
  logout). `actor_id` is the `coach_id`.
- `unauthenticated` -- a login attempt with no verified identity (used
  only for the `auth.login.failed` event). `actor_id`, if the attempted
  identifier resolved to a real coach, is that `coach_id` -- the submitted
  email/password is never recorded.

New action names: `auth.login.succeeded`, `auth.login.failed`,
`auth.session.logout`, `auth.session.revoked`. No audit payload here ever
contains a password, password hash, session secret, or session token.

## Privacy and logging

- Passwords, password hashes, session bearer tokens, and the
  `BBBFFL_SESSION_SECRET`/CSRF signatures are never logged and never
  appear in an audit payload (only `coach_id`, an action name, and a
  failure category do).
- `GET /account` (the landing/confirmation page) shows only the coach's
  `display_name` and `coach_id` -- never email/phone, keeping the private
  contact fields introduced by roadmap package 10 out of even this
  authenticated-only page.
- The public scoreboard (`GET /`) gains only a tiny "Coach sign in" /
  "Signed in as `<name>`" nav link (`app/routes/public.py`); no private
  data is added to that public response.
- Every credential-changing/session-changing request is a `POST`; no
  password, email, or session token is ever placed in a URL.

## Tests

- `tests/test_password_hashing.py` -- hashing/verification correctness,
  salting, malformed-hash handling.
- `tests/test_csrf.py` -- token issue/verify, double-submit mismatch,
  wrong secret, tampering, expiry.
- `tests/test_auth_rate_limit.py` -- bounded lockout, time-bounded (never
  permanent), independent keys, reset on success.
- `tests/test_auth.py` -- service-level: known-coach login success,
  invalid-credential failure (unknown email and wrong password
  indistinguishable), resolution to the *existing* coach row (no
  duplicate created), session survives repeated resolution, login always
  rotates the session, logout invalidates/is idempotent, expired and
  revoked sessions are rejected, rate limiting, one coach's session never
  resolves to another coach, and that the audit trail records success/
  failure/logout without ever containing secret material.
- `tests/test_auth_api.py` -- full HTTP sign-in/sign-out flow, CSRF
  rejection (missing token, mismatched token, no CSRF cookie at all,
  logout without a valid token), rate limiting via HTTP, cookie security
  flags in development vs. simulated production, no credential/token
  leakage into responses or redirect URLs, session ownership, navigation
  state, admin-assisted credential reset, and that scorer/admin proxy
  provenance is unaffected.
- `tests/test_audit.py` -- the `coach`/`unauthenticated` actor types are
  accepted and distinguishable; every other unrecognised actor type is
  still refused.
- `tests/test_config.py` -- production refuses a missing
  `BBBFFL_SESSION_SECRET` and refuses the development placeholder value.
- `tests/test_architecture.py` -- `app.auth` (plus its `app.password_hashing`/
  `app.csrf`/`app.auth_rate_limit` foundation leaves) sits in its own
  dependency-graph group with the same shape as `app.round_review`: it may
  depend on `app.identity`, but the season model/lockouts/weekly-submission
  sources may never depend back on it, and it must never depend on the
  Grand Final vertical, routes, or the composition root.

The existing full API, scoring, replay, audit and architecture suites all
remain green -- see this file's "Scorer/admin proxy provenance is
unchanged" and "Deterministic replay is independent of coach
authentication" sections above for what specifically was re-verified.
