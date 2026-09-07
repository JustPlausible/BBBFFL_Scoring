# Administrator Dashboard (issue #148)

The Administrator Dashboard (`bbbffl_app/app/admin_dashboard.py`,
`bbbffl_app/app/routes/admin_dashboard.py`,
`bbbffl_app/app/templates/admin_dashboard.html`) is the Administrator's
role home: the page an authenticated Administrator lands on to answer one
question --

> Is the league/season correctly configured and governed, what
> administrative issues need attention, and where should the
> Administrator go next?

This is deliberately a different question from the Scorer Operations
Dashboard's ([`scorer-dashboard.md`](scorer-dashboard.md), issue #147):

| | Owns | Question it answers |
|---|---|---|
| **Administrator Dashboard** | Durable governance, readiness, authority, navigation | Is this season correctly configured/governed, and where do I go next? |
| **Scorer Dashboard** | Weekly operational attention, next-safe-action | What requires scorer attention *this round*, right now? |

Like the Scorer Dashboard, it is an **aggregation and navigation
surface**, not a replacement for any existing workflow. It performs no
domain mutations, persists no dashboard-specific state, and never
reimplements any workflow's own rules -- every fact shown here is read
fresh, on every request, from the same authoritative repositories the
owning workflow pages already use, and every actionable item links
straight to that page.

## Relationship to existing workflows

| Concern | Owning workflow | Dashboard's role |
|---|---|---|
| Detailed management of one season (entries, coaches, licences, readiness) | [Season Centre](season-centre.md) (`/admin/season-centre/{season_id}`, issue #100) | Every season-portfolio row and the selected season's readiness link straight here; the Administrator Dashboard never duplicates Season Centre's own entry/coach editing. |
| Role grants (Administrator/Scorer/Secretary/Replay Operator, season-scoped or global) | Season Centre's "Role grants" panel (`/api/admin/role-grants*`, [`acting-context.md`](acting-context.md)) | The dashboard's Role & access overview *reads* who holds what, and links to Season Centre for every grant/revoke mutation -- it never grants or revokes anything itself. |
| Draft order/execution | `/admin/draft/{season_id}` | Readiness/attention items link here while the draft is incomplete or unfinalised. |
| Preseason trades/opening-squad freeze | `/admin/preseason/{season_id}` | Readiness/attention items link here while the preseason window is open. |
| AFL mapping, lockout-plan configuration, opening a round | [Round Preflight](round-preflight.md) (`/admin/round-preflight/{round_id}`, issue #152) | Round-preparation attention items link here; the dashboard reuses `app.round_preflight.build_round_preflight` directly rather than re-deriving readiness. |
| Opening Round nominations | `/operations/seasons/{season_id}/opening-round` | Readiness/integrity items link here once a season has accepted an Opening Round rule. |
| **Everything about the current round once weekly operations begin** | [Scorer Operations Dashboard](scorer-dashboard.md) (`/scorer`, issue #147) | Summarised concisely (round, state, next action, attention-item counts) and linked to `/scorer` -- the Administrator Dashboard never reproduces the Scorer Dashboard's lineup table, lockout/trigger detail, correction/adjudication reasoning or publication logic. |
| Anonymous ladder/results | Public Round Centre (`/seasons/{season_id}/rounds/{round_id}`) | One direct link per season; never duplicated. |

## Read model architecture

`app.admin_dashboard.build_admin_dashboard` composes, per call:

- **Season identity, entries and readiness** -- calls
  `app.season_centre.build_season_centre` (issue #100) directly and reuses
  its result verbatim for competitions, entries and the draft/preseason/
  player-pool/Opening-Round readiness signals; this module adds governance
  framing on top, never a second readiness computation.
- **Round definitions vs. lifecycle rows** --
  `app.scorer_dashboard.ordinary_rounds_with_lifecycle` and
  `select_current_round`, extracted from the Scorer Dashboard module
  precisely so both dashboards share one source of the *definitions-vs-
  lifecycle* distinction: a `bbbffl_round` row with no matching
  `bbbffl_round_lifecycle` row is a configured definition that has never
  been opened, never presented as "0 rounds created".
- **Round preparation readiness** -- `app.round_preflight.
  build_round_preflight` (issue #152), consulted only while the current
  round is `not_created`/`upcoming`, exactly like the Scorer Dashboard's
  own preflight branch.
- **Scorer operational summary** -- calls `app.scorer_dashboard.
  build_scorer_dashboard` (issue #147) *in full* once at least one round
  has opened, and extracts only round/state/next-action/attention-item
  counts from its result. Nothing about lineup readiness, lockout/trigger
  evaluation, correction/adjudication eligibility or review/publication
  logic is re-derived here -- see "Cross-dashboard consistency" below.
- **Identity integrity** -- a uniqueness scan over `IdentityRepository.
  list_entries`'s existing result (duplicate public team names, duplicate
  licence keys); no new identity model.
- **Role and access overview** -- reads `app.auth.RoleGrantRepository`
  directly for display only; every mutation stays in Season Centre's
  existing role-grant API.
- **Audit/integrity overview** -- `app.audit.AuditEventRepository`, scoped
  to this season's own known entities (the season itself, its coaches,
  entries, draft, preseason window, fixture draw and role grants) --
  never a second audit store, never a global unscoped feed.

## Governance attention queue

Every derived fact becomes one attention-queue item in exactly one of five
categories, in priority order: **Blocking configuration**, **Authority /
security**, **Data / evidence readiness**, **Operational handoff**,
**Completed / recent**. Each item names why it matters, its authoritative
state, which capability can resolve it, and a direct link to the existing
owning workflow -- never a dashboard-invented mutation.

## Workflow map

A compact, six-stage, state-aware sequence -- setup, draft, preseason,
round preparation, weekly operations, season complete -- with exactly one
stage marked current, computed from the same authoritative readiness
facts the attention queue uses. This is navigation only, never a second
mutable workflow engine: each stage's link points at the existing page
that actually performs that stage's work.

## Season portfolio

`app.admin_dashboard.build_season_portfolio` lists every season an
Administrator may see (every season -- Administrator authority is never
season-scoped, see below) with human labels, lifecycle state,
competition/rules labels, team count, round definitions-vs-opened counts,
the current round and the most significant readiness blockers. The newest
season is never assumed to be the operationally active one: each row
describes its own season's facts, and the Administrator explicitly
selects which season the detailed dashboard below describes.

## Role boundary and route choice

Unlike the Scorer Dashboard (Scorer, Replay Operator or Administrator),
the Administrator Dashboard is **strictly Administrator-only**
(`app.routes.admin_dashboard.require_admin_dashboard`). Coach, Secretary,
Scorer and Replay Operator must never reach Administrator-only identity,
authentication-provenance, audit or configuration information here, even
where one of those roles also happens to hold Scorer capability -- that
principal reaches Scorer information through `/scorer`, never through
here. Where an Administrator also holds Scorer capability (Administrator's
capability set is a strict superset, see `app.authorization.CAPABILITIES`),
navigating to `/scorer` is permitted without expanding the underlying
Scorer authority model in any way.

Administrator authority is never season-scoped
(`app.auth.RoleGrantRepository.grant` refuses to create a season-scoped
`admin` grant) -- so, unlike the Scorer Dashboard, there is no season-scope
restriction to enforce for the *authority* itself.
`app.authorization.require_role_covers_season` is still called for an
explicit `season_id` as defence in depth (it always passes for
`Role.ADMIN`), and an unknown `season_id` 404s rather than leaking
anything.

### Route: `/admin/dashboard`, not `/admin`

Issue #148 proposes `/admin` *or* `/admin/dashboard`, and explicitly
requires resolving any conflict with the retained legacy Grand Final admin
route "explicitly ... do not silently merge legacy token authority with
session-native Administrator authority". `GET /admin` already exists
(`app.routes.admin.admin_page`): the legacy, `X-Admin-Token`-gated
Grand-Final/SuperScore scoring panel, predating coach authentication
entirely. Silently repurposing that path -- or redirecting it based on
which credential a request happens to carry -- would be exactly the kind
of silent merge the issue warns against. The Administrator Dashboard
therefore lives at the deliberately distinct `GET /admin/dashboard`;
`GET /admin` is completely untouched.

## Identity, role and environment

The dashboard's header shows the authenticated human identity, active
role, every granted role, represented-team context (if any), replay/live
classification (`BBBFFL_AFL_MODE`) and authentication provenance --
`authenticated_session`, `legacy_shared_token` or `unauthenticated`. It
never displays a token value or any other secret.

### Legacy-token precedence warning

`app.authorization.resolve_principal` resolves the legacy shared
`X-Admin-Token` credential *before* ever looking at a coach session
cookie -- so a request that happens to carry both silently gets
legacy-token authority, with no signal that a real signed-in session was
shadowed. `app.routes.admin_dashboard._authentication_provenance` detects
exactly this case (a legacy-token-resolved principal *and* a session
cookie present on the same request) and surfaces
`legacy_token_precedence_warning: true` -- never the token value itself.

## Cross-dashboard consistency

Both dashboards are built from the same authoritative repositories and
the same shared `ordinary_rounds_with_lifecycle`/`select_current_round`
functions, so they can never disagree about which round is current or
what state it is in. `tests/test_dashboards_agree.py` is a dedicated
integration test proving this at the HTTP level for the three cases issue
#148 names explicitly: weekly operations active means the Scorer Dashboard
resolves the same current round; a Scorer attention item shown here is
reachable on the linked Scorer surface for an authorised dual-role user;
and a published/final round is reported identically by both surfaces.

## Authoritative refresh

Every dashboard load performs a fresh server read; nothing is cached
client-side or server-side across requests (issue #153). Returning to, or
reloading, the dashboard after a mutation on any linked workflow page
(Season Centre, Draft, Preseason, Round Preflight, role grants, the Scorer
Dashboard's own linked workflows) always reflects that workflow's current
authoritative state.

## Out of scope

Reimplementing any Scorer decision, calculation or publication logic;
Docker/database/filesystem/secret-material controls; a replacement for
the public Round Centre; in-app voting/quorum; a superuser bypass around
existing role/capability checks.
