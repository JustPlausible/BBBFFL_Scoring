# Audit-event boundary

**Status:** implemented, work package 02 (issue #17)<br>
**Implementation:** `bbbffl_app/app/audit.py`, integrated into
`bbbffl_app/app/db.py`'s `DecisionsRepository`<br>
**Schema:** `bbbffl_app/migrations/versions/0003_audit_event.py`
(revision `0003_audit`, see [`database-migrations.md`](database-migrations.md))

## Purpose

BBBFFL has several materially state-changing or privileged operations --
scorer DNP rulings, Interchange assignment, manual score overrides, and
result finalisation today; roster/ownership changes, draft corrections,
coach submissions, proxy actions and administrative actions later. Each of
these needs a durable, attributable record of *how* authoritative state
changed, independent of which domain produced the change.

`app/audit.py` is that one reusable boundary. Nothing about it is specific
to scoring -- it knows nothing about DNP, Interchange, overrides or
finalisation. Those meanings live entirely in the calling domain module
(`app/db.py` for this PR); a later domain (e.g. roster ownership) calls the
exact same `append_event` function rather than inventing a parallel history
mechanism.

## Domain truth vs. audit history

Domain tables (`slot_dnp`, `interchange_assignment`, `score_override`,
`matchup_state`, and whatever tables later domains add) remain the **sole
source of truth** for current state. `audit_event` explains the sequence of
changes that produced that state. It is a diagnostic/explanatory record, not
an alternative current-state model:

- normal reads (`get_dnp_map`, `get_overrides`, the public/admin views, ...)
  never consult `audit_event`;
- `audit_event` is never replayed to reconstruct current state -- see
  "Replay" below;
- if the two ever disagree, the domain table is right and the audit trail is
  a diagnostic that needs investigating, not the other way round.

## Append-only invariant

There is no `update_event` or `delete_event` anywhere in this codebase.
`AuditEventRepository` (the read/query surface) only offers `list_events`
and `get_event`; `append_event` (the write surface) only inserts. Correcting
prior authoritative state means calling `append_event` again with the new
"after" state -- never rewriting a previous row.

As defence in depth beyond that application-level guarantee, revision
`0003_audit` also installs a database trigger (SQLite: `RAISE(ABORT, ...)`;
PostgreSQL: a `BEFORE UPDATE/DELETE` trigger function) that rejects any
`UPDATE`/`DELETE` against `audit_event`, surfaced identically on both
dialects as `sqlalchemy.exc.IntegrityError`. This is not the primary
guarantee -- the primary guarantee is that no application code path exists
to call it -- but it means a stray manual `UPDATE`/ad-hoc script can't
silently rewrite history either. See `tests/test_audit.py` for both layers
under test.

## Actor convention

`ActorContext` in `app/audit.py` only accepts a closed set of actor types
(`KNOWN_ACTOR_TYPES`):

| `actor_type` | Meaning |
|---|---|
| `system` | The application itself acted with no human operator (e.g. a scheduled job). |
| `legacy` | State inherited from a pre-audit database with no true actor to attribute. |
| `anonymous_operator` | A human used the shared-token scorer/admin proxy surface. `actor_role` (free-form, e.g. `"scorer"` / `"admin"`) still distinguishes duties even though there is no individual identity behind this actor type -- and it must never be used for an authenticated coach's own action (see [`coach-authentication.md`](coach-authentication.md), "Scorer/admin proxy provenance is unchanged"). |
| `coach` | Roadmap package 19 (issue #74): a real, authenticated coach identity, resolved by `app.auth.AuthenticationService` to the existing persistent `coach` row. `actor_id` is that `coach_id` -- never an email or other contact detail. Used only for the coach's own action (login, logout); a proxy action on a coach's behalf still uses `anonymous_operator`. |
| `unauthenticated` | Roadmap package 19: a request with no verified identity, used only for the login-attempt audit event itself (e.g. a failed login). `actor_id`, if set, is the `coach_id` the attempt resolved to -- never the submitted email/password. |

`append_event` raises `ValueError` if given any other `actor_type` --
including plausible-looking values like `"scorer"` or `"admin"` used as an
*identity* rather than `anonymous_operator`'s `actor_role`. That is the
specific mechanism that stops an unauthenticated action from masquerading
as an authenticated one: extending `KNOWN_ACTOR_TYPES` (as `coach`/
`unauthenticated` were for package 19) is a deliberate code change, never
something a caller opts into by passing an arbitrary string. See
[`coach-authentication.md`](coach-authentication.md) for the full coach
authentication/session design that introduced these two actor types.

## Action naming convention

Actions are stable, dotted `<domain>.<entity>.<event>` identifiers describing
what happened in domain terms, never UI labels or HTTP verbs:

- `scoring.dnp.changed`
- `scoring.interchange.changed`
- `scoring.override.changed`
- `scoring.result.finalized`
- `auth.login.succeeded` / `auth.login.failed` / `auth.session.logout` /
  `auth.session.revoked` (roadmap package 19, issue #74 -- see
  [`coach-authentication.md`](coach-authentication.md))

Treat an existing action string as part of the audit contract: don't
repurpose one for a materially different meaning. A new kind of event gets a
new action name, following the same `<domain>.<entity>.<event>` shape (e.g.
a future `roster.ownership.transferred` or `draft.pick.corrected`).

## Entity references

Every event names what it's about via `entity_type` + `entity_id`, e.g.
`entity_type="scoring.slot"`, `entity_id="grand_final:team_a:Forward1"`. The
pair together is the actual key -- the same `entity_id` string can mean
different things under different `entity_type`s (a DNP slot and a score
override for the same team/position happen to share their `team:position`
suffix), so always filter by both when querying one entity's history.
`entity_version` is available when a mutation is naturally versioned (e.g.
finalisation records the `finalized_at` timestamp there) so a reader can
correlate the event with a specific version of a larger, independently
stored record instead of duplicating that record's content.

## Payload/schema versioning

`payload_version` (currently `AUDIT_PAYLOAD_VERSION = 1`) records the shape
of `before_state`/`after_state`/`payload` at the time an event was written.
Bump it (or introduce a per-action version if one action's shape needs to
evolve independently of the others) when a future reader would otherwise
misinterpret an old event -- and never rewrite old rows to the new shape;
older events simply carry their original `payload_version` forever.

## Before/after representation

`before_state`/`after_state` are small structured dicts holding only the
fields needed to explain the mutation -- e.g. a DNP change stores
`{"dnp": true}`, not a dump of the `slot_dnp` row or the surrounding
`PositionResult`. This keeps payloads:

- **predictable to replay/diagnose** -- a reader knows exactly which keys to
  expect for a given action + payload_version;
- **free of accidental sensitive-data capture** -- nothing is serialized
  that wasn't deliberately chosen;
- **stable as unrelated domain fields evolve** -- adding an unrelated column
  to `slot_dnp` can't silently change what `scoring.dnp.changed` events
  contain.

Where the "after" state is more naturally a large, independently-stored
record, store a reference instead of duplicating it: finalisation's
`after_state` carries a small `team_scores` summary plus `entity_version`
(the `finalized_at` timestamp), not a copy of the full frozen scoring
snapshot that `matchup_state.finalized_snapshot` already holds.

## Correlation IDs

`correlation_id` groups every event produced by one logical command. Each
`append_event` call generates a fresh UUID4 by default; a caller that wants
several `append_event` calls to share one command generates one
(`app.audit.new_correlation_id()`) and passes it explicitly to each call --
see `DecisionsRepository`'s `correlation_id` keyword-argument, threaded
through to `append_event`. No distributed tracing infrastructure is
involved; it's just a shared UUID column, queryable via
`AuditEventRepository.list_events(correlation_id=...)`.

This is what will let a future multi-entity roster transaction or result
correction emit several related audit events under one command and have a
reader reconstruct them as one story.

## How to append events transactionally (for a new domain)

The pattern integrated into `DecisionsRepository` (`app/db.py`) is the
template every future domain repository should follow:

```python
from app.audit import ActorContext, append_event
from app.db import transaction

def set_something(self, ..., *, actor: ActorContext, reason: str | None = None) -> None:
    with transaction(self.conn) as conn:
        # 1. Read the existing value on the SAME connection/transaction.
        existing = conn.execute("SELECT ... WHERE ...", (...,)).fetchone()
        before_state = {...}  # small structured dict, not the whole row

        # 2. Write the new authoritative state.
        conn.execute("INSERT/UPDATE ... WHERE ...", (...,))

        # 3. Append the audit event, in the same transaction.
        append_event(
            conn,
            actor=actor,
            action="<domain>.<entity>.<event>",
            entity_type="...",
            entity_id="...",
            before_state=before_state,
            after_state={...},
        )
    # 4. transaction() commits both writes together on success, or rolls
    #    both back together -- including the domain write already issued --
    #    if append_event (or anything else in the block) raises.
```

Because the domain write and `append_event` share one
`app.db.transaction()` block, there is no window where one commits without
the other: a successful authoritative mutation cannot commit without its
audit event, and a failed audit append rolls back the domain mutation too
(`tests/test_audit.py::test_failed_audit_append_rolls_back_the_domain_mutation`
demonstrates this by making `append_event` raise mid-transaction and
asserting the domain row was never persisted).

## Read/query boundary

`AuditEventRepository.list_events(...)` supports filtering by any
combination of `entity_type`, `entity_id`, `action`, `correlation_id`, and
`limit`, always ordered by the database-assigned `sequence` column
ascending -- a monotonically increasing surrogate that exists purely to make
read ordering deterministic (the public identifier is `event_id`, a UUID).
`get_event(event_id)` reads one event back directly.

A tiny read-only diagnostic endpoint, `GET /api/admin/audit-events` (gated
by the same `require_admin` dependency as the rest of the scorer/admin
surface, accepting the same filter parameters), exists to prove the
boundary end-to-end through the real API rather than only through unit
tests -- it is not, and must not become, a full audit UI.

## Finals, SuperScore and season-completion action catalogue (issue #194)

The action names below are the ones `app.finals`/`app.finals_review`
(issues #190/#191), `app.superscore_round`/`app.superscore_review`/
`app.superscore_results` (issues #192/#193) and `app.season_awards`/
`app.season_completion` (issue #195) actually append, verified directly
against those modules' source and their test suites rather than assumed
from prose -- issue #170's design document's own catalogue sketch used
`finals.result.published`/`finals.result.corrected`/`season.completed`
correctly, but did not name every event these modules actually emit (e.g.
`finals.round.finalized`, `finals.premier.recorded`, and the distinction
between `finals.premier.recorded` and `season.premiership.recorded` below).
Every event follows the append-only, actor/reason-provenanced convention
above unchanged; nothing new is introduced by this section beyond the
names, entity types and one intentional near-duplicate pair explained below.

### Finals bracket lifecycle (`app.finals`)

| Action | `entity_type` / `entity_id` | When |
|---|---|---|
| `finals.bracket.created` | `finals.bracket` / `bracket_id` | Bracket creation (`FinalsBracketRepository.create_bracket`), once per `(season_id, competition_id)`. |
| `finals.bracket.advanced` | `finals.bracket` / `bracket_id` | `advance_bracket` derives and persists the next week's pairing(s) from the previous week's official result(s). |
| `finals.bracket.rewound` | `finals.bracket` / `bracket_id` | `rewind_bracket` (apply mode) supersedes and regenerates the immediately downstream pairing/elimination after an upstream correction, only while that downstream week has no play state. |
| `finals.elimination.recorded` | `finals.pairing` / pairing-scoped id | A pairing's losing entry is recorded eliminated (Elimination Final, First Semi-Final, Preliminary Final). |

### Finals result publication/correction (`app.finals_review`)

| Action | `entity_type` / `entity_id` | When |
|---|---|---|
| `finals.result.published` | `competition.matchup` / `matchup_id` | First official result for a finals matchup, one event per matchup in the week's `publish_finals_round` transaction. |
| `finals.result.corrected` | `competition.matchup` / `matchup_id` | A later, versioned correction to an already-published finals result (`correct_finals_result`); the prior version is preserved, never overwritten. |
| `finals.round.finalized` | `competition.round` / `bbbffl_round_id` | The finals week's `bbbffl_round_lifecycle` transitions to `final` once every match in that week has published. |
| `finals.premier.recorded` | `season.entry` / `season_entry_id` | Whenever the Grand Final publishes or its result is corrected -- **not** the official season award; see the distinction below. |
| `finals.wooden_spoon.recorded` | `season.entry` / `season_entry_id` | Recorded alongside `finals.premier.recorded` when the Grand Final first publishes, from the bracket's own frozen mathematical rank-10 provenance -- **not** re-recorded on a Grand Final correction (only the premier is), and **not** the official season award; see below. |

**`finals.premier.recorded`/`finals.wooden_spoon.recorded` are deliberately
not the same event as `season.premiership.recorded`/`season.wooden_spoon.
recorded` below, and must never be conflated when reading the audit trail.**
The `finals.*` pair is an informational record `app.finals_review` appends
purely from the finals stream's own state, the moment the Grand Final
publishes/is corrected -- there is no persisted, versioned `season_award`
row behind it, and it exists whether or not the season is ever completed.
The `season.*` pair below is the durable, versioned `season_award` record
issue #195's completion transaction (or an explicit `reconcile_premiership`/
`reconcile_wooden_spoon` re-recording) materialises against **locked,
effective** provenance -- the actual official award of record. A reader
reconstructing "who won the premiership" must use `season.premiership.
recorded`, never `finals.premier.recorded` alone.

### SuperScore stream/round lifecycle (`app.superscore_round`)

| Action | `entity_type` / `entity_id` | When |
|---|---|---|
| `superscore.stream.created` | `superscore.stream` / `competition_id` | The season's `superscore`-typed `competition_stream` is created (`ensure_stream`), once per season. |
| `superscore.round.review_state_created` | `superscore.round` / `bbbffl_round_id` | `setup_round` atomically creates the complete ten-entry `superscore_entry_review_state` row set for one of SS1-SS4. |

### SuperScore entry-scoped rulings (`app.superscore_review`)

| Action | `entity_type` / `entity_id` | When |
|---|---|---|
| `superscore.review.dnp_ruling.recorded` | `superscore.review.slot_ruling` / slot-ruling-scoped id | A DNP ruling for one entry's one slot (`record_dnp_ruling`), the entry-scoped counterpart of ordinary/finals' matchup-keyed `scoring.dnp.changed`. |
| `superscore.review.interchange_ruling.recorded` | `superscore.review.interchange_ruling` / interchange-ruling-scoped id | An interchange target-position ruling for one entry (`record_interchange_ruling`). |
| `superscore.review.override.recorded` | `superscore.review.override` / override-scoped id | A manual score override for one entry's one slot (`record_override`). |

An operator filtering `AuditEventRepository.list_events(action=...)` must
use the `Action` column above (the literal string `app.superscore_review`
passes as `append_event`'s `action=`) -- the `entity_type` values are a
separate field on the same event, not a substitute for it.

### SuperScore leaderboard publication/correction (`app.superscore_results`)

| Action | `entity_type` / `entity_id` | When |
|---|---|---|
| `superscore.leaderboard.published` | `superscore.leaderboard` / `bbbffl_round_id` | First published leaderboard for one of SS1-SS4 (`SuperScoreLeaderboardService.publish`, version 1). |
| `superscore.leaderboard.corrected` | `superscore.leaderboard` / `bbbffl_round_id` | A later, versioned correction to an already-published leaderboard (the same `publish` command, version > 1); the prior version is preserved. |

### Season awards and completion (`app.season_awards`/`app.season_completion`)

| Action | `entity_type` / `entity_id` | When |
|---|---|---|
| `season.premiership.recorded` | `season` / `season_id` | `reconcile_premiership` idempotently creates or supersedes the official, versioned `season_award(award_type='premiership')` record against the *effective* Grand Final result -- called directly, or as step 3 of `complete_season`. |
| `season.wooden_spoon.recorded` | `season` / `season_id` | `reconcile_wooden_spoon` idempotently creates or supersedes `season_award(award_type='wooden_spoon')` against the *live* mathematical Round 20 ladder -- deliberately never the finals bracket's frozen seed/provenance (see `app/season_awards.py`'s module docstring). Called directly, or as step 3 of `complete_season`. |
| `season.completed` | `season` / `season_id` | Step 4 of `complete_season`'s atomic six-step transaction, immediately before the step-5 `active -> completed` lifecycle transition (which itself appends the existing `season.lifecycle.changed` event). This is the event issue #194's final archival checkpoint (step 7) must observe and bind to -- see `app/season_archival.py` and `docs/2026-finals-superscore-playbook.md`. |

`season.completed`'s `payload` carries `premiership_award_id`/
`wooden_spoon_award_id`/`finals_round_ids`/`superscore_round_ids` -- the
complete set of round ids the readiness gate verified `final` -- so a reader
can confirm exactly what was checked without re-deriving it.

## Live-season initialization action catalogue (issue #237)

The production-safe [Season setup](season-setup.md) commands record these,
each in the same transaction as its domain write, attributed to the acting
Scorer/Secretary/Administrator (`anonymous_operator`, `actor_id` = their
`coach_id` for a session, `actor_role` = their active role) with the
operator's reason. Existing actions they reuse unchanged:
`season.rules_version.created`, `ownership.squad_limit.configured`,
`draft.order.accepted`, `opening_round.rule.accepted`,
`finals.bracket.created`, `superscore.stream.created`.

| Action | `entity_type` / `entity_id` | When |
|---|---|---|
| `player_pool.season.refreshed` | `season.player_pool` / `season_id` | A complete live afl-api season player list was upserted (`PlayerPoolRepository.refresh_season_pool`): counts inserted/updated/unchanged, pool size, source provider. |
| `season.ordinary_competition.initialized` | `season` / `season_id` | `SeasonRepository.initialize_ordinary_competition` created the ordinary stream and Rounds 1-N (plus the rules version, which records its own `season.rules_version.created`). Not recorded for an idempotent no-op. |
| `finals.stream.created` | `competition.stream` / `competition_id` | `FinalsBracketRepository.ensure_finals_stream` created the season's `finals` competition stream, immediately before bracket creation. |
| `superscore.rounds.initialized` | `superscore.stream` / `competition_id` | `app.superscore_round.initialize_structure` created one or more of SS1-SS4 (listed in `after_state.created_rounds`). |

## Replay

Audit events are **not** replayed to reconstruct current scoring. What the
design guarantees for replay purposes (see the 2027 roadmap's replay
strategy) is:

- deterministic event ordering (`sequence`);
- persisted timestamps and IDs (`occurred_at`, `event_id`);
- structured, versioned before/after state (`before_state`/`after_state`/
  `payload_version`);
- stable action semantics (the naming convention above).

That is enough to explain and reconstruct *the sequence of decisions* a
scorer made (a DNP, then an Interchange assignment covering it, then a
correcting override, then finalisation, for example) for diagnostics or a
future replay harness -- domain state remains authoritative for what the
*result* of that sequence actually was.
