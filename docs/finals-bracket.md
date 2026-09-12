# Finals bracket generation and lifecycle (issue #190)

Implementation notes for the main BBBFFL finals bracket. See
`docs/2026-finals-superscore-design.md` (issue #170) for the full design
rationale this implements; this document describes what was actually built
and how to operate it, in the same spirit as `docs/midseason-draft.md`.

## Scope

`app.finals.FinalsBracketRepository` owns one finals bracket per
`(season_id, competition_id)`: the frozen top-5 seed order and its exact
provenance, the four-week pairing/progression state machine, and explicit
elimination records. It builds on issue #197's `create_non_ordinary_round`/
`create_stream_matchup` primitives and does not implement coach lineup
submission, scoring, or result publication -- that is issue #191, built on
top of the bracket this module creates.

## Bracket structure

Top 5 by frozen finals seed qualify; 6th-10th are recorded eliminated
(`stage='pre_finals'`) at bracket creation.

| Week | Slot(s) | Pairing |
|---|---|---|
| 1 | `bye` | seed 1 (no match) |
| 1 | `qf` (Qualifying Final) | seed 2 v seed 3 |
| 1 | `ef` (Elimination Final) | seed 4 v seed 5 -- loser eliminated |
| 2 | `second_semi` | seed 1 v QF winner |
| 2 | `first_semi` | QF loser v EF winner -- loser eliminated |
| 3 | `preliminary` | Second Semi loser v First Semi winner -- loser eliminated |
| 4 | `grand_final` | Second Semi winner v Preliminary Final winner |

## Seed resolution

`create_bracket` resolves the seed order exactly once, applying
`app.finals_seeding.resolve_finals_seed_order`'s own logic against a single
consistent read -- never a call to that function itself (see
`app.finals`'s module docstring for why). If a `finals_seeding_snapshot`
exists for the season and matches the given ordinary `competition_id`, its
order and `snapshot_id` are frozen. Otherwise, every regular-season round
must be `final` first; then a single `LadderRepository.snapshot` read
provides both the order and its exact `result_references` provenance,
re-verified under `SELECT ... FOR UPDATE` (in deterministic `matchup_id`
order) inside the same transaction that persists `finals_bracket`, aborting
(`StaleSeedOrderError`) if any referenced result changed since the read.
Every later decision -- Week 1 pairing, a tie-break, an audit payload --
reads the bracket's own frozen `finals_bracket_seed` rows, never a fresh
ladder/snapshot read.

This ladder-fallback path also locks the same `bbbffl_season` row
`app.finals_seeding.FinalsSeedingRepository.apply`'s own
`_require_replay_context(locked=True)` locks before it creates the 2026
historical snapshot, and re-checks for one under that lock -- closing the
race where `apply` creates the authoritative (and known-different) snapshot
concurrently, after this method's own unlocked read observed none but
before its transaction commits. **This protection is one-directional.** If
instead `create_bracket`'s transaction wins that same season-row lock
first and commits a ladder-sourced bracket, then `apply` (which was
waiting on it) proceeds to create the historical snapshot anyway --
`FinalsSeedingRepository.apply` has no way to know a bracket now exists,
since checking `finals_bracket` from inside `app.finals_seeding` would
create exactly the reverse dependency this issue's own architecture rule
forbids (`app.finals_seeding` "remain[s] a read-only dependenc[y], never
depended upon in the other direction" -- see `app.finals`'s module
docstring and `tests/test_architecture.py::
test_finals_does_not_depend_on_routes_grand_final_lockouts_or_composition_root`'s
reverse-dependency assertion). Closing this direction would require
`app.finals_seeding` to consult `app.finals`'s own table, which #190 cannot
do without crossing that boundary itself; the documented operator workflow
(finals-seeding `apply` before finals-bracket `create-bracket`, per issue
#187's and this issue's own CLIs, never run concurrently against the same
season) is the only thing preventing it in practice today.

## Two confirmed policies (Steve, issue #190)

1. **Tie-break.** A tied finals match, including the Grand Final, is won by
   the team with the higher frozen finals seed captured when the bracket
   was created -- never a live/recomputed ladder, percentage, PF, or any
   other later ordering. See `FinalsBracketRepository._winner_loser`.
2. **Correction/rewind.** A corrected prerequisite result never
   destructively overwrites the pairing/elimination history it already
   produced (`CompetitionLifecycleRepository.correct_matchup_result` is the
   normal audited correction boundary -- already finals-compatible).
   `rewind_bracket` may then supersede and regenerate the *immediately*
   downstream pairing and its elimination together, but only while that
   downstream week has no play state (an authoritative lineup submission, a
   genuinely locked position, a ruling/adjudication/override, a persisted
   calculation, or a published official result). If any exists, it fails
   closed (`DownstreamPlayStateError`) and reports the affected artifacts
   for a human competition decision -- it never invalidates/replays them,
   and it never recurses past the immediately downstream week.

## Locking

Both `advance_bracket` and `rewind_bracket` `SELECT ... FOR UPDATE` every
prerequisite `bbbffl_matchup` row, in deterministic (sorted `matchup_id`)
order, inside the transaction that derives and persists a pairing. Because a
finals matchup's official result lives in the same `bbbffl_matchup`/
`bbbffl_official_result` tables an ordinary matchup uses, the existing
`correct_matchup_result` correction boundary already serializes against this
identical row lock -- no separate coordination mechanism was needed. See
`tests/test_finals_postgresql.py` for tests proving this under genuine
competing PostgreSQL transactions, not merely an unlocked re-`SELECT`.

## Lifecycle: opening a finals week

`app.finals_preflight.open_finals_week` is the stream-aware equivalent of
`app.round_preflight.open_preflight_round` for one finals week: it requires
an accepted AFL mapping (exactly like an ordinary round, via
`app.round_mapping.RoundMappingRepository`) and an already-derived pairing
(`create_bracket` for Week 1, `advance_bracket` for Weeks 2-4), then creates
the round's lifecycle row, materialises any not-yet-realised pairing into a
real `bbbffl_matchup`, and transitions the round to `open`. The HTTP surface
is `app/routes/finals_preflight.py` (`/api/admin/finals/...`).

## CLI

`scripts/finals_bracket_2026.py` mirrors `scripts/finals_seeding_2026.py`'s
preview/apply shape:

```
python -m scripts.finals_bracket_2026 --database-url ... \
    create-bracket preview --season-id <id> --competition-id <finals_id> \
        --ordinary-competition-id <ordinary_id>
python -m scripts.finals_bracket_2026 --database-url ... \
    create-bracket apply --season-id ... --competition-id ... \
        --ordinary-competition-id ... --reason "..."
python -m scripts.finals_bracket_2026 --database-url ... \
    open-week --bracket-id <id> --week 1
python -m scripts.finals_bracket_2026 --database-url ... \
    advance preview --bracket-id <id> --from-week 1
python -m scripts.finals_bracket_2026 --database-url ... \
    advance apply --bracket-id <id> --from-week 1 --reason "..."
python -m scripts.finals_bracket_2026 --database-url ... \
    rewind --bracket-id <id> --from-week 1 --reason "..." [--apply]
```

`preview` subcommands and `rewind` without `--apply` never mutate. Every
mutating subcommand requires an explicit, substantive `--reason` and refuses
to run while `BBBFFL_ENVIRONMENT=production`, matching every other replay
operator CLI in this repository.

## Not yet implemented (issue #191's scope)

- Coach lineup submission/lockout for the finals stream.
- Finals-specific review-readiness/sign-off, scoring-input snapshot
  freezing, and the variable-match-count publish/correction command a real
  finals result is actually recorded through.
- Public/coach/Scorer views for the six finals matches.
- Wiring the completed-season write fence (issue #195) into bracket
  creation/advance/elimination writes, once #195 lands.
- **Bracket-participant eligibility enforcement inside the lineup
  submission path itself** (see `docs/2026-finals-superscore-design.md`'s
  "Coach lineup/submission behaviour": "Eligibility is not enforced by
  `WeeklyLineupRepository` itself and must be enforced by the finals
  module"). `rewind_bracket`'s downstream-play-state lock closes the race
  where a *committed* submission is invisible to a concurrent rewind (see
  "Locking" above), but it cannot close the narrower race where `rewind_
  bracket` wins the `bbbffl_round_lifecycle` row-lock race against a
  submission that is already in flight (queued behind that same lock, or
  about to start): that submission still commits normally afterwards,
  since nothing in `app.lineups.WeeklyLineupRepository._finalize_
  submission` today checks whether its target `season_entry_id` is still
  the pairing's own participant. #190 does not modify `app.lineups` (an
  explicit safety boundary of this issue) and does not implement the
  bracket-participant eligibility check itself (explicitly #191's job, per
  the design doc). #191 must therefore perform that eligibility check
  *inside* the same locked `_finalize_submission` transaction (after
  acquiring the `bbbffl_round_lifecycle` lock, not before), not merely at
  the route/service layer above it, or this exact race remains open even
  after #191 lands.
