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

## Actor convention (before authentication)

Authentication does not exist yet (roadmap packages 19/20). `ActorContext`
in `app/audit.py` deliberately only accepts a closed set of pre-authentication
actor types (`KNOWN_ACTOR_TYPES`):

| `actor_type` | Meaning |
|---|---|
| `system` | The application itself acted with no human operator (e.g. a scheduled job). |
| `legacy` | State inherited from a pre-audit database with no true actor to attribute. |
| `anonymous_operator` | A human used today's shared-token scorer/admin surface. `actor_role` (free-form, e.g. `"scorer"` / `"admin"`) still distinguishes duties even though there is no individual identity yet. |

`append_event` raises `ValueError` if given any other `actor_type` --
including plausible-looking future values like `"coach"`, `"scorer"` or
`"admin"` used as an *identity* rather than a role. That is the specific
mechanism that stops an unauthenticated action from masquerading as an
authenticated one: **only** package 19/20, by deliberately extending
`KNOWN_ACTOR_TYPES` once real authentication exists, may introduce those
values. Nothing here should be changed to accept them opportunistically.

## Action naming convention

Actions are stable, dotted `<domain>.<entity>.<event>` identifiers describing
what happened in domain terms, never UI labels or HTTP verbs:

- `scoring.dnp.changed`
- `scoring.interchange.changed`
- `scoring.override.changed`
- `scoring.result.finalized`

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
