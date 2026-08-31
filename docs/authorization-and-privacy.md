# Authorization and privacy boundary

Authentication and authorization are separate.  The session mechanism and
persistent coach identity are described in [coach authentication](coach-authentication.md).
`app.authorization` is the single HTTP policy boundary that decides what that
identity (or an operator credential) may do.

## Principals and capabilities

Every request resolves to exactly one typed principal: anonymous spectator,
coach, scorer, secretary/league manager, administrator, or replay operator
(`app.authorization.Role`).  The legacy `X-Admin-Token` remains an
administrator credential by default for compatibility.  Supplying
`X-Authority-Role: scorer` explicitly narrows that credential to scorer
capabilities.  It never creates a coach identity.

Ordinary browser operation is session-native: a signed-in operator uses the
session cookie and assigned active role without entering the shared token.
`BBBFFL_ADMIN_TOKEN` is an explicit bootstrap/emergency compatibility path,
not the normal long-term administration workflow. Browser clients only send
`X-Admin-Token` after an operator deliberately supplies a non-empty value;
an explicitly supplied invalid value fails authentication and never falls
back to a simultaneously valid session.

A coach session's active role is no longer fixed at "coach": roadmap
package #107 (issue #107, see [acting-context](acting-context.md)) lets a
signed-in coach identity hold additional granted roles (Scorer, Secretary/
League Manager, Administrator, Replay Operator) and switch which one is
active without re-authenticating.  A coach session *never* acquires the
legacy `X-Admin-Token`'s ambient authority this way, and switching active
role never changes which coach identity is authenticated
(`Principal.coach_id` is untouched by any context switch) -- see
[acting-context](acting-context.md) for the full model, including
represented-season-entry delegation and provenance.

When the token is unset in the supported development/test configuration, the
historic open operator mode is preserved as an explicit administrator
principal. Production configuration already refuses to start without a token.

Scorers may use delegated scoring, review, sign-off, and proxy domain
operations. Replay Operators may use season-authorised Round Centre review
operations, but do not satisfy the legacy/global scorer dependency used by
Grand Final and SuperScore mutations. Administrative configuration, credential recovery, audit access,
corrections, and administrative pages require administrator authority where
the existing API distinguishes them.  Operator actions continue to pass their
real scorer/admin `ActorContext` into domain services; they do not impersonate
a coach.

## Coach ownership

Private season-specific routes resolve the session to the persistent `coach`
row and query the current `season_entry_coach_history` assignment.  They never
compare a coach ID with an entry ID and never infer ownership from a team name.
The same ownership check applies after resolving any supplied object ID.  Both
an unknown entry and another coach's entry return `404`, preventing IDOR
enumeration.

The coach lineup endpoint returns an allowlisted private draft view.  Official
submitted lineups, when league policy makes them visible, must be exposed by a
separate public read model; submission never makes the mutable draft public.

## Public and private representations

Public routes use explicit serializers containing competition display fields
only.  Coach contact/profile data, password hashes, sessions and CSRF material,
private drafts and provenance, audit payloads, privileged controls, and private
ruling/correction reasons are never fields in those schemas.  New public
routes must introduce another allowlisted read model rather than serializing a
repository or domain object generically.

Templates may inspect a resolved principal to alter presentation, but hiding a
control is not enforcement.  JSON and server-rendered operator routes must use
the same dependencies before their handler or template executes, so direct URL
access is protected.

The existing admin HTML documents are non-sensitive token-entry/application
shells and remain navigable without a custom request header. Their privileged
data is fetched from protected JSON APIs; sensitive state must never be added
to the shell's server-rendered context.

## HTTP semantics

* `401` means no valid credential was supplied for a protected capability.
* `403` means a valid principal lacks the requested general capability (and is
  also used for CSRF failures).
* `404` conceals whether a private coach-owned object is absent or belongs to a
  different coach.

## Non-HTTP workflows

Authorization is an adapter at the request boundary, not a domain invariant.
Replay scripts, evidence sources, lineup proxy services, and scoring services
continue to accept explicit domain actor/proxy authority and do not depend on
browser sessions or FastAPI.  The processing order for routed work is:
request identity, authorization, existing domain operation, existing invariant
enforcement.
