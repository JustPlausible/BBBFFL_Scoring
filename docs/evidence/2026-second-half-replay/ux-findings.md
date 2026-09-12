# UX findings

The second-half replay reinforced the first-half conclusion that the domain
rules are generally strong, while several operator and coach workflows would
benefit from clearer presentation and more discoverable controls before normal
live-season use.

## Mid-season draft discoverability

The mid-season draft domain and CLI were sufficient for the historical replay,
but the next action was not obvious from the Administrator or Scorer dashboards.
The operator needed to know which CLI command to run and frequently had to
translate UUIDs into team/player names with database queries.

Issue #181 records the future requirement for a guided Admin/Scorer interface
covering ladder confirmation, delistings, generated selections, drafting,
trades and completion while reusing the existing domain state machine.

## Human-readable identities should be primary

Operator-facing output should prefer team/player/round names, retaining stable
UUID/provider identifiers as secondary diagnostics. This was particularly
noticeable when confirming mid-season draft order and entering draft activity.

The same principle applies to validation messages. Round 12 originally reported
`no AFL match found for team 1 in the mapped round`; issue #185 / PR #186 changed
the primary message to identify the AFL club by human-readable name.

## Invalid bye selections must remain correctable before lockout

Round 12 exposed a coach-experience trap: a player from an AFL club on a bye was
correctly rejected as unable to participate, but the old `INDETERMINATE — FAIL
CLOSED` presentation also disabled the entire selector, preventing the coach
from fixing the invalid choice.

Issue #185 / PR #186 introduced a distinct invalid-selection state. A confirmed
bye player remains un-submittable, but the dropdown stays editable until an
actual selective/main lockout freezes the position. Historical impossible
selections were reproduced only through an audited Scorer correction.

This establishes a useful UX distinction:

- deterministic non-participation (club bye): hard block submission;
- likely injury/team omission: future warning/filter candidate, not necessarily
  a hard block;
- unresolved/stale provider evidence: fail closed;
- genuine activated lockout: immutable through ordinary submission.

## Optional availability filtering would reduce coach mistakes

During bye rounds, lineup selectors still contain the full owned squad. A
future coach-facing option such as `Show likely available players only` could
hide or de-emphasise players known not to be participating, including:

- players from clubs on a bye;
- players explicitly reported injured/unavailable; and
- players omitted from official AFL team lists once that data is consistently
  available through afl-api.

Except for deterministic bye cases, this should remain advisory and reversible:
the coach retains final selection authority and can show all otherwise eligible
owned players. A later warning system could complement the filter.

## Player-selector ordering should be a per-user preference

Repeated delegated entry showed that fixed draft/selection order is not ideal
for every operator. A coach may value squad order, while a Scorer entering
multiple teams benefits from predictable alphabetical navigation.

Useful future options include:

- squad/draft selection order (current behaviour);
- alphabetical by player name;
- AFL club grouping; and
- availability-first ordering once reliable availability metadata exists.

The preference should belong to the user, not the BBBFFL team, so a coach and a
delegated Scorer can view the same squad differently without changing each
other's interface.

## Public ladder labels must describe the data actually included

While browsing forward to a scheduled future round, the public ladder correctly
continued to use only finalised results, but the subtitle could imply that the
selected future round had contributed to the standings. Issue #180 addressed
this presentation distinction so the label reflects the latest finalised round
represented by the ladder.

## Replay checkpoint stage wording can resemble live status

Round Preflight displays authoritative trigger activation status and, lower on
the same page, replay checkpoint recommendations. A recommendation pill labelled
`scheduled` is a checkpoint-schema stage value, not an indication that the
nearby lockout remains unactivated, but the visual similarity can make that
ambiguous.

This is very low priority and replay-only. A future wording such as `Replay
checkpoint stage: scheduled` would make the distinction explicit.

## Scorer/delegate tools were useful in routine operation

Rounds 11–20 demonstrated that delegated lineup entry and the Scorer review
surfaces are practical for repetitive weekly operation. This is reassuring for
2027 because ordinary coaches should perform most lineup entry themselves,
while delegated entry remains available for exceptional assistance rather than
being the normal workflow.

## Finals handoff should preserve the distinction between ladder and seeding

The Round 20 mathematical ladder remains useful evidence even though historical
2026 finals placement differed because of two known Scorer-error outcomes. PR
#188 therefore stores a separate replay-only historical finals-seeding snapshot
rather than changing what the public mathematical ladder says happened under
the reconstructed scoring evidence.

For future live seasons, finals seeding should normally be automatic from the
final mathematical ladder; any exceptional competition decision should be
explicit, auditable and clearly distinguished from ordinary standings.
