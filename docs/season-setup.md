# Season setup (live-season initialization)

Issue #237. This is the **supported, production-safe** way to take a brand-new
live BBBFFL season from nothing to a running preseason draft, and later into
Finals and SuperScore. It is a browser workflow for the Scorer, Secretary or
Administrator. It needs no SQL, no UUIDs and no 2026 replay script.

- Page: `/admin/season-setup/{season_id}`. Open it from the Season Centre's
  **Season setup** link, or from the Scorer dashboard once the home-and-away
  season is complete.
- API: `/api/admin/season-setup/{season_id}/...` (see "Routes" below).
- Service: `bbbffl_app/app/season_setup.py`. Route:
  `bbbffl_app/app/routes/season_setup.py`.

## Live initialization vs 2026 replay tooling

| | Live season (this page) | 2026 replay/bootstrap tooling |
|---|---|---|
| Entry point | Browser page, Scorer/Secretary/Admin session | `scripts/bootstrap_round1_2026.py`, `scripts/bootstrap_2026_first_half.py`, `scripts/replay_2026_draft.py`, `scripts/finals_bracket_2026.py`, `scripts/superscore_round_2026.py` |
| AFL data | Live `afl-api` (`BBBFFL_AFL_MODE=live`) | Captured replay evidence files |
| Opening Round rules | Derived from the live AFL fixture | Fixed 2026 constants |
| Finals seeding | Live mathematical ladder only | 2026 historical seeding snapshot |
| Production | Supported | Every one of these scripts refuses to run when `BBBFFL_ENVIRONMENT=production` |

`scripts/bootstrap_2026_first_half.py` had no production guard before #237. It
now refuses, before connecting to any database, in every mode (including
`--readiness-only`), like its sibling scripts
(`tests/test_bootstrap_2026_first_half_cli.py`).

## The steps

The page lists every step with its status (**Complete**, **Ready**, **Action
needed**, **Blocked**, **Optional**, **Not applicable**, **Conflict** or
**Read-only**), its blocked reasons, and the **next safe action**. Every step
needs a reason, which is stored in the audit trail against your own identity.
The page pre-fills a sensible default reason.

1. **Season entries.** Create the season, the ten coaches and the ten teams in
   the Season Centre, as before.
2. **Player pool.** Choose the AFL season (the page marks the one whose year
   matches this BBBFFL season), then **Populate/Refresh from live afl-api**.
   This reads the complete season player list
   (`GET /api/v1/seasons/{id}/players`, every page) and upserts it in one
   transaction.
   - Safe to repeat. Club and name changes are applied. New players are added
     as eligible. Existing players keep their eligibility. Ownership is never
     touched, and nothing is deleted. Players missing from the latest list are
     reported, not removed.
   - Refused, with nothing written, when:
     - the chosen AFL season's year differs from this season's year;
     - the pool was already populated from a different AFL season or source;
     - the evidence came from a stale cache (the refusal says so);
     - afl-api returns an incomplete or malformed list (for example bad
       paging, a repeated player, or a player with no resolved club);
     - the app runs in replay mode.
3. **Ordinary competition.** One click creates the `ordinary` rules version (or
   reuses a single existing one), the ordinary competition stream, and Rounds
   1 to N, where N is the season's `regular_season_round_count`. This is one
   transaction.
   - Repeating it is a no-op.
   - A partial or differently shaped existing structure is refused, with the
     rounds that are missing or unexpected named. It is never "repaired".
4. **Opening Round compensating byes** (optional, and only if the fixture needs
   them). **Check the live AFL fixture** reads the AFL season:
   - If there is no round 0, the page says no rules are needed, and the season
     continues without creating any.
   - Otherwise each club playing in the Opening Round is listed with its
     compensating bye. This is the first later AFL round whose published bye
     list includes the club. The page recommends the BBBFFL round with the same
     number, and you confirm or change each target.
   - Acceptance re-derives the clubs, Opening Round and byes server-side from
     fresh evidence, then accepts every rule in one transaction.
   - Refused when:
     - a bye cannot be derived (for example a later round's bye list is not
       published yet);
     - the submitted clubs do not exactly match the clubs playing in the
       Opening Round;
     - a target is not an ordinary round;
     - any preseason pick has been made (this is a before-Pick-1 prerequisite).
   - An identical re-acceptance is a no-op. A different target for an
     already-accepted rule is refused, because changing an accepted rule is an
     audited correction, not setup.
   - This is configuration only. Coach nominations still happen later on the
     existing Opening Round operations page.
5. **Squad limit.** Players per team.
   - Re-saving the current value is a no-op.
   - Changing it after the draft order is accepted is refused by the ownership
     boundary.
6. **Preseason draft order.** Give each team a pick position (teams are shown by
   name), then accept. This uses the existing draft engine
   (`DraftRepository.accept_order`), which freezes the order and creates every
   snake pick atomically.
   - Prerequisites:
     - exactly ten entries;
     - the ordinary competition exists;
     - a squad limit is set;
     - at least `10 × squad limit` eligible players are in the pool.
   - Re-accepting the identical order is a no-op. A different order is
     refused.
   - Once accepted, the existing Scorer draft board (`/admin/draft/{season_id}`)
     and each coach's own draft page (`/account/preseason-draft/{season_id}`)
     are live.
   - The page warns you to settle the Opening Round first.
7. **Fixture-number draw.** Links to the existing fixture setup page. After
   that, ordinary rounds are mapped and opened from the existing Round
   preflight page.
8. **Finals.** Available only when every regular-season round is `final` and
   the live mathematical ladder has no unresolved equality.
   - Creates the `finals` competition stream (audited `finals.stream.created`)
     and then the bracket (`FinalsBracketRepository.create_bracket`). The
     bracket re-verifies every prerequisite under its own locks.
   - Seeding is always `ladder`. A season carrying a 2026 historical seeding
     snapshot is refused.
   - A ladder tie is refused with the existing "requires an explicit, audited
     Scorer/competition-governance determination" diagnosis. #237 does not
     invent a tie policy.
   - Repeating it is a no-op.
   - If the bracket step itself is refused after the stream was created (for
     example a result was corrected between the check and the bracket
     transaction), the page reports "finals stream created, bracket pending",
     and a retry reuses the stream.
   - Finals weeks then appear on the Round preflight page by name.
9. **SuperScore.** Available only once the Finals bracket exists. Creates the
   `superscore` stream and SS1 to SS4 in one transaction
   (`app.superscore_round.initialize_structure`, audited
   `superscore.stream.created` / `superscore.rounds.initialized`).
   - Repeating it is a no-op.
   - A stream left with only some SS rounds by the 2026 per-step tooling is
     completed.
   - Any differently shaped round is refused.
   - Each week is then opened with the existing paired Finals + SuperScore
     "Open week" action.

## Safety properties

- **Server-side prerequisites.** Every command re-checks from persisted state.
  The page's statuses are advisory.
- **Idempotent or fail-closed.** Every command either reports
  `created: false` / `changed: false` with nothing written, or refuses with a
  409 that names the conflicting state. It never modifies structure it did not
  create.
- **Transactional.** Each step's writes, and their audit rows, are one
  transaction. That transaction is serialized on the season row
  (`SeasonRepository.guard_writable`).
  - Opening Round acceptance also takes the preseason `season_draft` row lock
    that a pick takes, and the 2026 bootstrap's advisory lock.
  - Concurrent duplicate requests converge: one creates, the other is a no-op
    (`tests/test_season_setup_concurrency.py`, PostgreSQL).
- **No premature phases.** Finals needs a complete, untied home-and-away
  season. SuperScore needs the Finals bracket.
- **Season isolation.** Everything is scoped to one season.
  - The pool records which AFL season it came from.
  - The AFL season's year must match.
  - 2026 and 2027 coexist untouched (`test_initialization_is_isolated_per_season`).
- **Completed seasons are read-only.**
- **Authorization.** Every route requires `roundsetup.manage` (Scorer,
  Secretary, Administrator) and `require_role_covers_season`.
  - A season-scoped grant only reaches its own season.
  - Cookie-session writes need the double-submit CSRF token.
  - Coaches and spectators are refused.
- **afl-api failures.** These surface as a 503 with a setup-specific
  explanation, never as a partial write.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/season-setup/{season_id}` | Page |
| GET | `/api/admin/season-setup/{season_id}` | Read model: steps, statuses, blockers, next safe action |
| GET | `.../afl-seasons` | Live AFL season choices |
| POST | `.../player-pool` | `{afl_season_id, reason}` |
| POST | `.../ordinary-competition` | `{reason}` |
| GET | `.../opening-round?afl_season_id=` | Live Opening Round preview |
| POST | `.../opening-round` | `{afl_season_id, targets: [{afl_club_id, bbbffl_round_number}], reason}` |
| POST | `.../squad-limit` | `{squad_limit, reason}` |
| POST | `.../draft-order` | `{ordered_entry_ids, reason}` |
| POST | `.../finals` | `{reason}` |
| POST | `.../superscore` | `{reason}` |

## Evidence

- Automated coverage:
  - `tests/test_season_setup.py`: every boundary, clean start, repeat,
    refusals with tables and audit unchanged, isolation, and a full fresh-season
    journey through Finals and SuperScore.
  - `tests/test_season_setup_api.py`: a real Scorer browser session from a
    clean database to a coach making Pick 1, plus auth, CSRF, scoped grants,
    409 and 503.
  - `tests/test_season_setup_client_requests.py`: the page script.
  - `tests/test_season_setup_concurrency.py`: PostgreSQL.
  - `tests/test_afl_client.py` / `tests/test_afl_resilience.py`: the season
    player list.
- The clean-database acceptance run:
  [`evidence/season-setup-acceptance-2026-09-25.md`](evidence/season-setup-acceptance-2026-09-25.md).

This is automated-test-proven plus a disposable clean-database acceptance run.
It has not been rehearsed in a real production deployment.

## Not in scope

- Provisional (not-yet-afl-api) players.
- Coach credential provisioning.
- The `setup → active` gate.
- Season completion.
- A policy for exact ladder ties.

These remain separate items in
[`2027-live-season-readiness.md`](2027-live-season-readiness.md).
