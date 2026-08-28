"""Append-only audit-event boundary for privileged/state-changing BBBFFL operations.

This module is the one place any domain repository should go to explain *how*
authoritative state changed. It is deliberately domain-neutral: nothing here
knows about DNP, interchange, overrides or finalisation specifically -- those
concepts live in app/db.py, which calls `append_event` from inside its own
transaction. Future domains (roster/ownership changes, draft corrections,
coach submissions, proxy actions, ...) should call `append_event` the same
way rather than inventing a parallel history mechanism.

Domain tables remain the source of truth for current state. Audit events
explain the sequence of changes that produced that state; they are never
read back to *derive* current state (see docs/database-migrations.md and
docs/audit-events.md for the full design rationale).

## Append-only invariant

There is no `update_event`/`delete_event` here, on purpose -- callers can
only append and read. A correction to prior authoritative state must produce
a *new* event, never rewrite an old one. `migrations/versions/0003_audit_event.py`
additionally installs a lightweight database trigger that rejects UPDATE/DELETE
on `audit_event` as defence in depth; the application-level absence of those
operations is the primary guarantee tests rely on.

## Actor convention (pre-authentication)

Authentication does not exist yet (see roadmap package 19/20), so every event
must still name a well-defined, non-impersonating actor. `ActorContext` only
accepts the values in `KNOWN_ACTOR_TYPES` today:

- `system` -- the application itself acted without any human operator
  (e.g. a scheduled job, a migration-triggered action).
- `legacy` -- state inherited from a pre-audit database that has no true
  actor to attribute to (see the 0001/0002 bootstrap in app/migrations.py).
- `anonymous_operator` -- a human used the shared-token scorer/admin surface
  that exists today. `actor_role` can still distinguish "scorer" from
  "admin" style actions even though there is no individual identity yet.

Once package 19/20 introduces real coach/scorer/admin authentication, new
actor types (e.g. `coach`, `authenticated_scorer`) should be added to
`KNOWN_ACTOR_TYPES` explicitly -- never by relaxing this allowlist to accept
arbitrary strings. `append_event` raises if given an actor type outside the
allowlist, which is what stops an unauthenticated action from ever
masquerading as an authenticated identity.

## Action naming convention

Actions are stable, dotted, lower_snake identifiers of the form
`<domain>.<entity>.<event>`, e.g. `scoring.dnp.changed`,
`scoring.interchange.changed`, `scoring.override.changed`,
`scoring.result.finalized`. They describe what happened in domain terms, not
UI labels or implementation details ("changed", not "endpoint called").
Treat existing action strings as part of the audit contract: do not repurpose
one for a materially different meaning; introduce a new action name instead.

## Payload/schema versioning

`payload_version` records the shape of `before_state`/`after_state`/`payload`
for that action at the time the event was written. Bump `AUDIT_PAYLOAD_VERSION`
(or a per-action version, if one action's shape evolves independently) when a
consuming reader would otherwise misinterpret an old event; never rewrite
old rows to the new shape.

## Before/after representation

`before_state`/`after_state` are small structured dicts holding only the
fields needed to explain the mutation (e.g. `{"dnp": True}`), not a dump of
an ORM row or the full scoring snapshot. Where the "after" state is more
naturally a reference to a larger, independently-stored record (e.g. a
finalised scoring snapshot already held in `matchup_state`), store that
reference (`entity_version`, or a small summary in `payload`) instead of
duplicating the whole record here.

## Correlation IDs

`correlation_id` groups every event produced by one logical command/
transaction. Each call to `append_event` generates a fresh UUID4 by default;
a caller that needs several events to share one command (e.g. a future
multi-entity roster transaction) passes the same `correlation_id` explicitly
to each `append_event` call it makes within that command.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

AUDIT_PAYLOAD_VERSION = 1

# Actor types this package is allowed to record. Deliberately closed: an
# authenticated actor type (e.g. "coach", "scorer", "admin") must not be
# added until a real authentication mechanism can populate actor_id
# truthfully (see roadmap package 19/20). Extending this set is a conscious
# code change, not something a caller can opt into by passing an arbitrary
# string.
KNOWN_ACTOR_TYPES = frozenset({"system", "legacy", "anonymous_operator"})

# Stable action-name convention: "<domain>.<entity>.<event>". These four
# cover this PR's integration; later domains add their own constants here
# rather than inventing a second convention.
DNP_CHANGED = "scoring.dnp.changed"
INTERCHANGE_CHANGED = "scoring.interchange.changed"
OVERRIDE_CHANGED = "scoring.override.changed"
RESULT_FINALIZED = "scoring.result.finalized"
PLAYER_ACQUIRED = "ownership.player.acquired"
PLAYER_RELEASED = "ownership.player.released"
LINEUP_SUBMITTED = "lineup.submission.created"
LOCKOUT_TRIGGER_CONFIGURED = "lockout.trigger.configured"

ENTITY_TYPE_SLOT = "scoring.slot"
ENTITY_TYPE_INTERCHANGE = "scoring.interchange"
ENTITY_TYPE_OVERRIDE = "scoring.override"
ENTITY_TYPE_MATCHUP = "scoring.matchup"
ENTITY_TYPE_OWNERSHIP_PERIOD = "ownership.period"
ENTITY_TYPE_LINEUP = "lineup.weekly"
ENTITY_TYPE_LOCKOUT_TRIGGER = "lockout.trigger"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_correlation_id() -> str:
    """A fresh command/transaction identifier. Callers that want several
    `append_event` calls to share one command should generate one of these
    once and pass it to every call in that command."""
    return str(uuid.uuid4())


class ConnectionLike(Protocol):
    """Structural type for anything `append_event` can run SQL against --
    either a full `app.db.DatabaseConnection` or the short-lived
    `_TransactionConnection` yielded inside `app.db.transaction()`. Defined
    here, rather than imported from `app.db`, so every domain repository
    that calls `append_event` inside its own `transaction()` block can share
    one annotation without depending on `app.db`'s private connection
    classes."""

    def execute(self, statement: str, parameters: Any = ()) -> Any: ...


@dataclass(frozen=True)
class ActorContext:
    """Who/what caused an audited mutation.

    Not an authentication mechanism -- see the module docstring. `actor_type`
    must be one of KNOWN_ACTOR_TYPES; `actor_id`/`actor_role` are free-form
    and optional (e.g. actor_role="scorer" or "admin" to distinguish duties
    even while every request still shares one anonymous operator identity).
    """

    actor_type: str
    actor_id: str | None = None
    actor_role: str | None = None

    @classmethod
    def system(cls) -> "ActorContext":
        return cls(actor_type="system")

    @classmethod
    def legacy(cls) -> "ActorContext":
        return cls(actor_type="legacy")

    @classmethod
    def anonymous_operator(cls, role: str | None = None) -> "ActorContext":
        return cls(actor_type="anonymous_operator", actor_role=role)


@dataclass(frozen=True)
class AuditEvent:
    """One immutable row of `audit_event`. `sequence` is a database-assigned,
    monotonically increasing surrogate used only to make read ordering
    deterministic -- `event_id` (a UUID) is the stable public identifier."""

    event_id: str
    sequence: int
    occurred_at: str
    actor_type: str
    actor_id: str | None
    actor_role: str | None
    action: str
    entity_type: str
    entity_id: str
    entity_version: str | None
    correlation_id: str
    reason: str | None
    before_state: dict | None
    after_state: dict | None
    payload: dict | None
    payload_version: int


def _row_to_event(row: Mapping[str, Any]) -> AuditEvent:
    return AuditEvent(
        event_id=row["event_id"],
        sequence=row["sequence"],
        occurred_at=row["occurred_at"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        actor_role=row["actor_role"],
        action=row["action"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        entity_version=row["entity_version"],
        correlation_id=row["correlation_id"],
        reason=row["reason"],
        before_state=json.loads(row["before_state"]) if row["before_state"] else None,
        after_state=json.loads(row["after_state"]) if row["after_state"] else None,
        payload=json.loads(row["payload"]) if row["payload"] else None,
        payload_version=row["payload_version"],
    )


def append_event(
    conn: ConnectionLike,
    *,
    actor: ActorContext,
    action: str,
    entity_type: str,
    entity_id: str,
    correlation_id: str | None = None,
    entity_version: str | None = None,
    reason: str | None = None,
    before_state: dict | None = None,
    after_state: dict | None = None,
    payload: dict | None = None,
    payload_version: int = AUDIT_PAYLOAD_VERSION,
) -> AuditEvent:
    """Append one immutable audit event.

    `conn` must be the same transaction-scoped connection (from
    `app.db.transaction()`) that the caller used for its domain mutation, so
    the two inserts commit or roll back atomically -- see app/db.py's
    DecisionsRepository methods for the intended pattern:

        with transaction(self.conn) as conn:
            before = <read current state via conn>
            conn.execute(<domain UPSERT/DELETE>, ...)
            append_event(conn, actor=..., action=..., before_state=before, after_state=..., ...)

    If this raises, the caller's `with transaction(...)` block propagates the
    exception and the whole block -- including the domain write already
    issued on `conn` -- rolls back. There is no path that commits a domain
    mutation without its audit event, or vice versa.
    """
    if actor.actor_type not in KNOWN_ACTOR_TYPES:
        raise ValueError(
            f"Unknown actor_type {actor.actor_type!r}; must be one of {sorted(KNOWN_ACTOR_TYPES)} "
            "(authenticated actor types are not available before package 19/20)"
        )
    event_id = str(uuid.uuid4())
    occurred_at = _now()
    correlation_id = correlation_id or new_correlation_id()
    row = conn.execute(
        """
        INSERT INTO audit_event (
            event_id, occurred_at, actor_type, actor_id, actor_role, action,
            entity_type, entity_id, entity_version, correlation_id, reason,
            before_state, after_state, payload, payload_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING sequence
        """,
        (
            event_id,
            occurred_at,
            actor.actor_type,
            actor.actor_id,
            actor.actor_role,
            action,
            entity_type,
            entity_id,
            entity_version,
            correlation_id,
            reason,
            json.dumps(before_state) if before_state is not None else None,
            json.dumps(after_state) if after_state is not None else None,
            json.dumps(payload) if payload is not None else None,
            payload_version,
        ),
    ).fetchone()
    return AuditEvent(
        event_id=event_id,
        sequence=row["sequence"],
        occurred_at=occurred_at,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        actor_role=actor.actor_role,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_version=entity_version,
        correlation_id=correlation_id,
        reason=reason,
        before_state=before_state,
        after_state=after_state,
        payload=payload,
        payload_version=payload_version,
    )


class AuditEventRepository:
    """Read-only query surface over `audit_event`.

    Deliberately offers no update/delete -- only `append_event` (above) adds
    rows, and this class can only list/read them back. Ordering is always by
    `sequence` ascending, so history reconstruction is deterministic.
    """

    def __init__(self, conn: ConnectionLike):
        self.conn = conn

    def list_events(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        action: str | None = None,
        correlation_id: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEvent]:
        clauses = []
        params: list = []
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        base_query = "SELECT * FROM audit_event"
        if clauses:
            base_query += " WHERE " + " AND ".join(clauses)
        if limit is not None:
            # A limited read should return the most *recent* matching
            # events, not get permanently stuck on the oldest `limit` rows
            # once more than `limit` events exist. Select the newest
            # `limit` rows first, then re-sort that page back into the
            # deterministic ascending order every caller of this method
            # relies on.
            query = f"SELECT * FROM ({base_query} ORDER BY sequence DESC LIMIT {int(limit)}) AS recent ORDER BY sequence ASC"
        else:
            query = base_query + " ORDER BY sequence ASC"
        rows = self.conn.execute(query, tuple(params)).fetchall()
        return [_row_to_event(row) for row in rows]

    def get_event(self, event_id: str) -> AuditEvent | None:
        row = self.conn.execute("SELECT * FROM audit_event WHERE event_id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row is not None else None
