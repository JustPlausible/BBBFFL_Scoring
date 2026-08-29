# Scorer round review, sign-off and correction

Roadmap package 28 (issue #58) is the boundary between a *calculated*
BBBFFL result and an *official* one. Calculated scores and AFL match
completion are never automatically finalised: a scorer's explicit sign-off
is the only transition into official competition history, and once
published an official result version is never edited in place.

This package adds no second scoring engine and no parallel decisions
store for the Grand Final/SuperScore vertical. It is the ordinary-round
counterpart to `app/scorer_decisions.py`/`app/db.py`'s
`DecisionsRepository`, built directly on top of the persisted round/
matchup/result lifecycle (`app/competition_lifecycle.py`, roadmap package
18/#32) and generalised match scoring (`app/calculations.py`, roadmap
package 26/#35).

## Schema

`migrations/versions/0019_round_review.py` adds:

- `bbbffl_matchup.review_version` — a per-matchup optimistic-concurrency
  counter, bumped by every ruling/override write against that matchup.
- `bbbffl_official_result.input_snapshot` — a nullable JSON column that
  freezes the exact inputs an official version was computed from. Null
  for any pre-existing row or any caller that does not supply one.
- `bbbffl_matchup_slot_ruling` — ordinary-round DNP rulings, keyed by
  `(matchup_id, season_entry_id, slot)`. `slot` uses the weekly-lineup
  vocabulary (`F1..F3`, `M1..M3`, `Ruck`, `Tackler`, `Interchange`) —
  the same names `app.calculations`'s calculated snapshot already uses
  per-slot, not `app.scoring`'s internal `Forward1`/... names.
- `bbbffl_matchup_interchange_ruling` — one row per `(matchup_id,
  season_entry_id)`; `target_position` is the covered position, or
  `NULL` for an explicit "leave vacant" ruling. Row absence means
  unresolved, not "no coverage".
- `bbbffl_matchup_override` — manual score overrides, requiring a
  non-empty `reason` and an authorised actor; retains both
  `calculated_score` (the original value) and `override_score` (the
  replacement).

`app.calculations.MatchupCalculationService` also now embeds, per slot,
`app.participation.assess_participation`'s DNP-evidence classification
and (per side) `interchange_potential_scores` — what the Interchange's
current AFL stats would score at each position — so the review read
model and the frozen input snapshot never need a second afl-api round
trip to explain a ruling.

## Scorer/API workflow

`app/round_review.py`:

- `RoundReviewRepository` — CRUD for rulings/overrides, one method per
  kind (`record_dnp_ruling`, `record_interchange_ruling`,
  `record_override`), each attributable, audited (`app.audit`) and
  CAS-protected via `expected_review_version`.
- `build_round_review`/`build_matchup_review` — the read model: all five
  matchups, both teams/coaches, calculated scores and lineup versions,
  rules version, DNP/interchange evidence and rulings, existing
  overrides, official-result history, and `eligible_for_signoff`/
  `blockers` per matchup and for the round as a whole.
- `attempt_signoff`/`attempt_correction` — validate a fresh review, then
  make exactly one call into `CompetitionLifecycleRepository.
  publish_results`/`correct_matchup_result`.

`app/routes/round_review.py` exposes this at `/api/admin/round-review`:
`GET /{round_id}` (review), `POST /{round_id}/dnp|interchange|override`
(rulings/overrides), `POST /{round_id}/signoff`, `GET
/matchup/{matchup_id}/history`, `POST /matchup/{matchup_id}/correct`.
Like every other route module, it never imports the season model
directly — see `tests/test_architecture.py`'s `ROUND_REVIEW` group.

A manual override requires `actor_role` in `{"scorer", "admin"}`
(`AUTHORISED_OVERRIDE_ROLES`) and a non-empty reason; every other role
is rejected with `UnauthorisedActorError`. This is the same
pre-authentication `ActorContext`/`actor_role` convention
`app.scorer_decisions` already uses (real per-person auth is roadmap
package 19/20, not yet built).

## Atomicity and concurrency

`CompetitionLifecycleRepository.publish_results`/`correct_results`/
`correct_matchup_result` remain the sole transaction boundary for
official-result writes — nothing in `app.round_review` opens a
multi-row write transaction of its own. `attempt_signoff` builds a
review, validates it, then makes exactly one `publish_results` call
carrying `expected_round_version` and `expected_review_versions`;
`publish_results` re-checks both against the row it locks, inside the
same transaction that writes all five results and flips the round to
`final` — closing the gap between "read the review" and "commit the
write" without a second lock anywhere else. A failure injected partway
through that loop (`failure_hook`, used by
`tests/test_round_review.py::test_signoff_failure_partway_rolls_back_all_five_matchups`)
rolls the whole transaction back: no official row, no state change.

`tests/test_round_review_concurrency.py` proves this under genuine
PostgreSQL row-lock contention (SQLite has no `SELECT ... FOR UPDATE`,
so these are Postgres-only, like `test_competition_lifecycle_
concurrency.py`): two scorers racing the same ruling serialize, and the
loser gets `StaleRoundVersionError`; two simultaneous sign-off attempts
produce exactly one official version; a correction based on a
now-stale review revision fails closed even when it only unblocks after
the conflicting write's row lock releases.

## Official-result versioning and correction

`bbbffl_official_result` was already an append-only, database-trigger-
enforced immutable table (0010) with a per-matchup version sequence and
an `effective_official_version` pointer on `bbbffl_matchup`. This
package only adds `input_snapshot`. `correct_matchup_result` (new)
corrects exactly one matchup: it appends version N+1, repoints
`effective_official_version`, and never moves the round out of `final`
— there is no "reopen" state, because nothing about the round's own
lifecycle needs to change for one matchup's official history to grow a
new version. Version 1 (and every prior version) is preserved exactly,
byte-for-byte, forever.

A consumer can determine everything roadmap package 28 promises
downstream systems without adopting "latest calculated score": read
`bbbffl_matchup.effective_official_version`, then
`bbbffl_official_result` for that `(matchup_id, version)` — its
`published_at`/`published_by`/`reason` name who/when/why, and
`AuditEventRepository.list_events(entity_type="competition.matchup",
entity_id=matchup_id)` gives the full `published -> corrected` sequence
with reasons, distinct from (and never the sole source for
reconstructing) the official-result versions themselves.

## Deliberate scope decisions

- No ladder/finals recomputation — issue #58 exposes correction
  metadata (`effective_official_version`, full version history, audit
  sequence) for a later package to consume, and deliberately does not
  implement ladder response to a correction itself.
- No new scorer UI/HTML — the existing Grand Final/SuperScore vertical
  has one (`admin.html`); the persisted season model has no HTTP-routed
  UI at all yet (see `docs/architecture.md`), so this package adds only
  the JSON API a future scorer UI would call, matching every other
  season-model route module (draft, preseason).
- Round-wide `correct_results` (all five matchups at once) already
  existed and is left untouched; `correct_matchup_result` (new) is the
  primary, narrower correction path this package's tests/API use, since
  a real correction is almost always one matchup's transcription error,
  not a round-wide republish.
