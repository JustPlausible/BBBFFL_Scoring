# Module boundaries and dependency direction

The authoritative preseason draft boundary and its atomic integration with the
player ownership ledger are described in [draft-ledger.md](draft-ledger.md).
The preseason trade/finalisation window that closes the draft out into a
frozen opening-ownership boundary is described in
[preseason-trades.md](preseason-trades.md).

This document is the roadmap package 03 (issue #36) deliverable: it names the
explicit service/repository boundaries the 2027 season model already has,
consolidates the one place that still mixed HTTP orchestration with business
rules, and pins the resulting dependency direction down with the tests in
`tests/test_architecture.py` so it cannot silently rot as later packages land.
Most of what issue #36 asks for was already true of `main` before this PR —
see "What roadmap 04–12 already delivered" below — so this is a consolidation
and documentation pass, not a rewrite: no repository's persistence mechanics,
no scoring formula, and no route's public behaviour changed.

## The rule

```
routes  --calls-->  application services  --calls-->  domain repositories  --calls-->  db/audit (persistence)
```

`app/scoring.py` is the exception: it is a pure, dependency-free leaf (no
imports of its own under `app/`) that the application layer calls into, never
the other way around. It is the "proven scoring implementation" issue #36
requires this refactor not touch, and `tests/test_architecture.py::
test_foundation_modules_have_no_internal_dependencies` fails if anything ever
gives it a dependency.

A route handler's job is to translate an HTTP request into a call against an
application service and the service's result (or raised domain exception)
back into a response — never to decide, itself, whether a mutation is legal.
An application service's job is to enforce that legality (does this team_key
exist, is this competition instance still open, is this the right moment to
finalise) and then delegate the actual write to a repository. A repository's
job is persistence mechanics only: it knows the SQL and the transaction
boundary for its aggregate, not league policy about who is allowed to change
what. `tests/test_architecture.py` encodes this as an import graph and fails
on the forbidden edges (route -> persistence, domain repository -> route,
anything -> the Grand Final/SuperScore vertical from the season model, a
cycle anywhere) rather than leaving it as prose that only this file states.

## The five boundaries issue #36 asks for

- **Season/configuration** — `app/season.py` (`SeasonRepository`): season
  identity, lifecycle (`setup -> active -> completed`), rules versions and
  competition streams. `app/config.py` is the separate, unrelated concern of
  environment-driven process settings (ports, tokens, the database URL).
- **Coach/team/ownership and roster state** — `app/identity.py`
  (`IdentityRepository`: coaches, season entries, team-name/coach-assignment
  history) and `app/player_pool.py` (`PlayerPoolRepository` for cached
  afl-api player facts, `OwnershipRepository` for acquire/release/transfer
  and squad-capacity enforcement). `app/teams.py` is a distinct, legacy
  concern: the Grand Final/SuperScore vertical's checked-in JSON roster
  config, predating this domain and deliberately left alone (see "What this
  PR does not touch"). `app/preseason.py` (`PreseasonRepository`) is the
  season-scoped preseason trade/finalisation window built on top of
  `OwnershipRepository`: audited multi-club trades and the frozen opening-
  ownership snapshot Round 1 relies on (roadmap package 15, issue #54; see
  [preseason-trades.md](preseason-trades.md)).
- **Weekly selections** — `app/lineups.py` (`WeeklyLineupRepository`: private
  drafts, immutable versioned submissions) and `app/lockouts.py`
  (`LockoutTriggerRepository`, `LockoutRepository`: the persisted round
  lockout plan and per-position lock evidence). Selections and lockouts are
  deliberately two modules, not one: `app/lineups.py`'s docstring documents
  why `submit()` only ever calls a duck-typed `lock_guard` collaborator
  (`LockoutRepository.guard(...)`, see `app/lockouts.py`) rather than
  importing `app.lockouts` — the reverse dependency (lockouts imports
  lineups, never lineups imports lockouts) is enforced by
  `test_lineups_and_lockouts_stay_decoupled`.
- **Opening Round deferred selection** (issue #69) — `app/opening_round.py`
  (`OpeningRoundRuleRepository`: versioned season+club configuration,
  mirroring `app.round_mapping`; `OpeningRoundNominationRepository`: the
  player-level nomination/correction boundary;
  `OpeningRoundSelectionGuard`: another duck-typed `lock_guard`
  collaborator, composable with `LockoutRepository.guard(...)`, that locks
  a nominated slot and excludes it from ordinary lockout evaluation). Sits
  above both weekly selections and lockouts — it reuses
  `app.lockouts.resolve_match` for AFL match resolution rather than a
  second copy — but, like lockouts, is never imported back by either; see
  [`opening-round-deferred-selection.md`](opening-round-deferred-selection.md).
- **Competition, fixtures, matchups and results** — `app/fixtures.py`
  (`FixtureRepository`: the frozen historical fixture draw), `app/
  round_mapping.py` (`RoundMappingRepository`: the versioned BBBFFL-round ->
  AFL-round mapping), and `app/competition_lifecycle.py`
  (`CompetitionLifecycleRepository`: ordinary-round lifecycle, matchups, and
  versioned official results). `app/calculations.py`
  (`MatchupCalculationService`) sits just above these three: it derives
  replaceable *calculated* snapshots from persisted fixtures/submissions and
  afl-api facts, and never reads or writes `bbbffl_official_result` — the
  calculated and official-result tables are intentionally different storage
  with different write paths, so a live estimate can never be mistaken for,
  or accidentally promoted to, an official result.
- **Scoring** — `app/scoring.py` (the pure scoring formulas: `score_position`,
  `ScoringRules`, the roster/position vocabulary) plus, for the Grand
  Final/SuperScore vertical specifically, `app/service.py` (matchup-state
  orchestration: `build_matchup_state`/`build_superscore_state`) and the new
  `app/scorer_decisions.py` (see below).
- **AFL resilience** (roadmap package 05, issue #37) — `app/afl_resilience.py`
  (`ResilientAflClient`: retry/timeout policy and a non-authoritative,
  freshness-tagged cache) and `app/afl_diagnostics.py`
  (`AflDiagnosticsRegistry`: secret-safe per-endpoint diagnostic state),
  sitting directly on top of the foundation `app.afl_client.AflApiClient`
  transport. Only `app/main.py` (the composition root) constructs and wires
  this wrapper; every domain/service module keeps depending on
  `AflApiClient`'s plain dataclasses and duck-typed `AflDataSource` call
  surface, never on the wrapper's concrete type, so it remains a drop-in
  replacement rather than a new required dependency. See
  `docs/afl-client-resilience.md` for the full design.
- **Scorer round review, sign-off and correction** (roadmap package 28,
  issue #58) — `app/round_review.py` (`RoundReviewRepository`: ordinary-
  round DNP/interchange rulings and manual overrides, keyed by
  `matchup_id`/`season_entry_id`; `build_round_review`/`build_matchup_
  review`: the read model; `attempt_signoff`/`attempt_correction`: the
  validated write path onto `CompetitionLifecycleRepository`'s existing
  atomic publish/correct methods). Unlike every other season-model
  module above, this one *is* imported directly by its route module
  (`app/routes/round_review.py`) — the same shape as `app/scorer_
  decisions.py` below, not the "only ever reached via `request.app.
  state`" shape the rest of the season model uses — because it is
  meant to be the one application-service layer routes call for this
  domain. See `docs/scorer-round-review.md` and `tests/test_
  architecture.py`'s `ROUND_REVIEW` group.

## What this PR adds: `app/scorer_decisions.py`

Before this PR, `app/routes/admin.py` and `app/routes/superscore.py` each
independently re-implemented the same three checks ("is this team_key part of
the competition instance", "is this slot/position a real one", "is the
competition instance already finalised and therefore locked") and the same
finalize-or-409 orchestration, directly against `app.db.DecisionsRepository`
and `fastapi.HTTPException`. That was route-handler business-rule
orchestration in the sense issue #36 means: correct, but not reusable outside
an HTTP request, and duplicated rather than shared between the two routers
that needed it.

`app/scorer_decisions.py` is the extracted application service: `set_dnp`,
`set_interchange`, `set_override` and `finalize` take a `DecisionsRepository`-
shaped object and the caller's `team_keys`, validate against
`app.scoring.ROSTER_SLOTS`/`SCORABLE_POSITIONS` and the repository's own
`get_matchup_state()`, and raise plain domain exceptions
(`UnknownTeamError`, `InvalidSlotError`, `InvalidPositionError`,
`CompetitionFinalizedError`, `ResultNotReadyError`) rather than
`HTTPException` — so the module has no HTTP dependency and stays usable from
an admin script, a replay harness, or a test, exactly as issue #36 asks.
`app/main.py` registers one `@app.exception_handler` per exception, mapping
each to the same HTTP status the two route files returned before (404, 400,
423, 409 respectively); the routes themselves now only translate the request
into a call and let the service decide.

The repository (`DecisionsRepository.set_dnp`/`set_interchange_assignment`/
`set_override`/`finalize`, in `app/db.py`) is untouched: it already owned its
own persistence mechanics and its own transaction, and still does — see
"Transaction ownership" below. Nothing about scoring math, the DNP/
interchange/override/finalize state machine, or the public JSON shape of any
route changed; `tests/test_api.py` and `tests/test_superscore_api.py` assert
byte-for-byte-equivalent status codes and response bodies before and after.

`tests/test_api.py::test_dnp_decision_crosses_route_service_repository_persistence_boundary`
demonstrates the full chain end-to-end for one real persisted use case: an
HTTP POST to `/api/admin/dnp` is read back through a second, independent
`DecisionsRepository`/connection opened directly against the same database
file (proving the write reached durable storage, not just in-process
`app.state`), and a rejected mutation (an unknown `team_key`, caught by
`app.scorer_decisions.set_dnp` before any repository call) is shown to leave
both the domain table and the audit trail untouched.

## Transaction ownership

Every multi-write domain operation in this codebase already opens its
transaction inside the repository method that owns it, via `app.db.
transaction()`, and commits the domain write and its `app.audit.append_event`
call together as one unit — never across two separate `with transaction()`
blocks, and never left implicit in a caller. This was true of
`DecisionsRepository` before this PR and remains the pattern this PR's new
module follows by *not* introducing a transaction of its own:
`app.scorer_decisions.finalize` decides whether finalising is legal and then
makes exactly one call into `DecisionsRepository.finalize`, which is the sole
transaction boundary for that write. The season-model repositories already
demonstrate this for genuinely multi-step commands — e.g.
`OwnershipRepository.transfer` (a correlated release+acquire, one
transaction), `CompetitionLifecycleRepository.publish_results` (five result
rows, five matchup updates and the round's `final` transition, one
transaction), and `WeeklyLineupRepository.submit` (revision check, ownership/
eligibility validation, the lock-guard callback, and the submission insert,
one transaction). Where a lock-evidence observation must survive even if the
caller's own transaction later rolls back (`app/lockouts.py`'s
`LockGuard.materialize`), that is a deliberate second, independent,
already-committed transaction run *before* the caller opens its own — see
`app/lockouts.py`'s module docstring, "Historical irreversibility" and
"Concurrency", for the full reasoning and why nesting write transactions
inside one another is not an option against SQLite's single-writer model.

A future package that needs one new operation to span *two different
repositories'* tables should give that operation its own explicit,
transaction-owning application-service function (following
`app.scorer_decisions.finalize`'s shape: validate, then make exactly one
repository call per logical write) rather than opening `app.db.transaction()`
directly from a route handler or reaching into a second repository's tables
from inside a first repository's own transaction.

## Deliberate consolidation, not a rewrite

Each season-model class listed above (`SeasonRepository`, `IdentityRepository`,
`PlayerPoolRepository`/`OwnershipRepository`, `FixtureRepository`,
`RoundMappingRepository`, `CompetitionLifecycleRepository`,
`WeeklyLineupRepository`, `LockoutTriggerRepository`/`LockoutRepository`) is
already the combined repository-and-invariant boundary for one aggregate: it
owns both the SQL and the domain rules that keep that aggregate consistent
(squad capacity, ownership overlap, lineup revision/CAS, lockout
irreversibility, and so on), all inside the one transaction that has to see
them atomically to enforce them correctly under concurrent writers — see each
module's own docstring, and `tests/test_*_concurrency.py`, for why. Splitting
each of these into a separate "pure repository" plus a "policy service" that
re-opens the same transaction from the outside would not remove any coupling;
it would only add an indirection layer while multiplying the surface area
for a concurrency or audit regression, which is exactly the outcome issue
#36's design constraints ("no speculative abstraction layers", "do not
regress ... concurrency ... guarantees") ask this refactor to avoid. This
document treats that existing shape as the intended, consolidated
architecture rather than mechanically refactoring code that later roadmap
packages already separated correctly.

One narrow, pre-existing exception is worth naming rather than hiding:
`WeeklyLineupRepository._validate_ownership`/`_validate_players` read
`player_ownership_period`/`season_player_pool` directly — tables that
`app/player_pool.py`'s repositories own — inside `submit()`'s own
transaction, rather than calling `OwnershipRepository`/`PlayerPoolRepository`.
This is a read-only referential check (does this selection point at a player
this entry currently owns), not a policy decision or a write, and it has to
run under the same row locks and the same transaction as the rest of
`submit()`'s compare-and-swap to be race-free — see `app/lineups.py`'s and
`app/player_pool.py`'s concurrency notes. Routing it through the other
repositories would mean either passing a live transaction connection across
a repository constructor boundary that does not exist today, or losing that
atomicity; either is a larger, riskier change than this package's scope
justifies, so it is left as a documented, intentional exception rather than
force-fitted into a cross-repository call for its own sake.

## What roadmap 04–12 already delivered

Issue #36 itself anticipates this: "existing later packages may already have
introduced some of these boundaries; implementation should first inventory
current main and consolidate". By the time this package started, `main`
already had: a season/rules/competition-stream boundary independent of any
route (`app/season.py`); a coach/team-identity boundary with full
assignment/rename history (`app/identity.py`); a player-pool/ownership
boundary with audited acquire/release/transfer and squad-capacity enforcement
(`app/player_pool.py`); a frozen historical fixture-draw boundary
(`app/fixtures.py`); a versioned AFL-round-mapping boundary
(`app/round_mapping.py`); a persisted ordinary-round lifecycle with versioned
official results (`app/competition_lifecycle.py`); a weekly-selection
boundary with private drafts and immutable versioned submissions
(`app/lineups.py`); a round-lockout-plan and lock-evidence boundary
(`app/lockouts.py`); and a season-aware live-calculation boundary
(`app/calculations.py`) — none of the season/competition/lineup/lockout
domains have any HTTP route at all yet, so "new domain rules can be
implemented without placing them in route handlers" and "repositories own
persistence mechanics rather than league policy" (for that domain) were
already true by construction. What main did not yet have was: (a) a
documented, test-enforced statement of the intended dependency direction
across all of these modules together (this document and
`tests/test_architecture.py`), and (b) the one place — the Grand Final/
SuperScore HTTP vertical, the only domain with routes at all — where
orchestration genuinely still lived in the route handlers rather than a
service (`app/scorer_decisions.py`, above). This package closes both gaps
without touching anything else.
