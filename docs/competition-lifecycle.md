# Persisted competition lifecycle

The ordinary-round lifecycle is a bridge over existing season, fixture and
BBBFFL-to-AFL mapping identities. It does not schedule a draw or infer an AFL
round. Creating a lifecycle round snapshots the frozen fixture draw/version,
the accepted mapping/revision and its provider identities. The five persisted
matchups refer to the fixture matchup IDs and stable season-entry IDs; names are
presentation data and are not copied into result identity.

## States and commands

The forward-only state path is `upcoming -> open -> live -> review -> final`.
Opening revalidates that the frozen fixture and accepted mapping revisions are
still the revisions captured at creation. A changed or unresolved context
therefore fails closed instead of silently changing history. `review -> final`
is not a general transition: only the five-result publication command can make
it, in the same database transaction that creates every official version 1 and
sets every effective-result pointer.

A post-final correction requires a reason and appends a new official version
for all five matchups atomically. Earlier rows are protected from update and
delete by database triggers. The matchup pointer identifies exactly one
effective official version; audit events explain both publication and pointer
movement, but lifecycle/result tables remain the source of truth.

Calculated snapshots live in `bbbffl_matchup_calculation`, separately from
`bbbffl_official_result`. They are opaque storage for later scoring work, not a
scoring engine. Likewise `bbbffl_round_upstream_fact` retains provider status
observations such as `LIVE`, `POSTGAME` and `CONCLUDED`; those observations do
not drive the authoritative BBBFFL lifecycle.

Migration `0010_lifecycle` adds these tables without altering the existing
Grand Final or SuperScore prototype decision tables. All identities are
season/competition scoped, so replay and live seasons can coexist.
