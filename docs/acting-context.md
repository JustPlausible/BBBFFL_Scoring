# Multi-role / acting-context model

**Roadmap:** Milestone B½ — Season Operations UI (issue #107).

**Implementation:** `bbbffl_app/app/auth.py` (`GRANTABLE_ROLES`,
`RoleGrantRepository`, `ActingContextService`, `coach_session.active_role`/
`represented_season_entry_id`), `bbbffl_app/app/authorization.py`
(`Role`, `Principal`, `resolve_principal`, `require_secretary_or_admin`,
`require_capability`, `require_entry_context`), `bbbffl_app/app/routes/context.py`
(the shared `/api/context*` switch API and `/api/admin/role-grants*`
administration). **Schema:** `bbbffl_app/migrations/versions/0022_acting_context.py`.

## Problem

One real, authenticated person may legitimately hold more than one BBBFFL
responsibility: a coach may also Score; a league officer may hold
Secretary/League Manager authority and, separately, Administrator
authority; the 2026 first-half replay needs one human operator to move
rapidly between Secretary/Scorer/Administrator/Replay Operator duties and
act on behalf of any of the ten replay season entries, without ten
separate logins. Before this package, `app.authorization.Principal.role`
was fixed for the life of a request: a coach session was always exactly
`Role.COACH`, and the only other roles (`scorer`/`admin`) came from the
legacy shared `X-Admin-Token`, which has no concept of a person at all.

## The three concepts, kept structurally separate

1. **Authenticated actor** -- the real signed-in person, `Principal.coach_id`/
   `display_name`, resolved by `app.auth.AuthenticationService`/`app.identity.
   IdentityRepository` exactly as roadmap package 19 (issue #74) already
   established. Nothing in this package changes how that identity is
   resolved, and no operation here ever writes `coach_session.coach_id`.
2. **Active authority role** -- `Principal.role`, one of `Role.COACH`,
   `Role.SCORER`, `Role.SECRETARY`, `Role.ADMIN`, `Role.REPLAY_OPERATOR`
   (or `Role.SPECTATOR` for an unauthenticated request). This is the
   authority the *current request* is exercising, computed fresh from
   `coach_session.active_role` on every call to `resolve_principal` --
   never assumed to still be authorised just because a session row names
   it.
3. **Represented season entry** -- `Principal.represented_season_entry_id`,
   optional, only meaningful for a delegated (non-"coach") active role: the
   team/coach context that role is currently acting on behalf of. "Coach"
   mode never uses this field -- it always resolves the signed-in person's
   *own* current season entry through the pre-existing `IdentityRepository.
   coach_owns_entry`/`coach_has_current_entry`, exactly as before this
   package.

Switching (2) or (3) never touches (1). There is no "log in as another
user": a delegated role's write is still attributed to the real operator
(see "Provenance" below), never to the represented coach.

## Role grants (`app.auth.RoleGrantRepository`, `role_grant` table)

"Coach" is never itself a grantable role -- it comes implicitly from
`season_entry_coach_history` (`IdentityRepository.coach_has_current_entry`).
`GRANTABLE_ROLES = {"scorer", "secretary", "admin", "replay_operator"}` are
each explicitly granted to a `coach_id` by an Administrator
(`POST /api/admin/role-grants`, strict `require_admin` -- granting/revoking
role authority is itself account/role administration, so it is never
available to a Secretary). A grant optionally carries a `season_id`:

- `season_id = NULL` -- the role applies across every season. The ordinary
  case for a standing Scorer/Secretary/Administrator.
- `season_id = <a specific season>` -- the role applies only to season
  entries within that one season. This is how a Replay Operator (or a
  Scorer/Secretary granted *only* for the 2026 replay) is confined to the
  ten 2026 replay entries and structurally cannot represent a live/current
  season's entry -- `ActingContextService.can_represent` checks the
  represented entry's own `season_id` against the grant's scope on every
  request, so this is enforced server-side, not by hiding a selector.

Grants are revocable (`revoked_at`, never deleted) and are re-checked on
every request: a grant revoked mid-session stops conferring authority on
the very next request, not merely the next login.

## Active-context resolution (`app.auth.ActingContextService`)

`ActingContextService` is the one reusable place this logic lives --
`app.authorization.resolve_principal` calls it on every request, and
`app.routes.context` calls it to validate and perform a switch. No other
module re-derives this logic; #101-#105 and the existing weekly coach/
scorer pages are meant to read `Principal.role`/`granted_roles`/
`represented_season_entry_id` the same way, never re-invent their own role
check.

- `available_roles(coach_id)` -- "coach" (if the person currently
  represents any season entry) plus every actively granted role, in any
  season scope. This is what a role *switcher* offers.
- `can_represent(coach_id, role, season_entry_id)` -- whether the active
  delegated role may represent a specific entry (used both to validate a
  switch and, in `app.authorization.require_entry_context`, to authorise a
  domain write).
- `representable_entries(coach_id, role, season_id)` -- the entries a
  delegated role may represent within one season (the represented-team
  selector's data source); empty, never an error, if the role does not
  cover that season at all.
- `resolve_active_role`/`resolve_represented_entry` -- the self-healing
  reads `resolve_principal` performs on every request: a stored role/entry
  no longer authorised silently falls back to "coach"/`None` rather than
  continuing to confer stale authority.
- `activate_role`/`set_represented_entry` -- the validated writes behind
  `POST /api/context/role`/`POST /api/context/represented-entry`. Both
  raise `UnauthorizedContextSwitchError` (mapped to HTTP 403 by
  `app.main`) for anything not currently authorised; switching role always
  clears any previously represented entry (a represented team chosen under
  one role must never silently carry into a different role's context).

## HTTP surface (`app.routes.context`)

- `GET /api/context` -- the current principal's context: active role,
  every granted role, the represented entry (if any, with its public team
  name -- never coach contact data), and `is_replay_context`. Any resolved
  principal may read this.
- `POST /api/context/role` `{role}` -- switch active role. Requires a real
  coach session (`principal.coach_id is not None`); the legacy shared
  `X-Admin-Token` has no session row to switch and is out of scope for this
  package. CSRF-protected (`app.csrf`, the same double-submit design every
  other coach-session-authenticated mutation in this codebase uses).
- `POST /api/context/represented-entry` `{season_entry_id}` (or `null` to
  clear) -- switch represented entry. Same coach-session/CSRF requirements.
- `GET /api/context/entries?season_id=` -- entries the *active* role may
  represent within a season (the represented-team selector's data).
- `GET/POST /api/admin/role-grants`, `POST /api/admin/role-grants/{id}/revoke`
  -- Administrator-only role-grant management.

Client-controlled input never confers authority by itself: a role name or
season-entry ID in a request body is only ever *validated* against
server-authoritative `role_grant`/`coach_session` state before anything is
written; an unauthorised request is rejected (403/404), never silently
narrowed or ignored.

## Capability-based checks

`app.authorization.CAPABILITIES`/`require_capability(name)` map each
`Role` to a closed set of capability strings (Administrator implicitly
carries every capability). `#101-#105` and later Season Operations UI
routes should express their requirement as
`Depends(require_capability("season.manage"))` rather than repeating raw
`principal.role == Role.SECRETARY` comparisons -- one place to extend the
permission matrix as those pages land, not a bespoke helper each would
need to replace later. `app.authorization.require_entry_context` is the
matching reusable "may this principal write to this season entry" check
(Coach-owns vs. delegated-represents), for any future route that mutates a
specific entry.

## Provenance / audit

Delegated *domain* writes (e.g. a future scorer/admin lineup-proxy route)
continue to use `app.audit.ActorContext.anonymous_operator(role=...)`
exactly as roadmap package 22 (issue #55) established -- never
`ActorContext.coach(...)`, and never the represented coach's identity; see
`app/lineup_proxy.py`'s "Actor, never the coach" and `app/audit.py`'s
"Actor convention". This package adds one thing: `actor_id` on that
`anonymous_operator` context may now carry the *authenticated operator's*
`coach_id` when they were resolved through a real session rather than the
legacy shared token (see `app.routes.season_centre`'s `_actor` helper for
the first concrete use). This is strictly additive provenance -- which
operator performed a delegated action -- never a new actor type and never
an impersonation path; the represented entry is still recorded exactly as
before (the write's own `entity_id`/`season_entry_id`).

Switching role or represented entry is itself audited
(`auth.context.role_activated`, `auth.context.represented_entry_set`,
`ActorContext.coach(coach_id)` -- always the authenticated person's own
action, never a proxy action), and granting/revoking a role is audited
too (`identity.role_grant.created`/`identity.role_grant.revoked`).

## Season Centre retrofit (issue #100 / PR #106)

`app.routes.season_centre` now requires `require_secretary_or_admin`
instead of strict Administrator authority on every endpoint: ordinary
league-season setup (season entries, team names, coach records) is
Secretary/League Manager's own operational authority, not something that
should require blanket Administrator authority (issue #107's explicit
requirement). Its page shell now issues the CSRF double-submit cookie/
token so its embedded context bar can call the CSRF-protected
`/api/context/*` endpoints, and its `api()` JS helper no longer sends an
empty `X-Admin-Token` header unconditionally -- previously that made the
page reachable *only* via the admin token, even for a signed-in Secretary/
Administrator coach session, because an empty header still took the
admin-token branch in `resolve_principal`. The page renders a persistent
context bar ("Acting as Secretary", a role switcher when more than one
role is granted, a separate represented-team selector for a delegated
role, and an unmistakable replay banner when the active role is Replay
Operator), plus an Administrator-only "Role grants" panel to grant/revoke
Scorer/Secretary/Administrator/Replay Operator authority for a coach
identity, optionally scoped to one season.

## 2026 replay acceptance path

One login covers the whole flow:

1. An Administrator grants the replay operator's coach identity Secretary,
   Scorer and Replay Operator authority (the latter two scoped to the 2026
   replay season) through the Season Centre's Role grants panel.
2. The operator signs in once (`app.auth`/`docs/coach-authentication.md`)
   and uses Secretary context (the session's default is still "coach", so
   they switch once via the context bar) for season/preseason setup.
3. They switch to Replay Operator (or Scorer) and use the represented-team
   selector to move rapidly among the ten replay entries -- each switch is
   a validated, audited `POST /api/context/represented-entry` call, never a
   raw client-trusted ID.
4. They switch back to Scorer for review/sign-off.

Throughout, `Principal.coach_id` (the authenticated operator) never
changes, and every delegated write still carries `actor_type=
"anonymous_operator"` with the represented entry from the write's own
domain fields -- never recorded as though the represented (historical)
coach authenticated and performed the action themselves.

## Deliberate scope boundaries

- No generic "log in as another user" impersonation exists anywhere in
  this package -- there is no operation that changes `coach_session.
  coach_id`, and `require_entry_context`/`can_represent` only ever
  authorise a delegated role to *represent*, never to acquire the coach's
  own login session.
- `#101-#105`'s actual workflows (player pool browser, draft board,
  preseason squads/transactions, fixture draw, round setup) are not built
  here -- this package is the shared framework those issues consume; see
  each issue's own scope.
- The legacy shared `X-Admin-Token` operator credential is unchanged and
  out of scope: it has no persistent per-person session to hold multiple
  roles or a represented entry, and remains the simple development/
  dev-ops credential it always was (see docs/authorization-and-privacy.md).
- This package's own new mutating routes (`app.routes.context`) verify the
  CSRF double-submit token on every coach-session-authenticated write.
  Existing admin-token-gated routers (`app.routes.admin`, `app.routes.
  draft`, `app.routes.preseason`, `app.routes.round_review`, and the parts
  of `app.routes.season_centre` predating this package) do not verify it,
  relying -- as they implicitly always have for any future cookie-reachable
  mutation -- on the session cookie's `SameSite=Lax` attribute (which
  modern browsers already refuse to attach to a cross-site POST/fetch) for
  CSRF defence. Now that a coach session can carry Scorer/Secretary/
  Administrator authority into those routers for the first time, retrofitting
  an explicit CSRF check into each of them is worthwhile follow-up, but it
  is pre-existing surface out of this PR's scope (see #107's "keep this PR
  focused" instruction); any #101-#105 route that accepts a coach-session
  principal should include the check from the start, following
  `app.routes.context`'s pattern.
