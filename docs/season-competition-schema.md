# Season-aware competition identity

Revision `0004_season` adds the small relational parent core used by future
season domains. `bbbffl_season` has a UUID identity independent of its unique
human-facing year, label, lifecycle (`setup`, `active`, `completed`), timestamps
and optimistic version. Lifecycle only moves `setup -> active -> completed`;
completion is frozen and has no reopening shortcut. Transitions are privileged
writes appended through the shared audit-event boundary.

`season_rules_version` is append-only at the repository boundary and is owned by
one season. It represents that season's authoritative rules and configuration
context, not a globally reusable scoring-formula identity: annual squad size,
fees, lockouts, finals/SuperScore configuration, mappings, prizes and notes may
all differ even when scoring formulas do not. Therefore unchanged formulas in
2026 and 2027 still receive distinct persisted rules identities, and historical
references stay attached to the immutable season-owned version that governed
them. Later scoring package 26 will consume this context rather than interpreting
its IDs as global formula IDs. Its stable UUID, season-scoped logical key and
positive version number allow a later result to retain its exact interpretation.
A changed rule is a new row, never an edit, and creating one is audited.

`competition_stream` selects an explicit same-season rules version and has a
stream key unique only within its season. Its constrained type identifies only
sporting contexts: ordinary competition, finals, or SuperScore. Replay/test are
execution or data provenance concerns, not sporting stream types. Thus a
reconstructed season contains ordinary and SuperScore streams just as the
season being reconstructed did; isolation comes from its distinct `season_id`,
not by misclassifying those streams as `replay`.

`bbbffl_round` belongs to a competition stream and has a stable UUID plus a
stream-scoped key, label and sequence. `bbbffl_round_afl_reference` maps it to
the integer `season_id` and `round_id` supplied by the pinned `afl-api` v1
contract. The provider reference is not a BBBFFL primary key and no AFL fixture,
statistics or other upstream facts are copied. Multiple BBBFFL stream rounds may
map to the same AFL round.

**Invariant:** BBBFFL season/competition/round identity must never be resolved
from an implicit current AFL season or round. Repository operations therefore
take explicit parent IDs and expose no `get_current_season` convenience.

## Prototype compatibility

The scorer vertical slice continues unchanged on its legacy `competition_key`
tables. Existing `grand_final` and derived `superscore:<year>:<round>` rows are
preserved byte-for-byte and are **not** claimed or backfilled as new competition
streams: those keys lack enough trustworthy parent identity. New domain work
uses UUID-backed season entities. A later scoring/result package may introduce
an explicit, operator-approved adapter/crosswalk while migrating the vertical
slice; until then this conspicuous boundary avoids silently rewriting history.

Fixtures, entries/ownership, detailed exceptional mapping rules, results,
ladders/finals, SuperScore selection history and scoring-rule payloads remain
deferred to their roadmap packages.

## Coach and season-entry identity

Private human identity is stored in `coach`. Its UUID is stable when names or
contact details change and is deliberately unrelated to AFL/provider identity.
Contact/profile columns never appear in the public entry projection.

`season_entry` is a UUID-backed competition licence in exactly one
`bbbffl_season`. The human occupying it is recorded in temporal
`season_entry_coach_history`; a replacement closes the current interval and
adds another rather than changing an old row. Likewise,
`season_entry_team_name_history` retains every public rename interval. Partial
unique indexes permit only one open coach assignment and one open public name
per entry. `(season_id, licence_key)` is unique, while licence keys may be
reused in another season, so replay and live data cannot collide.

Later squad, draft, fixture, lineup, result, ladder, finals and SuperScore
tables should ordinarily foreign-key `season_entry.season_entry_id`. They must
not key records by `coach`, email, display name, team name, AFL player identity,
or a provider identifier. Scorer-approved entry transfers and renames append
events through the existing audit boundary with actor, reason, and before/after
attribution; no authentication capability is implied by the coach table.
