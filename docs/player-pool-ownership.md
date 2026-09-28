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

## Provisional players (issue #242)

`season_player_id` is BBBFFL's stable internal player identity; `canonical_
player_id` is a cached afl-api association that can temporarily be absent.
A player is a **provisional player** exactly while `canonical_player_id IS
NULL` -- there is no separate flag, so nothing can drift out of sync with
that fact. This lets a legitimate AFL player who is not yet represented by
`afl-api` (a genuine rookie, or a mid-season recruit whose upstream identity
has not yet been published) participate in the BBBFFL draft/season without
BBBFFL inventing, guessing or reusing a fake `afl-api` ID -- see
`app.provisional_players`'s module docstring for the full lifecycle,
role boundary and reconciliation-safety design, and
[`docs/2027-live-season-readiness.md`](2027-live-season-readiness.md) for
current status.

In short:

1. **Coach nominates** a missing player (`app.provisional_players.
   PlayerNominationRepository.submit`) -- never creates the identity.
2. **Scorer/Administrator verifies and creates** the provisional player
   (`ProvisionalPlayerRepository.create`): `display_name`, `given_name`,
   `family_name` and a reason/source note are all required; `canonical_
   player_id` is left `NULL`. The new row is `eligible=TRUE` by construction,
   so it enters the normal season player pool immediately -- draft/ownership
   never distinguish a provisional player from a canonical one; both are
   drafted, owned, selected and scored identically via `season_player_id`.
3. **The Coach drafts** the provisional player through the existing draft
   workflow (or a Scorer/Admin proxy pick), exactly like any other player.
4. **A later live player-pool refresh** (`app.season_setup.
   refresh_player_pool`) calls `app.provisional_players.detect_candidates`
   once it commits: an exact, case-insensitive `given_name`+`family_name`
   match against a provisional player is surfaced as a
   `provisional_match_candidate` row for Scorer/Administrator review --
   never automatic reconciliation. The matched canonical pool row is
   quarantined (`eligible=FALSE`) while the match is unresolved, so nobody
   can draft "the same real person twice" under two different identities.
5. **Scorer/Administrator reconciles** (`ProvisionalPlayerRepository.
   reconcile`): attaches the chosen canonical `afl-api` identity to the
   *existing* provisional `season_player_id` (never creating a new player
   or repointing history) and retires the now-redundant duplicate canonical
   pool row. Every ownership period, draft pick, weekly selection and audit
   event already attached to that `season_player_id` survives unchanged.
   `was_provisional`/`provisional_note` remain permanently on the row as a
   historical record even after reconciliation; only `canonical_player_id
   IS NULL` decides whether a player is *currently* provisional (and
   therefore whether the persistent Coach/Scorer/Admin dashboard notices
   keep showing it).

Reconciliation is treated as a high-risk identity operation: it is
transactional, refuses a target that already carries BBBFFL ownership
history, refuses a target that is not itself canonical or not in the same
season, and a repeated attempt against an already-reconciled player fails
closed (`NotProvisionalError`) rather than double-applying.
