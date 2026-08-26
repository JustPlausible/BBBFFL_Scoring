# Season-aware competition identity

Revision `0004_season` adds the small relational parent core used by future
season domains. `bbbffl_season` has a UUID identity independent of its unique
human-facing year, label, lifecycle (`setup`, `active`, `completed`), timestamps
and optimistic version. Lifecycle only moves `setup -> active -> completed`;
completion is frozen and has no reopening shortcut. Transitions are privileged
writes appended through the shared audit-event boundary.

`season_rules_version` is append-only at the repository boundary. Its stable
UUID, season-scoped logical key and positive version number allow a later result
to retain its exact interpretation. A changed rule is a new row, never an edit.
Creating one is audited. `competition_stream` selects an explicit rules version
and has a stream key unique only within its season. Its type identifies ordinary,
finals, SuperScore, replay or test context without implementing their mechanics.

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
