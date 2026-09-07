# Scorer Operations Dashboard (issue #147)

The Scorer Operations Dashboard (`bbbffl_app/app/scorer_dashboard.py`,
`bbbffl_app/app/routes/scorer_dashboard.py`,
`bbbffl_app/app/templates/scorer_dashboard.html`) is the Scorer's normal
operational home: the page an authenticated Scorer, season-scoped Replay
Operator or Administrator lands on to answer one question --

> What requires scorer attention now, what is waiting on somebody else,
> and what is the next safe action?

It is an **aggregation and navigation surface**, not a replacement for any
existing workflow. It performs no domain mutations, persists no
dashboard-specific state, and never reimplements mapping, lockout,
scoring, correction or adjudication rules -- every fact shown here is read
fresh, on every request, from the same authoritative repositories the
owning workflow pages already use, and every actionable item links
straight to that page.

## Relationship to existing workflows

| Concern | Owning workflow | Dashboard's role |
|---|---|---|
| AFL mapping, lockout-plan configuration, opening a round | [Round Preflight](round-preflight.md) (`/admin/round-preflight/{round_id}`, issue #152) | Surfaces preflight blockers/advisories and the accepted mapping/lockout plan; links to Preflight for every mutation. |
| Ordinary delegated/proxy lineup entry | Delegated operations (`/operations/rounds/{round_id}/lineup`, [`acting-context.md`](acting-context.md)) | Shows each team's authoritative submission/draft state and links here whenever a position is still editable. |
| Missed-initial-submission adjudication | `app.lineup_adjudication` (`/scorer/lineup-adjudication/{round_id}`, issue #146) | Flags exactly which teams have no submission *and* a trigger has already activated -- the only condition this workflow applies to -- and links there. Never conflated with ordinary delegated entry. |
| Correction of an already-locked submission | `app.lineup_correction` (`/scorer/lineup-correction/{round_id}`, issue #137) | Flags a private draft that diverges from a locked submission and a submission the round's calculation is now stale against; links there. Never conflated with adjudication. |
| Calculation, DNP/Interchange decisions, overrides, atomic sign-off/publication | Scorer Round Centre (`/scorer/round-centre/{round_id}`, issue #58) | Summarises calculation freshness, unresolved decisions and sign-off readiness read straight from `app.round_review.build_round_review`; every mutation (calculate, rule, override, transition, sign off) stays on the Round Centre. |
| Anonymous ladder/results | Public Round Centre (`/seasons/{season_id}/rounds/{round_id}`) | One direct link per round; the dashboard never duplicates the public read model. |

## Read model architecture

`app.scorer_dashboard.build_scorer_dashboard` composes, per call:

- **Lifecycle/round selection** -- `app.competition_lifecycle.
  CompetitionLifecycleRepository` for persisted state; `select_current_round`
  deterministically picks the earliest non-`final` round in the requested
  season (or the most recent `final` one once every round is published),
  unless an explicit, already-authorised `round_id` is supplied.
- **Preflight readiness** -- `app.round_preflight.build_round_preflight`
  (issue #152), consulted only while a round is `not_created`/`upcoming`.
- **Lockout/trigger state** -- `app.lockouts.LockoutRepository.
  describe_triggers`/`lock_state`, the *same* authoritative
  evaluation/materialisation path the lineup and lockout-plan pages use.
  Advancing replay time changes observed evidence but not persisted
  activation by itself; this module always durably materialises first
  (exactly like those pages), so the dashboard can never show a stale or
  empty activation merely because no other page happened to be opened
  first. Observed AFL match status and the durable activation fact are
  always kept in separate fields.
- **Lineup readiness** -- `app.lineups.WeeklyLineupRepository.get_draft`/
  `get_effective_submission` (read-only; never `get_or_create_header`,
  which would conjure a draft into existence merely by being asked) plus a
  per-position `lock_state` read, for every team competing in the round.
  A private draft is never presented as an authoritative submission; a
  draft that has changed since the last submission is its own distinct
  `diverged` state.
- **Review/publication readiness** -- `app.round_review.build_round_review`
  and `calculation_staleness_for_entry` (issue #58/#153), consulted once a
  round has calculated results.
- **Recent activity** -- `app.audit.AuditEventRepository`, scoped to this
  round's own lifecycle/matchup/lineup entities -- never a second audit
  store, never a global unscoped feed.
- **Human-readable identity** -- `app.identity.IdentityRepository`/
  `app.player_pool.PlayerPoolRepository`/`app.season.SeasonRepository`
  (issue #151's shared projections). UUIDs/provider IDs are never the
  primary label; every attention item carries only human-readable text,
  with internal identifiers available in an item's `diagnostics` field for
  operators who need them.

## Deterministic next safe action

`_determine_next_action` is a pure function of already-computed
authoritative state (lifecycle state, preflight blockers, trigger
activation, per-team submission state, review readiness) -- never a
client-side guess and never itself a mutation. It always returns exactly
one action with a `title`, `detail` and a `url` pointing at the existing
workflow that performs it; the dashboard never calls that workflow's
mutation itself.

## Attention queue

Every derived fact -- a preflight blocker, an unresolved DNP/Interchange
ruling, a missing submission, a stale calculation, an unconfigured or
not-yet-activated lockout trigger -- becomes one attention-queue item in
exactly one of five categories: **Blocking**, **Decision required**,
**Waiting**, **Advisory**, **Completed/recent**. Each item names why it
requires attention, its authoritative state, which capability can resolve
it, and a direct link -- never a dashboard-invented flag.

## Role and season boundaries

`app.routes.scorer_dashboard.require_scorer_dashboard` mirrors the Scorer
Round Centre's own authority boundary exactly (Scorer, Replay Operator or
Administrator; Coach and Secretary are never authorised). Every season
selection is re-validated through `app.authorization.
require_role_covers_season` -- a season-scoped Scorer/Replay-Operator
grant can never view a season outside that grant, and a season id supplied
by the browser never confers authority by itself. `GET /api/scorer/
dashboard` also returns the list of seasons the active role actually
covers, so a principal with more than one season's grant can explicitly
switch between them.

## Authoritative refresh

Every dashboard load performs a fresh server read; nothing is cached
client-side or server-side across requests (issue #153). Returning to, or
reloading, the dashboard after a mutation on any linked workflow page
always reflects that workflow's current authoritative state -- the
dashboard can never keep showing a lifecycle label, submission state or
publication status the linked page has already moved past.

## Out of scope

Reimplementing round, lineup, correction, adjudication or scoring
mutations; in-app voting/quorum; external notifications; database
restoration, checkpoint mutation or other replay infrastructure controls
(those remain outside the Scorer dashboard entirely); Administrator
identity/role-grant/bootstrap/system configuration management.
