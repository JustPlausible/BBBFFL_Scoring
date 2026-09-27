# Season player pool and ownership boundary

BBBFFL stores one `season_player_pool` row per BBBFFL season and canonical
`afl-api` player ID. The canonical numeric ID is identity; `display_name`, AFL
club ID/name, eligibility and source timestamps are a season-specific read
cache for selection screens. They are replaceable AFL facts, not a second AFL
system of record. In particular, refreshing AFL club membership never changes
fantasy ownership. Consumers should use `PlayerPoolRepository.list_selectable`
rather than query an upstream database or match names.

`given_name`/`family_name` (issue #248, afl-api commit `d21d15a`) are the
same kind of cached, replaceable afl-api fact as `display_name` and the club
columns: nullable, refreshed the same way, and never a second authority.
BBBFFL never derives either field by splitting `display_name` -- a player
with no upstream structured name simply has `NULL` in both columns,
including every row cached before this pair existed. In short:

- `canonical_player_id` -- stable AFL player identity;
- `display_name` -- the required, preferred presentation name;
- `given_name` / `family_name` -- cached authoritative structured-name facts
  from afl-api, present only where afl-api resolves them;
- AFL team ID/name -- cached season-scoped club facts.

`PlayerPoolRepository.browse`'s searchable text (the shared player browser
behind the live draft, the coach shortlist and mid-season delisting screens)
matches `given_name`/`family_name` alongside `display_name`, the AFL club
and the canonical ID, but default ordering stays by `display_name` -- an
explicit given/family-name sort is later UI work building on these columns,
not part of this cache boundary.

The cache records its public-contract provider, fetch time and (when supplied)
upstream update time. Thus a 2026 replay snapshot and a 2027 live snapshot of
the same canonical player can coexist and can have different club facts.
BBBFFL only integrates through the documented public `afl-api` v1 client
contract; no Champion Data/CFS or `afl-api` internal table is referenced.

BBBFFL ownership is authoritative and lives in `player_ownership_period` as
half-open intervals (`acquired_at <= t < released_at`). Releasing, transferring
and reacquiring append periods rather than replacing an owner field. Composite
foreign keys prevent cross-season player/entry linkage. Database overlap
triggers, a unique current-owner index, and transactional parent-row locking
enforce exclusive ownership, including concurrent acquisition. Transfers emit
correlated append-only audit events in the same transaction.

`season_squad_configuration` holds the positive season limit.
`OwnershipRepository.validate_squad_capacity` is the reusable effective-time
validator for later draft and transaction services; this package deliberately
does not implement those workflows. Existing JSON prototype teams remain the
configured input to the current scoring application and are not migrated or
removed.
