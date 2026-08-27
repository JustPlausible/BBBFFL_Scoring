# Staged AFL-match lockouts

**Status:** implemented, work package 23 (issue #34)<br>
**Implementation:** `bbbffl_app/app/lockouts.py`, integrated into
`bbbffl_app/app/lineups.py`'s `WeeklyLineupRepository.submit`<br>
**Schema:** `bbbffl_app/migrations/versions/0012_lockouts.py`
(revision `0012_lockouts`, see [`database-migrations.md`](database-migrations.md))<br>
**Depends on:** the validated `afl-api` v1 boundary
([`afl-api-v1-contract.md`](afl-api-v1-contract.md), issue #18), the accepted
BBBFFL-to-AFL round mapping ([`round-afl-mapping.md`](round-afl-mapping.md),
issue #31), the persisted round/matchup lifecycle
([`competition-lifecycle.md`](competition-lifecycle.md), issue #32), and
weekly lineup drafts/submissions ([`weekly-lineups.md`](weekly-lineups.md),
issue #33).

## Purpose

A BBBFFL round does not lock as a whole when the first AFL match begins.
Each selected player locks individually, according to the AFL match that
contains them. A lineup spanning several AFL matches therefore locks
progressively: players in started matches freeze while players in
not-yet-started matches remain editable.

## Player -> AFL match resolution

`app.lockouts.resolve_match(afl_team_id, matches)` matches a selected
player's cached `afl_team_id` (from `season_player_pool`, itself populated
from `afl-api`'s player identity endpoint) against the AFL club identities
embedded in the round's match list. This is a stable-ID join, never a
name-based one. `matches` is always the mapped round's match list from
`app.afl_client.AflApiClient.get_matches`, reached exclusively through the
accepted BBBFFL round mapping (`RoundMatchFactsProvider`, wrapping
`app.round_mapping.RoundMappingRepository.resolve` + the AFL client) --
never a provider "current round" or other implicit state. If zero or more
than one match involves the player's club, resolution fails explicitly
(`MatchResolutionError`) rather than guessing.

## The lock rule and where it lives

The lock *decision* is a pure function, `app.lockouts.evaluate_match_lock`,
that takes one resolved AFL `Match` and one explicit `evaluation_at` instant
and returns a `LockState` (`editable` / `locked` / `indeterminate`) plus a
reason:

- **editable** -- the match's status is `UPCOMING` (or a tolerated legacy
  alias) and `evaluation_at` is strictly before `start_time_utc`;
- **locked** -- `evaluation_at >= start_time_utc` (the boundary is
  inclusive: exactly at the scheduled start, the player is locked), **or**
  the match's status is `LIVE`, `POSTGAME` or `CONCLUDED` regardless of
  wall-clock time (status is authoritative over a stale/missing schedule --
  see [Unusual states](#unusual-postponedabandonedindeterminate-states)).
  `POSTGAME` and `CONCLUDED` are never collapsed into one reason
  (`match_status_postgame` vs. `match_status_completed`), matching
  `afl-api`'s own distinction between "siren has sounded" and "statistics
  declared final";
- **indeterminate** -- the match's raw status is not one of `afl-api`'s
  documented v1 lifecycle values or tolerated aliases
  (`app.afl_client.is_recognized_match_status`), or it is `UPCOMING` with no
  `start_time_utc` to evaluate against.

This function never calls `datetime.now()`; every caller supplies (or
defaults once, at the outer edge) an explicit `evaluation_at`. It has no
database access -- purely `Match` + `datetime` in, `(LockState, reason)`
out -- so the boundary/status matrix is covered by fast, hermetic unit tests
with no lineup, round or player fixtures at all.

Everything *around* that pure function -- resolving players to matches,
persisting irreversible evidence, enforcing the rule against a mutation,
and exposing a read model -- lives in `app.lockouts.LockoutRepository`.
`afl-api` supplies facts; BBBFFL, specifically this module, owns the
lockout decision and its persisted evidence.

## Deterministic evaluation

`LockoutRepository.lock_state(lineup_id, bbbffl_round_id, season_entry_id,
positions, *, match_facts, evaluation_at=...)` answers "what is locked
right now" for an arbitrary `{position: season_player_id}` map, given:

1. the persisted lineup (identified by `lineup_id`, from
   `app.lineups.WeeklyLineupRepository`);
2. the accepted BBBFFL -> AFL round mapping (via `match_facts`, a
   `RoundMatchFactsProvider` in production, a fixed fake in tests);
3. controlled AFL match timing/status facts (`match_facts.matches_for(...)`);
4. the supplied `evaluation_at`;
5. any already-persisted lock evidence (`weekly_lineup_lock`, see below).

Given the same five inputs, the same `LineupLockView` comes back every
time -- this is what makes 2026 replay deterministic: seed the same
persisted lineup, the same fixed match-fact fixtures and the same replay
clock, and lock state reproduces exactly. Tests never sleep, poll, or
depend on a live `afl-api` connection.

## Historical lock irreversibility

Once a position's lock has actually been *observed* -- by a coach/scorer
read of lock state, or by a submission attempt that evaluates it -- it is
durably recorded as one row in `weekly_lineup_lock` (PK
`(lineup_id, position)`, matching `season_player_id`/`afl_match_id`/
`observed_status`/`effective_lock_at`/`lock_reason`/`locked_at`). From then
on, `LockoutRepository._evaluate_position` always prefers that persisted
row over a fresh recomputation against `matches`. A later upstream
schedule/status correction -- a rescheduled start time moved back, a status
walked back from `LIVE` to `UPCOMING` -- therefore cannot silently unlock a
selection that had already, legitimately, locked.

`weekly_lineup_lock` rows are immutable: database triggers reject
`UPDATE`/`DELETE` on both SQLite and PostgreSQL, mirroring
`weekly_lineup_submission`'s immutability from issue #33. Nothing here
rewrites a submitted lineup version; lock evidence is a separate,
append-only table keyed off the same `lineup_id`.

Before a lock exists, a legitimate schedule correction *does* move the
boundary -- there is nothing to protect yet, so the corrected
`start_time_utc` simply governs the next evaluation.

## Interaction with #33 submitted versions

`WeeklyLineupRepository.submit`'s optional `lock_guard` parameter is the
sole integration point. When supplied (a closure built by
`LockoutRepository.guard(match_facts=..., evaluation_at=...)`), it runs
*inside* `submit`'s existing transaction -- after the previous effective
submission's positions are read, before the new version is written -- and
may raise `LockedSelectionError` to abort the whole submission. `submit`
never imports `app.lockouts`; the two modules stay decoupled through this
plain callable.

The guard compares the previous effective submission's positions against
the proposed ones:

- any position whose evaluated state is `locked` or `indeterminate` must
  keep its previous player unchanged, or the submission is rejected --
  covering removal, replacement and repositioning/swapping of a locked
  player, since a swap changes *two* positions and either one being locked
  is enough to reject the whole attempt;
- any position introducing a genuinely new player (one that was not
  already in that position) is rejected if that player's *own* match is not
  currently editable -- this is what stops Interchange, or any other
  still-open position, being used to route around a started club's lock
  ("no additional player from an already-started club may be added",
  per the season model's early-lockout rule).

A permitted edit -- changing only positions whose matches have not started
-- produces a normal new submitted version through the existing #33
machinery, with the frozen positions carried through unchanged and every
earlier submitted version left untouched, exactly as issue #33 requires.

## Concurrency

`submit`'s row lock on `weekly_lineup` (`SELECT ... FOR UPDATE` on
PostgreSQL) already serializes concurrent submissions for the same lineup;
`lock_guard` runs after that lock is acquired, so the loser of a race
re-evaluates lock state (and observes any lock evidence the winner just
committed) before its own compare-and-swap. An edit prepared against stale
(pre-lock) information therefore either commits against genuinely pre-lock
authoritative state, or fails with `LockedSelectionError` -- it can never
silently mutate a player who was already locked at the authoritative
decision point. Two independent connections racing to *observe* (not
mutate) the same never-before-evaluated lock rely on
`INSERT ... ON CONFLICT (lineup_id, position) DO NOTHING`, so simultaneous
first observations converge to one row rather than crashing or diverging.
See `tests/test_lockouts_concurrency.py` (PostgreSQL-only, opt-in via
`BBBFFL_DATABASE_URL`) for the exercised races; no test uses a client/
browser-supplied timestamp anywhere in this design.

## Unusual (postponed/abandoned/indeterminate) states

A match whose raw status is not one of `afl-api`'s documented v1 values or
tolerated legacy aliases (e.g. an upstream `POSTPONED`/`ABANDONED`/
`CANCELLED` value not yet part of the canonical vocabulary) evaluates to
`indeterminate`, never guessed as locked or unlocked. Ordinary coach edits
fail closed against an indeterminate position exactly as they do against a
locked one: an unchanged resubmission is safe (nothing about it changes),
but introducing a different player is rejected. `indeterminate` is
deliberately **not** written to `weekly_lineup_lock` -- it is not a
confirmed historical lock, so a later status correction (the postponed
match gets a real new start time) is free to resolve it normally on the
next evaluation.

## Read model

`LineupLockView` (`lock_state`'s return value) carries, per position, a
`PositionLockState`: `state` (`editable`/`locked`/`indeterminate`),
`reason`, `afl_match_id`, `effective_lock_at` (the scheduled start used),
`observed_status` (the raw AFL status observed) and `irreversible` (true
once backed by persisted `weekly_lineup_lock` evidence). This exists so a
later coach/scorer UI can explain *why* a position is locked without
recomputing any of these rules client-side. No UI is built in this issue.

## Audit

Ordinary lock evaluation and materialization never call
`app.audit.append_event` -- see [`audit-events.md`](audit-events.md)'s
domain-neutral boundary. This mirrors `app.competition_lifecycle`'s
upstream-fact observations: recording an already-authoritative AFL fact is
not itself a privileged decision. A future scorer/admin correction
mechanism that overrides a lock (out of scope for this issue) would need to
call `append_event` in the same transaction as its write, exactly like
every other privileged mutation in this codebase.

## Non-goals of this issue

Carry-forward/proxy entry (#22/roadmap 22), broader ownership/position/bye
validation beyond lock integrity (roadmap 24), the coach selection UI
(roadmap 25), matchup score calculation (roadmap 26), DNP/Interchange
replacement decisions (roadmap 27), scorer result finalisation (roadmap
28), and any alternate AFL schedule/status data source. `Match.start_time_utc`
and `app.afl_client.is_recognized_match_status` are the one narrow,
documented `afl-api` client extension this issue required (both fields were
already part of the validated v1 contract; only their consumption was
missing -- see [`afl-api-v1-contract.md`](afl-api-v1-contract.md)).
