# 2026 finals and SuperScore replay design (issue #170)

**Status:** planning/decomposition, not implementation<br>
**Depends on:** the completed 2026 second-half replay through Round 20
([`docs/evidence/2026-second-half-replay/`](evidence/2026-second-half-replay/)),
the mathematical-ladder-vs-historical-finals-seed distinction (issue #169)
and the resulting audited snapshot (issue #187 / PR #188,
[`finals-seeding-2026.md`](evidence/2026-second-half-replay/finals-seeding-2026.md)).

## Purpose and scope

Issue #170 asks for the design required to continue the same 2026 historical
database from the completed Round 20 home-and-away replay through the main
finals series and the four SuperScore rounds, to season completion, while
preserving the 2026 dataset as historical evidence alongside the eventual
2027 live season. It is explicitly a planning issue: it does not implement
the finals or SuperScore system. This document is that plan, and the
"Follow-up issues" section is its decomposition into implementable work.
Season completion here means every required finals round **and** each of SS1,
SS2, SS3 and SS4 has reached lifecycle state `final`; a final Grand Final and
SS4 do not compensate for an unfinished earlier round in either stream. It
also requires current, internally consistent premiership and wooden-spoon
records bound to the effective result/snapshot versions from which they were
derived; #195 atomically materialises them before setting `completed`.

This document does **not** reopen the mathematical-ladder-vs-historical-seed
decision (issue #169/#187/PR #188) and does not introduce a generic ladder
editor or a 2027 replay exception (see "Explicit non-goals" at the end).

## Where the replay actually stands

- Rounds 1-20 of the 2026 home-and-away season are complete, published, and
  verified (`docs/evidence/2026-second-half-replay/round-results.md`).
- The mathematical Round 20 ladder is frozen as evidence and is never
  rewritten.
- An audited, replay-only `finals_seeding_snapshot` exists for this season
  (`app/finals_seeding.py`, migration `0028_finals_seeding.py`) recording the
  historical finals order: Running Hots, Bridesmaids, JHAS, Wolverines, Evil
  Absolutes, The Crabs, One Percenters, Motherruckers, Pommy Rules, The
  Plague.
- `resolve_finals_seed_order(database, season_id, competition_id)` is the
  documented integration seam for seed order in the general case, and its
  logic — a 2026 snapshot exists; no snapshot exists, so fall back to the
  mathematical ladder, tied-row check included — is exactly what the finals
  bracket module must apply. **Correction (Codex review, PR #196, sixth
  round): the finals bracket module does not literally call this function**
  — see "Seed consumption" below for why calling it and then separately
  re-reading for provenance is itself unsafe, and for the single-read
  mechanism (`FinalsSeedingRepository.get_snapshot`, or one `LadderRepository.
  snapshot` call) that applies this function's *exact* logic — including
  its `snapshot.competition_id == competition_id` and `ladder.season_id ==
  season_id` fail-closed checks — from one consistent read instead. Every
  non-2026 season still takes the mathematical-ladder path automatically,
  with no special-casing at the call site; only *how* that path's result and
  provenance are captured together changed. **Finals/SuperScore
  implementation must never recompute or override seed order through any
  path other than this one (whether by calling the function directly, or by
  applying its exact fallback logic against a single ladder read).**
- The season/database lineage is a single continuous line: the same
  `bbbffl_season` row (`season_id = 3832745c-c19a-4224-bceb-86ded6baa09c`)
  and the same `player_ownership_period`/result/audit history used since
  Round 1 first-half replay. There is no proposal anywhere in this document
  to bootstrap a fresh finals season or a fresh database. Finals and
  SuperScore are new `competition_stream` rows under the *same*
  `season_id`, exactly as the ordinary competition and the (not yet
  created) finals/SuperScore streams already anticipated by the schema (see
  below).

## Architecture: two verticals, and which one this design uses

The repository currently contains two materially different implementations
that both use the words "Grand Final" and "SuperScore." Confusing them would
misdirect every follow-up issue, so this distinction is stated explicitly
and is treated as settled fact for this design, not a decision left open:

### 1. The legacy Grand Final/SuperScore HTTP vertical (prototype era)

`app/teams.py`, `app/superscore.py`, `app/routes/superscore.py`,
`app/routes/public.py`'s `legacy_grand_final_page`, `app/service.py`'s
`build_matchup_state`/`build_superscore_state`, and `app/scorer_decisions.py`
implement a single configured two-team "Grand Final" and an optional
one-round SuperScore leaderboard, using **checked-in JSON team config**
(`data/grand_final_teams.json`/`data/superscore_teams.example.json`) and the
legacy `DecisionsRepository` SQLite tables keyed by a fixed
`competition_key` (`"grand_final"` or `"superscore:<season>:<round>"`; see
`app/db.py`). `docs/architecture.md` explicitly classifies this as its own
vertical, deliberately left alone while the season model was built around
it, and `docs/season-competition-schema.md`'s "Prototype compatibility"
section is explicit that its rows are "preserved byte-for-byte" and "**not**
claimed or backfilled as new competition streams" because "those keys lack
enough trustworthy parent identity."

**This design does not build the 2026 replay's finals/SuperScore on this
vertical.** It has no season/competition-stream identity, no persisted
lineup history, no lockout plan, no audit-attributed actor, and no ladder
integration — none of which the 2026 replay's finals/SuperScore can do
without. It remains valuable only as *UI/UX evidence* (`docs/plans/
2027-season-model.md` calls the "2026 Grand Final and SuperScore trial
interfaces" a "successful starting point") for how a matchup/leaderboard
view can be presented, and its route/JSON shapes are reasonable prior art
for the new views' response shapes.

### 2. The season model (what actually produced Rounds 1-20)

`app/season.py`, `app/identity.py`, `app/player_pool.py`, `app/fixtures.py`,
`app/round_mapping.py`, `app/competition_lifecycle.py`, `app/lineups.py`,
`app/lockouts.py`, `app/calculations.py`, `app/round_review.py`,
`app/ladder.py`, `app/carry_forward.py`, `app/lineup_proxy.py`,
`app/midseason_draft.py`, `app/finals_seeding.py`, and
`app/replay_continuation.py` are the UUID-keyed, season-scoped, audited
repositories that actually ran the completed 2026 replay. **This is the only
foundation this design builds on.** Every follow-up issue below is scoped
in terms of these modules and their existing patterns (frozen snapshots,
append-only audit events, fail-closed context checks, actor/reason on every
mutation, CLI-first operator tooling before any UI).

The schema already anticipated this split: migration `0004_season_
competition.py`'s `competition_stream` table has always had
`CHECK (stream_type IN ('ordinary', 'finals', 'superscore'))` — finals and
SuperScore were named as first-class stream types from the very first season
migration, years before either had implementing code. **No migration is
needed to introduce these stream types.** What is missing is the code that
creates and operates rounds within a `finals`/`superscore` stream:
`CompetitionLifecycleRepository.create_ordinary_round` explicitly rejects
any `stream_type != "ordinary"`, and it reads its five matchup pairs from
`season_fixture_matchup` — the frozen, fixed round-robin fixture draw. That
table has no meaning for finals (pairings depend on results, not a
pre-drawn fixture) or SuperScore (there is no head-to-head pairing at all).
**A finals bracket/lifecycle and a SuperScore round/lifecycle are therefore
genuinely new abstractions, not a parameter change to the ordinary
lifecycle** — this is why the follow-up decomposition below gives each its
own issue rather than trying to widen `create_ordinary_round`.

## A foundational schema fork found by Codex review, resolved by a new prerequisite issue

**This section was added after Codex review of PR #196 found that the
original decomposition's "reuse `app.lineups`/`app.lockouts`/`app.
calculations`/`app.round_review` unchanged, only stream-scoped" claim was
wrong at the schema level for both finals and SuperScore, not just for
SuperScore's scoring as first identified.** Verified directly against the
migration and the repository code, not merely against the design's own
earlier prose:

- `bbbffl_round_lifecycle` (migration `0010_competition_lifecycle.py`)
  declares `fixture_draw_id`, `fixture_draw_version`, and
  `fixture_round_number` all `nullable=False`, foreign-keyed to `season_
  fixture_draw` — the fixed, pre-drawn round-robin fixture. A finals or
  SuperScore round has no fixture-draw row to reference at all.
- **This blocks lineup submission itself, not merely scoring.**
  `app.lineups.WeeklyLineupRepository._finalize_submission` — the single
  core function every submission source (coach, carry-forward, proxy,
  correction, adjudication) goes through — reads `bbbffl_round_lifecycle`'s
  `state` and raises `LineupIntegrityError` if no row exists. Without
  resolving this, **no finals or SuperScore lineup can ever be submitted at
  all**, regardless of anything else this design or its follow-up issues
  build. Every earlier claim in this document that "lineup submission and
  lockout are reusable unchanged, only stream-scoped" is true only once
  this is resolved — it is a shared prerequisite, not an implementation
  detail internal to #191 or #192.
- The identical shape of problem recurs one level down, for finals
  specifically: `bbbffl_matchup`'s `fixture_matchup_id` is likewise
  `nullable=False`, foreign-keyed to `season_fixture_matchup`, which is why
  `app.calculations`/`app.round_review`'s matchup-keyed methods can't be
  called against a finals match either (SuperScore has no matchups at all
  by design, so only the round-lifecycle layer above applies to it).

**[Issue #197](https://github.com/JustPlausible/BBBFFL_Scoring/issues/197)**
is a new prerequisite issue, not present when this decomposition was first
written, that resolves this once for both finals and SuperScore rather
than letting #190 and #192 each independently reinvent (or silently
diverge on) the same decision. It offers two paths — loosening the
fixture-draw linkage to optional for non-ordinary streams, or building
parallel lifecycle/matchup storage with a dispatching lookup (a deeper
change, since that means `app.lineups`/`app.lockouts` are not, in fact,
left byte-for-byte unchanged) — and leaves the choice, with its reasoning,
to whoever implements it.

**If path 2 is chosen, the dispatching lookup must also cover `app.
lineup_adjudication`, not only `app.lineups`/`app.lockouts` — correction
(Codex review, PR #196, ninth round), verified directly against `app/
lineup_adjudication.py`.** `LineupAdjudicationService._eligibility` —
which the cross-stream carry-forward fallback extension (see "Coach
lineup/submission behaviour" and "SuperScore design" below) depends on —
independently queries `SELECT state FROM bbbffl_round_lifecycle WHERE
bbbffl_round_id=?` and treats a missing row as `round_state = "unknown"`,
which then fails its own `round_state not in ("live", "review")` check and
refuses the adjudication outright. Under path 2 (no `bbbffl_round_
lifecycle` row for finals/SuperScore rounds at all), this lookup would
always see "unknown" and always refuse — meaning the entire cross-stream
fallback mechanism #191/#192 need would be unreachable, independent of
whatever `app.lineups`/`app.lockouts` dispatching #197 built. **#197's
path-2 scope must include making this specific lookup dispatch-aware too**
(or #191/#192 must provide their own adapter in front of it) — do not
assume changing only `app.lineups`/`app.lockouts` is sufficient.

**Correction (Codex review, PR #196, sixth round): making the fixture-draw
columns nullable is not, by itself, sufficient for path 1 — verified
directly against `app/competition_lifecycle.py`.**
`CompetitionLifecycleRepository.transition`'s `upcoming -> open` step (a
transition every round, finals/SuperScore included, must go through) calls
`_validate_frozen_context`, which reads `season_fixture_draw WHERE
fixture_draw_id=?` using the round's own `fixture_draw_id` and raises
`ValueError("frozen fixture context changed; round remains closed")` when
that lookup finds no row — exactly what happens when `fixture_draw_id` is
null. A finals or SuperScore round created under path 1 would therefore
still be permanently stuck at `upcoming`, unable to ever open, unless
`_validate_frozen_context` is *also* made stream-aware: skip the
fixture-draw check when `fixture_draw_id` is null, while still performing
its other check (the accepted AFL-round-mapping revision, which stays
meaningful regardless of stream type). **Path 1's scope in #197 therefore
includes this lifecycle-transition change, not only the column-nullability
migration** — describing it as a schema-only change understates the work.

**#190 and #192 both now depend on #197's outcome**, which changes their
"Depends on" relative to the original decomposition below.

Every place elsewhere in this document that says a module is "reusable
unchanged" for finals or SuperScore lineup submission/lockout should be
read as "reusable unchanged once #197 lands" — restated here once rather
than qualified at every occurrence.

### A wider pattern: `stream_type='ordinary'` is hard-coded across the application, not just in the modules already named

**Added after the ninth and tenth Codex review rounds each found another
independent call site with the identical shape (`LineupAdjudicationService.
_eligibility`; `app/routes/lineup_adjudication.py` and `app/routes/
lineup_correction.py`'s shared `_authorise_round`), and confirmed directly
by grepping the repository rather than waiting for an eleventh.** This
document's earlier framing — "#197 resolves the schema fork, then #190-
#193 build on top of it" — understated how many *existing* call sites
assume "ordinary" is the only real stream and will need their own
adaptation regardless of #197's schema choice, because they filter or
branch on `stream_type` themselves rather than merely relying on the
schema constraints #197 touches. A repository-wide search
(`grep -rn "stream_type.*ordinary"` under `bbbffl_app/app/`) found, at
minimum:

- `app/round_preflight.py` — `open_preflight_round` calls `create_
  ordinary_round` directly, and `build_round_preflight` adds a hard
  `fixture_invalid` blocker unless it finds exactly five frozen fixture
  matchups. **The operator preflight/open-round workflow itself cannot
  open a finals or SuperScore round**, independent of #197's storage
  choice — #190/#192 need a stream-aware preflight/open adapter (and
  `app/routes/round_preflight.py`'s own `stream_type='ordinary'` filter
  needs the same treatment).
- `app/coach_lineup.py` — `CoachLineupService.list_rounds` and `resolve`
  both filter `stream_type='ordinary'` explicitly, and `_opponent` reads
  the fixed fixture draw. **The authenticated coach-facing lineup
  submission route 404s for both new streams even once #197 lands** —
  #191/#192 need a stream-aware coach service/route, including bracket-
  derived finals opponents and matchup-free SuperScore presentation
  (this may reasonably be the same views work #193 already owns for
  SuperScore's public/coach/Scorer surfaces, rather than a fourth
  implementation).
- `app/scorer_dashboard.py` and `app/admin_dashboard.py` similarly select
  "the ordinary competition" for their round listings/dashboard displays —
  a finals/SuperScore round would not appear in the Scorer's or admin's
  dashboard without an equivalent addition.
- `app/public_rounds.py`'s `_ordinary_competition_id` scopes the public
  round/ladder view the same way — expected, since #191/#193 already plan
  their own public finals/SuperScore views separately; noted here only so
  it isn't mistaken for a gap to "fix" in `app/public_rounds.py` itself.
- `app/routes/delegated_operations.py` has the identical `_authorise_
  round`-shaped `stream_type='ordinary'` filter as the adjudication/
  correction routes above, for delegated-scorer operations — the same
  adaptation-or-sibling-route treatment applies if delegated operators need
  to act on finals/SuperScore rounds.
- **Not gaps, checked and excluded deliberately:** `app/round_mapping.py`'s
  `bbbffl_stream_type != "ordinary"` guard only disables *automatic* AFL-
  round-mapping recommendation for non-ordinary streams — manual mapping
  (which #190/#192 already require) is unaffected, so this is a correctly-
  scoped guard, not a gap. `app/replay_bootstrap.py`/`app/replay_
  continuation.py`'s ordinary-only scoping is correct too: those modules
  bootstrap/continue specifically the *ordinary* competition's history,
  which is exactly their job; finals/SuperScore get their own separate
  bootstrap through #190/#192, not through these modules.

**This list is not guaranteed exhaustive — it is what a repository-wide
grep found as of this document's tenth-round revision, not a formally
verified closed set.** Rather than let an eleventh, twelfth, and
thirteenth Codex review round each surface one more independent site one
at a time, **#190, #191, #192 and #193 must each re-run an equivalent
search scoped to their own domain at the start of their own work** (coach-
facing surfaces for #191/#192; Scorer/operator/admin surfaces for #190-
#193; public views for #191/#193) and adapt every relevant call site they
find, rather than treating the list above as complete.

### Confirmed bracket structure

`docs/plans/2027-season-model.md`'s "Finals" section and `docs/plans/
2026-workbook-findings.md`'s "Finals structure" section (cross-checked
against five years, 2021-2025, of scorer workbooks, plus the actual 2026
workbook) agree exactly. This is a **confirmed historical rule**, not an
inference:

- Top 5 (by finals seed) qualify. 6th-10th are eliminated at the end of the
  home-and-away season.
- **Week 1:** 1st seed has a bye. Qualifying Final: 2nd v 3rd. Elimination
  Final: 4th v 5th. Elimination Final loser is eliminated.
- **Week 2:** Second Semi-Final: 1st v Qualifying Final winner. First
  Semi-Final: Qualifying Final loser v Elimination Final winner. First
  Semi-Final loser is eliminated.
- **Week 3:** Preliminary Final: Second Semi-Final loser v First Semi-Final
  winner. Winner advances to the Grand Final.
- **Week 4:** Grand Final: Second Semi-Final winner v Preliminary Final
  winner.
- A tied finals match (including the Grand Final) is won, for progression
  purposes, by the team that finished higher on the locked finals seed,
  subject to scorer confirmation.

With the 2026 finals seed already resolved (`resolve_finals_seed_order`),
the top five are: Running Hots (1), Bridesmaids (2), JHAS (3), Wolverines
(4), Evil Absolutes (5). Seeds 6-10 (The Crabs, One Percenters,
Motherruckers, Pommy Rules, The Plague) are eliminated and become SuperScore-
only participants for the remainder of the season.

### Seed consumption

The finals bracket module resolves the seed order **exactly once, at
bracket creation**, and freezes both the order and its exact provenance
into the persisted `finals_bracket` row. **Correction (Codex review,
PR #196, fifth round): an earlier fix here still recommended calling
`resolve_finals_seed_order` and then *separately* re-calling `LadderRepository.
snapshot` afterward to recover provenance — two independent calls that can
observe two different ladder states if a result correction lands between
them, and even then `through_round`/`latest_included_round` alone don't
identify the exact official-result versions the order was actually derived
from (`LadderSnapshot.result_references`, the `(matchup_id, official_
version)` pairs, does).** The correct shape is one read, not two:
1. Call `app.finals_seeding.FinalsSeedingRepository.get_snapshot(season_id)`
   first. **Retain `resolve_finals_seed_order`'s own fail-closed check
   here, not just the happy path (Codex review, PR #196, sixth round):**
   a non-`None` result is only usable if `snapshot.competition_id ==
   competition_id` (the ordinary `competition_id` this bracket was given) —
   `get_snapshot` itself only takes `season_id`, so this equality check is
   what stops a season that happens to carry a snapshot scoped to some
   other competition from being silently accepted here. If it returns a
   snapshot **and** that check passes, use its order and record its
   `snapshot_id` as provenance — done, no ladder involved at all. If it
   returns a snapshot whose `competition_id` does *not* match, treat this
   exactly as "no snapshot" and fall through to step 2 (mirroring
   `resolve_finals_seed_order`'s own behaviour) rather than raising.
2. Only if no matching snapshot exists, **first verify every configured
   regular-season round is actually `final`** — correction (Codex review,
   PR #196, ninth round), verified directly against `app/ladder.py`.
   `LadderRepository.snapshot`'s query filters to `l.state='final' AND
   l.fixture_round_number<=?` and **silently returns whatever subset of
   rounds already happens to be final** — it does not check that the
   season's full regular-season round count (`through_round`, normally 20)
   has actually been reached. Calling it before every round is final would
   silently freeze a seed order computed from an incomplete ladder, with
   no error at all. Perform the same completion check `app.finals_seeding.
   _require_replay_context` already does for the snapshot-creation path
   (every round from 1 through the season's `regular_season_round_count`
   is present and `state == 'final'`) before proceeding to the ladder read
   below — fail closed, do not proceed, if it is not. Only once that
   passes, call `app.ladder.LadderRepository.snapshot(competition_id,
   through_round)` **once** — this single
   `LadderSnapshot` object is both the source of the seed order (its
   `rows`, after checking for `tied` exactly as `resolve_finals_seed_
   order`'s own fallback path does, raising `UnresolvedLadderTieError` on a
   genuine tie) *and* the source of its exact provenance (persist its
   `result_references` — every `(matchup_id, official_version)` pair that
   produced it — alongside `through_round`/`latest_included_round`, not
   those two round numbers alone). **Retain the second fail-closed check
   here too:** `resolve_finals_seed_order` verifies `ladder.season_id ==
   season_id` and raises `FinalsSeedingContextError` otherwise, because
   `LadderRepository.snapshot` derives its own season solely from
   `competition_id` and never checks the caller's `season_id` against it —
   without this check, a wrong-but-plausible `competition_id` could freeze
   a different season's ladder into this bracket. Perform the identical
   check on this single read and raise the same way.

**A further correction (Codex review, PR #196, fourteenth round): the
single read from step 2 above is still not coupled to the transaction that
persists `finals_bracket`, so a supported ordinary-result correction can
commit in the gap between them.** The single-read fix above closes the race
between *two separate reads* (an earlier draft of this design called
`resolve_finals_seed_order` and then separately re-called `LadderRepository.
snapshot`); it does not, by itself, close the race between that one read
and the later write. If a Scorer corrects an already-final regular-season
result after step 2's `LadderRepository.snapshot` call returns but before
the bracket-creation transaction commits its `INSERT`, the bracket
permanently freezes a seed order — and a `result_references` provenance
record — that was already superseded at the moment it was written.
Persisting the stale `result_references` makes this traceable after the
fact; it does not prevent it. **The bracket-creation transaction must
therefore re-verify every captured `(matchup_id, official_version)`
reference — e.g. against `bbbffl_matchup.effective_official_version` — for
that exact set of matchups, inside the same transaction that inserts
`finals_bracket`, and abort (for the caller to retry step 2 from scratch)
if any has changed**, the same compare-and-swap discipline `app.carry_
forward.CarryForwardService.carry_forward` already applies to a carried-
forward lineup's source submission via `require_unchanged`.

**A further correction (Codex review, PR #196, fifteenth round): moving
that re-check inside the transaction is not, by itself, atomic unless the
re-checked rows are actually locked, not merely re-read — verified directly
against `app/lineups.py`'s own implementation of `require_unchanged`.**
Under PostgreSQL's default READ COMMITTED isolation, a plain `SELECT`
re-check inside the bracket-creation transaction can still observe the
pre-correction version and then have a concurrent `correct_matchup_result`
acquire its own row lock, update, and commit — all before the bracket's own
`INSERT` commits — leaving the bracket frozen against a version that is
already stale by the time it is persisted, exactly as before this fix.
`require_unchanged`'s own implementation is not merely "the same
discipline" in the abstract; it is safe specifically because it locks the
source row with `SELECT ... FOR UPDATE` (via `app.db._for_update_suffix`)
before comparing its version, not because it re-reads inside a transaction
per se. The bracket-creation transaction must do the same: `SELECT ...
FOR UPDATE` every captured matchup row, in a deterministic order (e.g.
sorted by `matchup_id`, to avoid a deadlock against another concurrent
locker of the same rows), *before* comparing each one's current version
against its captured `result_references` entry — a plain unlocked
re-`SELECT` does not close this race, only an explicit lock does. `app.
midseason_draft.confirm_ladder` (which this design otherwise mirrors for
the "freeze an independent copy" pattern) does not re-verify result
freshness this way either — it only re-checks the season's trigger-round
*configuration* between its two transactions, not the ladder's own result
versions, and even that re-check is a locked re-read (`_for_update_suffix`
on `bbbffl_season`) rather than a plain one — so it is not a sufficient
precedent to copy for this specific check; fixing that gap in
`confirm_ladder` itself is existing ordinary-season code and out of scope
for this document. This requirement applies only to the ladder-fallback
path (step 2); the snapshot path (step 1) reads an already-immutable
`FinalsSeedingRepository` snapshot at bracket-creation time, so
no live result can go stale underneath it.

This means the finals bracket module does not call `app.finals_seeding.
resolve_finals_seed_order` as an opaque black box for the no-snapshot case;
it reimplements that one small fallback step (snapshot-driven order plus
tie check) inline against a single ladder read, precisely so the order and
its provenance always describe the *same* read rather than two. (The
snapshot-exists case is unaffected — it was already a single, consistent
`get_snapshot` call.) Persist whichever provenance applies onto `finals_
bracket` alongside the frozen order, mirroring `app.midseason_draft.
confirm_ladder`'s "freeze an independent copy" pattern for the exact same
reason. **Every later
decision that needs the seed order — tie-break resolution in a later week,
a display, an audit payload — reads the bracket's own frozen seed, and never
calls `resolve_finals_seed_order` again for this bracket.** This matters
because, for a season with no finals-seeding snapshot (any season other
than this 2026 replay), `resolve_finals_seed_order` recomputes the
mathematical ladder fresh on every call; if a supported post-final
home-and-away result correction changed that ladder after Week 1's bracket
was already built and played, a second, later call could return a
different order than the one the bracket actually used — silently
desynchronising a tie-break decision in Week 3 from the pairings Week 1
was actually built from (Codex review, PR #196). Never hard-code the 2026
team names or IDs; this is what keeps the same bracket-generation code
correct for a future season that has no seeding snapshot at all.

**Which `competition_id` to pass matters and is easy to get wrong.**
`resolve_finals_seed_order(database, season_id, competition_id)` — and the
single-read mechanism above, which applies the exact same logic — resolves
the snapshot (or the mathematical-ladder fallback) *for that
`competition_id`* — and the 2026 finals-seeding snapshot is scoped to the
**ordinary** home-and-away `competition_id` (`9de8e7d3-8d56-4c5c-
afd0-803b787e4055`, per `provenance-manifest.md`), because that is the
`competition_id` `FinalsSeedingRepository.apply` was called against. A new
`finals`-typed `competition_stream` is a *different* `competition_id`.
Resolving with the finals `competition_id` instead of the ordinary one will
not find the snapshot (it is scoped to a different ID) and will silently
fall through to computing a mathematical ladder *for the finals
competition* — which has no Round 1-20 results at all, since those live
under the ordinary stream. **The finals bracket module must therefore
retain and pass the ordinary home-and-away `competition_id` into the
single-read mechanism above, separately from the new `finals`
`competition_id` it creates its own rounds/rulings under.** This is not an
edge case to discover during implementation; #190's acceptance criteria
must test it explicitly (Codex review, PR #196).

### Match generation/representation — a new finals bracket abstraction

A new module (recommended name: `app.finals`, mirroring `app.midseason_
draft`'s shape) should own:

- **`FinalsBracketRepository`**: one `finals_bracket` row per `(season_id,
  competition_id)` where `competition_id` is the `finals`-typed
  `competition_stream` (a distinct ID from the ordinary home-and-away
  `competition_id` used for seed resolution — see above). Materialises
  Week 1's three fixed slots (1st seed's bye, Qualifying Final 2nd v 3rd,
  Elimination Final 4th v 5th — **two matches, not one, plus a bye that is
  not a match at all**) directly from the resolved seed order — this part
  never depends on any result. Week 2 (Second Semi-Final 1st v QF winner,
  First Semi-Final QF loser v EF winner — **also two matches**) and Week 3
  (Preliminary Final — one match) are **not** known at bracket creation;
  each is computed from the *previous* week's finalised result(s) plus the
  frozen seed. Week 4 (the Grand Final) is one match. This variable match
  count per week (0 matches for the bye, otherwise 1 or 2) is the concrete
  reason a fixed fixture draw (`season_fixture_matchup`, what `create_
  ordinary_round` reads, which always has exactly five) cannot represent
  finals: both the pairing itself for weeks 2-3 *and* the number of matches
  in a week depend on results, not a pre-drawn fixture (Codex review,
  PR #196).
- A **finals round lifecycle** analogous to `bbbffl_round_lifecycle`
  (`upcoming -> open -> live -> review -> final`), but the transition from
  "final" that would open the *next* week's round should be the one place
  that actually computes and persists the next week's pairing — an
  explicit, audited "advance bracket" step, not an implicit side effect of
  publication, so an operator/Scorer can inspect and confirm the derived
  pairing before it becomes visible (mirroring `app.round_review.
  attempt_signoff`'s "the scorer confirms, never the software alone"
  convention already used for tied-finals progression).
- The **tied-finals rule** ("higher seed wins for progression") is a
  deterministic recommendation from the frozen seed order, but the actual
  historical convention is that the scorer confirms it (see "Historical
  gaps" below for the one open question about *which* seed number a tie
  resolves against).

**The same locked-re-check discipline the bracket-creation transaction
needs for the ladder read (see "Seed consumption" above) applies equally to
this "advance bracket" step — Codex review, PR #196, sixteenth round.**
"Advance bracket" reads a prerequisite match's official result and derives
next week's pairing from it; if a Scorer correction to that exact
prerequisite match races the advance step — landing after the read but
before the derived-pairing write commits — the persisted pairing would
describe an already-superseded winner, without ever entering the
already-advanced-correction handling below (because, from the correction's
point of view, the bracket had not advanced yet when the correction
committed). The advance-bracket transaction must therefore `SELECT ...
FOR UPDATE` the prerequisite matchup row(s) it is reading — the same
locking discipline as the bracket-creation transaction, not a plain
re-read — and verify their effective official-result version while
persisting the derived pairing in that same transaction, aborting for the
caller to retry if the version has changed underneath it. The finals
correction command (see "Finals result publication" and "Handling of
corrections" below) must serialize against the identical row lock, so a
correction and an in-flight advance can never both believe they are
working from the same, now-stale result.

### Team naming and matchup presentation

Reuse `app.identity.IdentityRepository`'s existing public season-entry/team-
name projection (`season_entry_team_name_history`) exactly as the ordinary
Round Centre does — no new team-naming concept. Bracket display should show
seed number alongside team name (e.g. "1. Running Hots"), matching the
workbook's own presentation convention.

### Round lifecycle and lockout

Reuse `app.lockouts.LockoutTriggerRepository`/`LockoutRepository` once
#197 has supplied the stream-aware lifecycle lookup described above (whether
that leaves these repositories unchanged under path 1 or requires the path-2
dispatch adapter).
A finals round's lockout plan is configured exactly the same way an
ordinary round's is (selective/main triggers against real AFL match IDs) —
nothing about staged lockout is specific to the ordinary stream. The
existing `LockoutRepository.guard(...)` duck-typed collaborator threads
straight through `WeeklyLineupRepository.submit_positions` regardless of
which `competition_id` the round belongs to.

### Coach lineup/submission behaviour

Reuse `app.lineups.WeeklyLineupRepository` unchanged (once #197 lands — see
above), scoped to the `finals` competition stream's rounds. The
nine-position structure, private-draft-vs-submit distinction, and
resubmission-while-unlocked rules are identical to an ordinary round.

**Eligibility is not enforced by `WeeklyLineupRepository` itself and must
be enforced by the finals module (Codex review, PR #196).**
`WeeklyLineupRepository._validate_scope` only checks that the competition,
round, and entry belong to the same season — it has no concept of "is this
entry actually one of this specific finals match's two participants" or
"is this entry even still in the top five". Left unenforced, an eliminated
seed (6th-10th) could submit a lineup against a finals round it has no
business playing in, with nothing beyond route-level visibility stopping
it. The finals module (#191) must validate the submitting entry against
its own bracket's known participants for that specific round *before*
calling into `WeeklyLineupRepository`, rather than relying on
`WeeklyLineupRepository`'s generic same-season scope check alone.

**Failure-to-submit carry-forward is a confirmed rule, not an open
question — correction to an earlier draft of this document.**
`docs/plans/2027-season-model.md`'s "Failure to submit" section states
that for "the normal home-and-away competition and continuing finals
participation," the default carry-forward source "normally means the
coach's previous premiership/home-and-away lineup" — using "premiership"
as the same umbrella term the document elsewhere uses for "ordinary
home-and-away plus finals, as opposed to SuperScore" (see its "All-time
record scope" section). Read together with the immediately following,
structurally identical SS1 rule ("derived from that coach's most recently
named ordinary BBBFFL team"), the intended mechanical rule for finals is:
carry forward from the entry's most recent **finals-stream** submission
when one exists (the common case, Week 2 onward for a team that played and
submitted the previous week) — this is exactly `app.carry_forward`'s
existing same-`competition_id` behaviour, unchanged — and fall back to the
entry's most recent **ordinary** submission (cross-stream) only when no
finals-stream predecessor exists at all: every finalist **who actually
plays a Week 1 match** (seeds 2-5; seed 1's bye has no match and therefore
no lineup requirement at all), and seed 1 specifically again in Week 2
(their only finals-stream history at that point is the bye, which has no
submitted lineup of its own). **Correction (Codex review, PR #196,
twenty-fourth round): the Week 1 fallback must not be applied to seed 1.**
Seed 1 has no Week 1 match and must not be given an artificial Week-1
finals-stream submission — if it were, `CarryForwardService.resolve_
source`'s same-stream lookup (which simply finds the most recent BBBFFL
round with a non-null `effective_submission_version` in the same
`competition_id`) would find that phantom Week-1 row as seed 1's "most
recent finals-stream predecessor" for Week 2, silently skip the required
cross-stream fallback to Round 20, and carry forward the wrong (and
possibly stale, if the ordinary Round 20 lineup is later corrected)
provenance and content. Scope the Week 1 fallback strictly to entries with
an actual Week 1 pairing. This is the *same* cross-stream mechanism
SuperScore's SS1 needs (see "SuperScore design" below) — #191 and #192
should share one narrow cross-stream fallback function rather than each
building their own, and #191's implementation should treat this as a
confirmed requirement to build, not an unresolved question to raise with
Steve.

**This fallback must be reachable through the real, supported post-lockout
path, not just exist as a bare standalone function — correction (Codex
review, PR #196, fourth round).** By the time a fallback is actually
needed (a coach genuinely missed the deadline), the round's lockout has
already activated, so an ordinary `submit`/`submit_positions` call would
simply be rejected by the lock guard — that is exactly why `app.
lineup_adjudication.LineupAdjudicationService.apply_carry_forward_fallback`
exists as the supported authority for this situation.

**A naive `fallback_source_competition_id` parameter on `resolve_source`
itself does not work — second correction (Codex review, PR #196, fifth
round), verified directly against `app/carry_forward.py`.**
`CarryForwardService.resolve_source(season_id, competition_id,
bbbffl_round_id, season_entry_id)` first requires the **target**
`bbbffl_round_id` to belong to that same `competition_id` (`SELECT sequence
FROM bbbffl_round WHERE bbbffl_round_id=? AND competition_id=?` — raises
`LineupIntegrityError` otherwise), then finds the source lineup by
comparing `r.sequence < target["sequence"]` **within that one
competition**. For a finals-Week-1 (or SS1) target round, simply swapping
in the ordinary `competition_id` breaks the first check outright (the
target round belongs to the *finals*/*superscore* stream, not the
ordinary one) — and even if it didn't, `sequence` numbers restart
independently per stream (finals Week 1 and SS1 are both sequence 1;
ordinary Round 20 is sequence 20), so comparing them across streams is
meaningless, not merely differently-scoped.

**The correct shape is a distinct resolution function with separate target
and source scopes, not a parameter threaded through the existing
same-stream lookup.** For exactly the confirmed cross-stream cases (finals
Week 1 and seed 1's Week 2; SuperScore SS1), the "source" is unambiguous
and does not need a sequence comparison at all: it is simply *the entry's
most recent submitted lineup in the ordinary competition* (highest
`sequence` with a non-null `effective_submission_version`) — there is
nothing to compare it against, since the target round has no comparable
position in that sequence space. #191/#192 should add a small, separate
function (e.g. `resolve_cross_stream_fallback_source(database, season_id,
source_competition_id, season_entry_id)`, alongside — not inside —
`app.carry_forward`) that performs exactly this one lookup, and extend
`LineupAdjudicationService.apply_carry_forward_fallback`/its preview with
an optional parameter that, when the confirmed cross-stream case applies,
uses this function's result **in place of** (not in addition to)
`self._carry_forward.resolve_source`'s same-stream call — while preserving
`apply_carry_forward_fallback`'s existing atomic `require_unchanged`
re-validation against whichever source it resolved, exactly as the
same-stream case already does. Every other caller's behaviour (the
default, no cross-stream case indicated) must remain byte-for-byte
unchanged.

**Extending the service layer is still not enough on its own — a Scorer
has no way to reach it, correction (Codex review, PR #196, tenth round),
verified directly against `app/routes/lineup_adjudication.py`.**
`_authorise_round`, the shared authorization helper every adjudication
route calls first, hard-filters `WHERE ... c.stream_type='ordinary'` and
raises `HTTPException(404, "Unknown ordinary BBBFFL round")` for anything
else — a finals or SuperScore round is rejected at the route layer before
the request ever reaches `LineupAdjudicationService`, regardless of how
correctly that service was extended above. **#191/#192 must also adapt
(or add sibling) HTTP routes**: widen `_authorise_round`'s stream filter
to accept `finals`/`superscore` (with its per-round participant listing,
currently derived from matchup rows, adapted for SuperScore's matchup-free
shape), or provide dedicated finals/SuperScore adjudication routes. Without
this, a Scorer applying the confirmed Week-1/seed-1/SS1 fallback in
production has no reachable endpoint at all.

**Widening `_authorise_round`'s stream filter alone would reopen the
bracket-eligibility hole this document already requires closed elsewhere
— correction (Codex review, PR #196, twelfth round), verified directly
against `app/routes/lineup_adjudication.py`.** The route's separate
`_authorise_entry` helper — called for every entry-targeting request —
only checks `entry.season_id == scope["season_id"]`; it has no concept of
"is this entry actually one of this bracket round's legitimate
participants," and neither does `LineupAdjudicationService` itself. Once
`_authorise_round` is widened to accept finals rounds, a request naming an
eliminated seed (6th-10th) directly would pass every check this route/
service currently performs and could create or correct a finals lineup
for a team with no business playing that round. **This is the same
bracket-participant eligibility check "Coach lineup/submission behaviour"
above already requires for ordinary submission — it must be applied at
this entry-authorization layer too**, not only in the plain `submit`/
`submit_positions` path, preferably inside the domain service (so it
covers every route that reaches it) rather than duplicated per-route.

### Scoring

The scoring *formulas* are unchanged — the same nine-position `app.scoring`
core every stream uses. Two different layers of `app.calculations`/`app.
round_review` behave differently here, and conflating them was an earlier
draft's mistake:

- **Matchup-level calculation and ruling storage** (`MatchupCalculationService.
  calculate_matchup`, `RoundReviewRepository.record_dnp_ruling`/
  `record_interchange_ruling`/`record_override`, all keyed by `matchup_id`)
  depends entirely on which path #197 chooses for the `bbbffl_matchup.
  fixture_matchup_id` constraint: if #197 loosens the schema, these are
  genuinely reusable unchanged; if #197 builds parallel storage, finals
  needs its own calculation/ruling path analogous to SuperScore's (see
  "SuperScore design" below).
- **Round-level review/sign-off is a separate, unconditional gap —
  correction (Codex review, PR #196, seventh round), verified directly
  against `app/round_review.py`.** `build_round_review` hard-codes `if
  len(reviews) != 5: round_blockers.append(...)`, and `attempt_signoff`'s
  own docstring states it publishes "if every one of the five matchups is
  ready." **This is unconditional on #197's choice** — even if #197 loosens
  the schema so finals matches populate ordinary `bbbffl_matchup` rows,
  `build_round_review`/`attempt_signoff` still refuse any round that
  doesn't have exactly five matchups, and every finals week has one or two.
  Finals therefore needs its own review/sign-off adapter (variable-match-
  count aware) regardless of which path #197 takes — it can still call the
  same underlying matchup-level ruling methods (once #197 resolves whether
  those are reusable unchanged or need the parallel path), but it cannot
  call `build_round_review`/`attempt_signoff` themselves. See "Finals
  result publication" below, which already reaches a compatible conclusion
  for the write side; this extends the same reasoning to the read/
  readiness side.

This document does not pick between #197's two paths for the matchup-level
layer — that is #197's decision — but #191 must build the round-level
adapter regardless of that choice, and must build the matchup-level layer
on whichever shape #197 actually produced.

Whichever path #197 takes, nothing about the DNP/Interchange/override
*rules* changes for finals — only where the resulting rulings/calculated
scores are stored, and (per above) how round-level readiness/sign-off is
computed.

**If #197 chooses parallel storage, the finals adapter also needs its own
correction-invalidation hook — a conditional case this document's earlier
fix only stated for SuperScore, corrected after further Codex review of
PR #196 (fifth round) found the same gap recurs here.**
`WeeklyLineupRepository.submit_correction`'s `_invalidate_stale_review_
state` queries and bumps only `bbbffl_matchup` and clears only the
existing matchup-keyed ruling tables. If finals matches live in a parallel
table instead of `bbbffl_matchup` (the path-2 outcome from #197), a finals
lineup correction after a DNP/Interchange/override ruling has been
recorded would not invalidate that ruling either — the identical
correctness gap "SuperScore design" below describes, just conditional on
which path #197 took rather than unconditional. If and only if #197 chose
parallel storage, #191's finals adapter must clear changed-slot rulings
and advance its own CAS revision inside the same correction transaction,
mirroring `_invalidate_stale_review_state`'s existing behaviour for the
ordinary case. If #197 instead loosened the schema, this is unconditionally
covered already: an ordinary `bbbffl_matchup` row exists and `_invalidate_
stale_review_state` handles it exactly as it does for an ordinary round.

### Progression and elimination

New, as described above under "Match generation/representation": the
finals bracket module derives each week's pairing from the previous week's
official result plus the frozen seed, records eliminations explicitly
(`finals_bracket_elimination` or equivalent), and never infers elimination
from the absence of a future pairing.

### Finals result publication

Reuse the existing publish/correction *shape* from `app.competition_
lifecycle.CompetitionLifecycleRepository.publish_results` (one round
transition, one transaction, versioned official results protected by
immutability triggers, post-final correction requires a reason and appends
a new version) — but a finals week has a *variable* number of matches (two
in Week 1, two in Week 2, one in Week 3, one in Week 4 — never a fixed five,
and never reliably one), so the finals bracket module needs its own publish
command that accepts however many matches are actually present in that
week and publishes all of them atomically in one transaction, rather than
either literally calling the ordinary five-matchup method or assuming
exactly one match (Codex review, PR #196). Week 1's bye is not a match and
publishes nothing of its own for seed 1.

**This new command must also freeze the same scoring-input snapshot the
ordinary sign-off path freezes, not merely reuse `publish_results`'
versioning/immutability shape — Codex review, PR #196, eighteenth round,
verified directly against `app/round_review.py` and `app/competition_
lifecycle.py`.** `_freeze_matchup_inputs` (rules version, calculation
revision/fingerprint, both sides' lineup/DNP/interchange/override state,
who/when finalised it) is called by `attempt_signoff`/`attempt_correction`,
*not* by `publish_results` itself — `publish_results`'s `input_snapshots`
parameter is optional and defaults to `None`. Because the finals-specific
adapter this section requires necessarily replaces `attempt_signoff` (see
below), simply calling into `publish_results` with the reused *shape* and
forgetting to also assemble and pass an equivalent `input_snapshots` payload
would silently publish immutable finals scores with no record of the
lineup/calculation/ruling state that produced them — true regardless of
which #197 path is chosen, and not obviated by it. The finals publish and
correction commands must therefore assemble and pass the same kind of
per-matchup input snapshot `_freeze_matchup_inputs` does, adapted to
whichever matchup representation #197/#190 produced.

**Assembling that snapshot is not itself sufficient — the finals publish
and correction transaction must also lock and revalidate it before
inserting the official result, or the same read-then-write race already
fixed for bracket creation, advance-bracket, and SuperScore's publisher
recurs here — Codex review, PR #196, twenty-second round, verified
directly against `app/calculations.py`'s `_persist`.** `_persist` writes
only to `bbbffl_matchup_calculation`; it never touches `bbbffl_matchup.
review_version` at all — recalculation and review-version-bumping rulings
are, exactly as established for SuperScore, two independent counters. If a
finals calculation reruns (e.g. a later AFL evidence correction) after the
publish adapter assembles its input snapshots but before the result
transaction commits, simply copying the ordinary `expected_review_versions`
check would not catch it either, since recalculation never advances
`review_version` — the new official result could freeze the old score and
the now-stale snapshot as if nothing had changed. The finals publish/
correction transaction must therefore `SELECT ... FOR UPDATE` every
captured matchup's calculation row (or whichever shared serialization
record #197's chosen path exposes) alongside its `bbbffl_matchup.
review_version`, in a deterministic order, and compare each against the
value captured when its snapshot was assembled — aborting for the caller to
rebuild/retry if either has changed — before inserting the official result.
This mirrors the identical discipline already required of bracket creation,
advance-bracket, and SuperScore's publisher; finals' publish/correction
command is new code, not reused `attempt_signoff`, so it must not silently
inherit the narrower guarantee the ordinary path happens to get away with.

**Two further corrections carry SuperScore's twenty-ninth/thirty-first
round fixes into this finals adapter — Codex review, PR #196, thirty-first
round.** First: none of the locking above proves the finals scores being
published reflect *current* upstream AFL facts, only that nothing changed
since the snapshot was assembled — exactly the gap identified for
SuperScore. The finals publish/correction command must recompute the
round's matchups (`state.calculations.calculate_round(round_id)`) inside
one fresh `evidence_batch()` scope immediately before assembling its input
snapshots, and fail closed if the batch reports itself not
`evidence_fresh` — the same discipline the ordinary `signoff` route and
SuperScore's publisher both already require, adapted from five/ten
independent units to finals' matchups. Second: the `SELECT ... FOR UPDATE`
revalidation above closes a narrower race than it first appears to — two
overlapping calculations for the *same* matchup around an AFL-evidence
correction can still let an older one overwrite a newer one via ordinary
commit-order, exactly as identified for SuperScore, and (verified against
`app/calculations.py` and `app/afl_resilience.py`) there is no live,
totally-ordered `upstream_revision` value to compare at persist time to
stop it — `upstream_revision`/`upstream_observed_at` are caller-supplied,
default to `None`, and no real call site populates them. The finals
calculation path must therefore serialize, not compare: it must hold an
exclusive lock on the matchup's serialization row across its entire
compute-then-persist window — acquired before reading scoring/evidence
inputs, held through persisting the calculation and committing — so a
second calculation for the same matchup cannot start computing until the
first has finished, making "persisted last" and "reflects the newest
evidence" the same statement again without requiring any ordering field
the real evidence client does not provide. **Which row that lock targets
is #197-path-dependent, not always `bbbffl_matchup` — Codex review, PR
#196, thirty-third round.** Under #197's shared-table path, a finals
match *is* a `bbbffl_matchup` row and that row is the correct lock target,
exactly as written above. Under #197's parallel-storage path, a finals
match has no `bbbffl_matchup` row at all, so requiring a lock on one
would be unimplementable on that path — silently skipping the lock
instead would reopen exactly the stale-overwrite race this correction
exists to close. This must lock whichever always-present matchup/
serialization row #197's chosen path actually exposes (the same "shared
serialization record #197's chosen path exposes" already referenced for
the calculation-row/`review_version` revalidation two corrections above),
never unconditionally `bbbffl_matchup`.

**A third correction closes a gap the per-matchup lock above does not, on
its own, cover for the bulk recompute-before-publish path specifically —
Codex review, PR #196, thirty-second round, verified directly against
`app/calculations.py`'s `_RoundFacts`.** `MatchupCalculationService.
calculate_round` constructs a *single* `_RoundFacts` object before
iterating the round's matchups, and that object caches each distinct AFL
round's matches/player-stats the first time any matchup asks for them,
reusing the cached copy for every subsequent matchup in the same bulk
call — deliberately, to avoid redundant AFL-API calls. If the finals
publish command's fresh-evidence-batch bulk recompute (the first
correction above) populates that shared cache while processing one
matchup, and a *separate*, single-matchup calculation acquires the lock
for a *different* matchup in the same round and persists a corrected
result using genuinely fresher facts in the meantime, the bulk job later
reaches that same matchup, acquires its lock exactly as required, but
recomputes from its own already-stale cached facts and overwrites the
corrected calculation — the lock correctly serializes *access to the row*,
but does not by itself guarantee the facts used were fetched after the
lock was acquired. **The bulk recompute path must therefore acquire every
matchup lock it will need (whichever row #197's chosen path exposes, per
the correction immediately above), in deterministic order, before
constructing or populating any shared facts cache for that batch** —
mirroring the deterministic-order multi-row locking this design already
requires elsewhere (SuperScore's ten `superscore_entry_review_state` rows,
the per-matchup calculation-row/`review_version` revalidation above) —
rather than locking each matchup individually as the bulk loop reaches
it. This applies equally to SuperScore's own fresh-evidence-batch bulk
recompute (the twenty-ninth round's requirement, "Scoring" below): if its
implementation uses an analogous shared per-round facts cache across the
ten entries, it must acquire all ten entry locks up front, before
populating that cache, for the identical reason — SuperScore's
`superscore_entry_review_state` row is not #197-path-dependent, since
SuperScore never has `bbbffl_matchup` rows regardless of which path #197
chooses for finals.

This new command is not optional polish on top of otherwise-reusable
review machinery — per "Scoring" above, `app.round_review.
build_round_review`/`attempt_signoff` themselves hard-code "exactly five
matchups" independent of #197's schema choice, so a finals-specific
review-readiness/sign-off adapter is required unconditionally, not merely
when #197 happens to build parallel storage.

### Public, coach and Scorer/operator views

- **Public:** a finals bracket page (who plays whom, this week's/each
  week's result, who is eliminated, who advances) — the legacy Grand Final
  prototype's single-matchup detail view is reasonable evidence for what
  the "click a match to see live positional detail" experience should look
  like, generalised to whichever of the **six finals matches across the
  four weeks** (two in Week 1, two in Week 2, one each in Weeks 3-4 — never
  four) is selected. `docs/plans/2027-season-model.md`'s "Public spectator
  scope" already confirms a public finals bracket is intended.
- **Coach:** identical weekly-lineup submission experience to an ordinary
  round, plus bracket context (who they play, what a win/loss means for
  progression).
- **Scorer/operator:** round review/sign-off identical in shape to
  `app.round_review`, plus the explicit "advance bracket" confirmation step
  described above.

### Handling of corrections

Exactly the existing audited pathway: a post-final correction to a finals
result reuses the same "new reason-carrying official-result version,
previous version protected by trigger" mechanism ordinary rounds already
have (`docs/competition-lifecycle.md`) **while the season remains active**.
Every finals correction and cascading re-derivation must first lock the
owning `bbbffl_season` row and reject `lifecycle_state = 'completed'`; see the
shared completed-season write fence below. **A correction that changes who
advanced after the bracket has already progressed is a genuinely hard case**
(see "Historical gaps" below) and must not be silently resolved by this
design — it needs an explicit Scorer-facing decision path (recompute
downstream pairings vs. flag for manual review), which is exactly the kind
of thing `docs/plans/2027-season-model.md`'s design principle 8 ("audit
exceptional changes rather than silently rewriting history") anticipates
but does not itself resolve. **A distinct, narrower race sits underneath
that open design question — see "Match generation/representation" above:**
the finals correction command must lock the same prerequisite matchup
row(s) an in-flight "advance bracket" transaction is reading, so the two
can never both proceed against what each believes is the current result;
this is a concurrency-control requirement #191/#190 must implement
regardless of which answer the Scorer-facing decision path above lands on.

**If #197 chooses the shared-table path, the existing ordinary correction
endpoint must be fenced off from finals matchups — verified directly
against `app/routes/round_review.py` (Codex review, PR #196, thirteenth
round).** `POST /matchup/{matchup_id}/correct` and its `_authorise_matchup`
helper check only that the matchup exists and that its round's season is in
scope — there is no `stream_type` check at all, unlike the routes catalogued
in "A wider pattern" above. Under the shared-table path, a finals matchup
*is* a `bbbffl_matchup` row, so this endpoint would accept its `matchup_id`
and call `app.round_review.attempt_correction` directly: a new official-
result version is recorded, but the finals-specific correction path above
(and its downstream-pairing/premiership decision) is never invoked. Whoever
implements the shared-table path in #197, and the finals correction work in
#191, must make this endpoint reject a finals matchup (dispatching it
through the finals correction boundary instead) rather than silently
correcting it through the ordinary path. **This is conditional on #197's
choice**: under the parallel-storage path, a finals matchup is never a
`bbbffl_matchup` row, so `_authorise_matchup`'s existing lookup already
404s it — no additional fencing is needed there.

### Grand Final/season winner recording and end-of-season completion

New: explicit `season.premiership.recorded` and `season.wooden_spoon.recorded`
(or one equivalent awards event) audit provenance plus persisted, versioned
records. The premiership record contains the premier `season_entry_id`,
runner-up, exact effective Grand Final official-result version, and its frozen
bracket/finals-seeding provenance. The wooden-spoon record contains the last-
placed `season_entry_id` and the frozen Round 20 mathematical-ladder snapshot/
effective-result references used to establish that fact — it is an H&A fact,
not derived from the finals bracket or historical finals-seeding order.

**#195 materialises both canonical award records inside the completion
transaction, before establishing the completed-state fence.** After taking the
season lock and locking/re-verifying the prerequisite effective result and
snapshot rows, it derives both awards. If an identical current award version
already exists, this step is idempotent; if an active-season correction made an
existing award stale, it appends an audited superseding version in the same
transaction. It must fail closed rather than complete if either award cannot be
derived, its referenced result/snapshot version is no longer effective, or the
two persisted current records do not exactly match those locked inputs. This
removes any crash window in which the season could become completed before its
required historical awards exist.

Before completion, an ordinary Round 20 or Grand Final correction that changes
an award may use this same reason-carrying re-recording boundary. After
completion, premiership/wooden-spoon correction or re-recording is rejected by
the completed-season fence unless a separately designed audited reopen pathway
has first returned the season to a writable state.

## SuperScore design

### Confirmed historical requirements

`docs/plans/2027-season-model.md`'s "SuperScore" section and `docs/plans/
2026-workbook-findings.md`'s "SuperScore" section, cross-checked against the
2026 workbook's four `SUPERSCORE ROUND` blocks and the 2021-2025 workbooks'
consistent `SUPERSCORE ROUND 1`-style structure, together establish these as
**confirmed historical rules**:

- Four independent rounds, SS1-SS4, running across the same four AFL rounds
  as the four finals weeks (**concurrent with finals**, not sequential to
  them, not before or after).
- **All ten coaches** participate in every SuperScore round, including the
  five teams eliminated from or never qualified for the finals.
- Each round ranks all ten entries by total score; **highest score wins
  that round**. Equal highest scores are **joint winners** — unlike the
  ordinary ladder's exact-equality escalation to a scorer ruling, a tied
  SuperScore round has an explicit, confirmed resolution (there is nothing
  to rule on).
- Coaches select only from players their own club already owns — the same
  season ownership ledger as the ordinary competition, not a separate
  SuperScore-only pool or draft.
- The same nine-position lineup structure and the same scoring formulas as
  the ordinary competition (Forward/Midfielder/Tackler/Ruck/Interchange;
  same DNP/interchange/loophole rules).
- A finalist's SuperScore lineup for a given AFL round is **independent**
  of their finals lineup for that same round — the same coach can and does
  submit two different nine-position teams for the same AFL round, one
  under the finals stream, one under the SuperScore stream.
- Failure-to-submit fallback: **SS1** may derive from that coach's most
  recently named *ordinary* BBBFFL lineup (Round 20, in the 2026 structure)
  because no SuperScore lineup has ever existed yet, subject to scorer
  confirmation. **SS2 onward** carries forward from the coach's own
  previous SuperScore lineup, exactly like an ordinary round.
- SuperScore performances are **excluded** from the normal all-time BBBFFL
  record book (confirmed at the 2026 Grand Final-day coach discussion,
  `docs/plans/2027-season-decisions.md`) and SuperScore should be capable of
  maintaining its own separate record/history context.
- A monetary prize is awarded for each of the four SuperScore rounds
  (`docs/plans/2027-season-model.md`'s "Awards" section) — lightweight
  configuration only, per that document's existing fees/prizes scope.

### A genuine architecture gap this investigation found: cross-stream carry-forward

`app.carry_forward.CarryForwardService.resolve_source` is **deliberately**
scoped to the *same* `competition_id` — its own module docstring states this
explicitly: "an ordinary lineup can therefore never source a SuperScore
submission, or vice versa." This guard exists to stop an ordinary round
accidentally sourcing a SuperScore lineup (or vice versa) by mistake, and it
must **not** be weakened or removed — but it also means the confirmed SS1
fallback rule above (derive from the coach's most recent *ordinary*
lineup) cannot go through `resolve_source`/`carry_forward` as written today.

**A bare standalone function is not enough on its own — correction (Codex
review, PR #196, fourth round), the same finding as finals' identical gap
above.** By the time SS1's fallback is actually needed (a coach genuinely
missed the deadline), SS1's lockout has already activated, so the real
supported path is `app.lineup_adjudication.LineupAdjudicationService.
apply_carry_forward_fallback`, not a raw ordinary `submit` call — and that
method calls `self._carry_forward.resolve_source(season_id, competition_id,
...)` internally.

**That standalone function also cannot be a parameter threaded into
`resolve_source` itself — second correction (Codex review, PR #196, fifth
round), verified directly against `app/carry_forward.py`, the same
mechanical problem as finals' identical case above.** `resolve_source`
requires its *target* `bbbffl_round_id` to belong to the `competition_id`
passed in, then compares source-round `sequence` against the target's
*within that one competition* — so an SS1 target round paired with the
ordinary `competition_id` fails the first check outright, and `sequence`
numbers restart independently per stream regardless (SS1 is sequence 1,
Round 20 is sequence 20 — not comparable). The fix, shared verbatim with
finals: a small separate function (e.g.
`resolve_cross_stream_fallback_source(database, season_id,
source_competition_id, season_entry_id)`) that looks up simply *the
entry's most recent submitted lineup in the ordinary competition* — no
target round, no sequence comparison, since there is nothing meaningful to
compare against across streams — and an extension to
`LineupAdjudicationService.apply_carry_forward_fallback`/its preview that
uses this function's result **in place of** the same-stream `resolve_
source` call when the confirmed SS1 case applies, while preserving the
method's existing atomic `require_unchanged` re-validation. This is a
small, well-bounded, default-preserves-existing-behaviour addition to
`app.lineup_adjudication`, not a reason to widen `app.carry_forward`
itself — and it is the *same* extension finals needs, so #191 and #192
should implement and test it together rather than each adding a competing
mechanism.

### Roster/list construction and player eligibility

No new roster concept: SuperScore entries are the same ten `season_entry`
rows, drawing from the same `player_ownership_period`/`season_player_pool`
ledger the ordinary competition and finals both already use. There is no
separate SuperScore squad, no SuperScore-specific draft, and no SuperScore-
specific eligibility rule beyond "currently owned by this entry" — identical
to the ordinary competition's ownership check in `app.lineups.
WeeklyLineupRepository._validate_ownership`.

### Lineup structure/positions, selection and lockout lifecycle

Identical to the ordinary competition for everything that is genuinely
about *selection*, not *scoring*: reuse `app.lineups`, `app.lockouts`, and
`app.lineup_validation` unchanged **once #197 lands** (see "A foundational
schema fork" above — `bbbffl_round_lifecycle`'s fixture-draw requirement
blocks lineup submission for SuperScore rounds exactly as it does for
finals, and this is a shared prerequisite, not something #192 resolves on
its own), scoped to the `superscore` competition stream's rounds. `app.
participation.assess_participation` is also reusable unchanged regardless
— it is a pure, stateless evidence-assessment function with no `matchup_id`
or round-lifecycle dependency at all.

### DNP, Interchange and loophole rulings — a second genuine SuperScore-specific abstraction

**Correction to an earlier draft of this document (Codex review, PR #196):
`app.round_review`'s DNP/Interchange ruling machinery cannot be reused
unchanged for SuperScore, for the same underlying reason "Scoring" below
cannot.** `RoundReviewRepository.record_dnp_ruling`/`record_interchange_
ruling`/`record_override` (and the row-locking helper `_locked_matchup`
they all share) are keyed by `matchup_id`, and `_locked_matchup` explicitly
validates that the ruled `season_entry_id` is that matchup's home or away
side (`UnknownEntryError` otherwise) before writing to `bbbffl_matchup_
slot_ruling`/`bbbffl_matchup_interchange_ruling`, both of which are
themselves keyed by `matchup_id`. Since SuperScore deliberately has no
`bbbffl_matchup` rows at all, there is no `matchup_id` for these calls to
resolve against — they would either raise `UnknownMatchupError` or have
nowhere correct to persist. SuperScore therefore needs its own
**entry-scoped** DNP/Interchange/override ruling boundary (ruling rows keyed
by `season_entry_id` + round + slot, not `matchup_id`), following `app.
round_review`'s validation/CAS-versioning/audit conventions but adapted to
that key shape — not a stream-scoped call into the existing module. This
belongs to whichever issue owns SuperScore's round lifecycle (#192).

**This new ruling boundary must also invalidate itself on a lineup
correction — correction (Codex review, PR #196, fourth round), a gap the
original identification of this boundary missed.** `app.lineups.
WeeklyLineupRepository.submit_correction` calls a private helper,
`_invalidate_stale_review_state`, that clears any `bbbffl_matchup_slot_
ruling`/`bbbffl_matchup_interchange_ruling`/`bbbffl_matchup_override` row
for a position the correction actually changed — specifically so a ruling
recorded against the *pre-correction* occupant of a slot can never keep
silently applying to whoever the correction just installed there instead.
That helper queries `bbbffl_matchup` by `bbbffl_round_id`/`season_entry_id`
and is therefore a safe no-op for SuperScore (no matchup rows exist to
match), which means a SuperScore lineup correction today would **not**
invalidate the new entry-scoped ruling boundary's rows at all — the exact
correctness gap `_invalidate_stale_review_state` exists to prevent for the
ordinary/finals case.

**There is only one safe fix here, not two — correction (Codex review,
PR #196, eighth round): an external wrapper cannot provide atomicity.**
`submit_correction` opens and commits its own transaction internally
(`with transaction(self.database) as conn:`) and exposes neither that
connection nor a callback hook to any caller — verified directly against
`app/lineups.py`. A separate function called before or after
`submit_correction`, however "atomic" it looks written down, is
necessarily a second, independent transaction: a crash or a concurrent
write between the two leaves the corrected lineup and the entry-scoped
ruling table disagreeing, exactly the inconsistency `_invalidate_stale_
review_state` exists to prevent. **#192 must therefore extend `app.
lineups` itself** — either widen `_invalidate_stale_review_state` with an
additive, raw-SQL clear of the new entry-scoped ruling table (mirroring how
it already reaches into `app.round_review`'s tables by raw SQL rather than
importing that module), or add a narrow callback parameter that `submit_
correction` invokes from *inside* its own existing transaction. A
disconnected external hook is not an acceptable alternative. A corrected
SuperScore lineup's stale rulings must not silently keep applying to the
replacement player — this is an additive extension consistent with `app.
lineups`'s own existing pattern, not a change to its behaviour for any
existing ordinary/finals caller.

**This in-transaction fix is still unreachable through the supported
Scorer correction workflow — the same route-layer gap as the adjudication
finding above, correction (Codex review, PR #196, tenth round), verified
directly against `app/routes/lineup_correction.py`.** Its own
`_authorise_round` hard-filters `stream_type='ordinary'` the same way
`lineup_adjudication`'s does, and its round-entry listing is likewise
matchup-derived — so a Scorer has no way to invoke a finals/SuperScore
correction at all through the existing HTTP surface, regardless of how
correctly `app.lineups`'s invalidation extension is implemented. (Under
#197's parallel-storage path, `LineupCorrectionService.describe` also
reads `bbbffl_round_lifecycle` directly, adding a second reason this route
would need adaptation there.) **#191/#192 must adapt this route the same
way as the adjudication route above** — the correction extension this
section describes has no way to be exercised in production otherwise.

### Scoring

**Correction to an earlier draft of this document (Codex review, PR #196):
`app.calculations.MatchupCalculationService` cannot be reused unchanged
either, for the identical reason.** `calculate_round` iterates a round's
`bbbffl_matchup` rows and `_calculate` computes and persists one
`bbbffl_matchup_calculation` per home/away pair, keyed by `matchup_id`.
Because this design deliberately creates no SuperScore matchups, calling
`calculate_round`/`calculate_matchup` unchanged against a SuperScore round
would calculate nothing — there is no matchup for it to find. SuperScore
therefore needs its own **entry-scoped calculation path**: a small sibling
service that, for each of the ten entries' submitted lineups, calls the
same underlying pure `app.scoring` formulas (`score_position` etc. — this
part genuinely is unchanged and must not be reimplemented) and persists an
entry-keyed calculated snapshot, preserving the same calculated-vs-
official-result *separation principle* `app.calculations`/`app.
competition_lifecycle` established, without literally being that module.
This is the calculation-side counterpart of the "leaderboard results, not
matchups" official-result gap already identified below; both stem from the
same root cause (SuperScore has entries, not matchup pairs) and belong to
the same follow-up issue (#193).

The calculation is not the durable coordination record. During setup of each
SuperScore round, its lifecycle transaction must create an always-present
`superscore_entry_review_state` (name illustrative) row for every eligible
entry, uniquely keyed by round and `season_entry_id`, with
`review_version = 0`. Setup is incomplete, and the round must not open, if the
complete set of ten state rows cannot be created or verified. These rows exist before lineups,
rulings, or calculation rows do and remain the shared lock/CAS target for the
round's lifetime.

**Locking and advancing the review-state row when a calculation is
persisted is not itself sufficient — the calculation service must also
verify its own inputs were not superseded while it was computing, or a
stale calculation can acquire a fresh `review_version` and sail through
the publisher's check undetected — Codex review, PR #196, twenty-fifth
round.** The calculation service reads an entry's scoring inputs
(lineup, rulings) at the start of computation, then some time later locks
the review-state row to persist its result. If a lineup correction or
ruling lands *during* that computation window — after the inputs were
read, but before the calculation's own persistence transaction locks the
row — the calculation still computes from the now-stale inputs. The
calculation service must therefore capture the entry's `review_version`
at the moment it reads its scoring inputs (before computing), then,
inside the same transaction that locks the review-state row to persist
the calculation, compare the row's current `review_version` against that
captured value — aborting the persist (and requiring the caller to
re-read inputs and recompute) if it has changed.

**A further correction (Codex review, PR #196, twenty-sixth round): the
calculation itself must not *advance* `review_version` on successful
persist — it must instead *record* which version it was computed against,
and the publisher must check that recorded value against the row's
current version, not merely "unchanged since the publisher's own read."**
The round-25 fix above closes the race *during* one calculation's own
compute window; it does not close a simpler, more common case: a
calculation persists successfully (against review_version `v3`, say), and
*afterward* — with no race at all, just an ordinary later event — a new
ruling or lineup correction lands and legitimately advances the row to
`v4`. No further calculation has run yet to reflect `v4`. When the
publisher later assembles its snapshot, it reads whatever `review_version`
happens to be on the row *right now* (`v4`) as its "expected" value, locks
the row, sees `v4` again (nothing raced during the publish transaction
itself), and its CAS check passes cleanly — yet the calculated score it is
about to publish still reflects the stale, pre-`v4` inputs. The CAS check
as originally specified only ever detects a race *within the publish
transaction's own window*; it cannot detect staleness that already existed
*before* the publisher ever looked, because the publisher has no way to
tell "the current version" from "the version this calculation actually
reflects" — both looked identical from where it was standing. Fix: the
entry-scoped calculated snapshot must persist the exact `review_version`
it was computed against as `computed_as_of_review_version` (captured at
the same moment as the round-25 check, above) — a *description of what the
calculation is a derivation of*, not a stamp claiming new truth. Because a
calculation never introduces new truth (only lineups, rulings, and
corrections do), it must **not** advance `review_version` itself on
success — doing so was the original (now-superseded) instruction, and is
precisely what let a stale calculation look current. The publisher must
then, for each entry, lock the review-state row and compare its *current*
`review_version` against that entry's latest calculation's
`computed_as_of_review_version` — not against whatever the publisher itself
captured moments earlier. A mismatch means the calculation is stale
relative to the row's actual current state (regardless of whether anything
changed during the publish transaction), and publication must abort/block
for that entry until a fresh calculation, computed against the row's
current version, exists. Calculation rows are derived mutable data and may
be absent; no operation may use their existence, revision, or
`computed_as_of_review_version` as a substitute for review state itself —
only as the fact publication checks that state against.

### A genuine SuperScore-specific abstraction: leaderboard results, not matchups

Unlike an ordinary or finals round, a SuperScore round has **no head-to-head
pairing at all** — ten independent entries ranked by total score. This
means:

- `bbbffl_official_result`'s home/away two-team shape (the same shape
  `app.competition_lifecycle` and any finals-match publish command use)
  does not fit SuperScore. A SuperScore round needs its own official-result
  representation: one row per entry per round (`season_entry_id`, `total_
  score`, `rank`, `is_joint_winner`), not a home/away pair.
- Publication is "confirm all ten entries' scores and freeze the ranking,"
  not "confirm five results and transition the round." The lifecycle state
  machine can stay the same shape (`upcoming -> open -> live -> review ->
  final`), but the publish command is genuinely new, not a relabelled call
  into `publish_results`.
- The old prototype's `app/superscore.py`/`app/routes/superscore.py`
  already demonstrate the *ranking* concept cleanly (`get_superscore_view`
  ranks N configured entries by total score with no leader/margin
  concept) — that shape is useful evidence for the new module's read model,
  even though its underlying JSON-config/legacy-table storage is not
  reused (see "Architecture" above).

**Two further corrections (Codex review, PR #196, seventeenth round), both
verified directly against `app/competition_lifecycle.py` and `app/
round_review.py`'s existing ordinary-result machinery:**

- **A correction must version and republish the whole leaderboard, not
  just the corrected entry's row.** `rank` and `is_joint_winner` are
  properties of the *entire* ten-entry set, not of any one entry in
  isolation: correcting one entry's score can move it above or below
  others, or push it into or out of a tie for first, which changes the
  persisted `rank`/`is_joint_winner` of entries whose own score never
  changed. If corrections version and persist only the one row that was
  actually corrected, the official leaderboard becomes a mix of rows from
  different revisions — internally inconsistent (e.g. two entries both
  recorded as rank 3, or a stale `is_joint_winner` on an entry the
  correction actually separated from the tie). The SuperScore official-
  result representation must therefore be a **round-level revision**: one
  version number shared by all ten entries' rows for that round (mirroring
  how ordinary publication versions and republishes the whole round's set
  of matchups together, per `app.competition_lifecycle.publish_results`,
  rather than one matchup at a time). Every correction recomputes rank and
  `is_joint_winner` for all ten entries from their (possibly-unchanged)
  scores and atomically persists a brand-new revision covering all ten —
  never a partial update to a subset.
- **Each revision must also freeze the exact scoring inputs that produced
  it, per entry — the entry-scoped counterpart of `bbbffl_official_
  result.input_snapshot`.** Ordinary publication does not persist only the
  derived `home_score`/`away_score`; `app.round_review._freeze_matchup_
  inputs` also captures `rules_version_id`, `calculation_revision`,
  `calculation_fingerprint`, both sides' lineup/DNP/interchange/override
  state, and who/when finalised it, precisely so a later recalculation or
  ruling correction can never change what an already-published version
  meant (`app/round_review.py`'s own docstring: "never re-derived from live
  lineup/rule/recommendation state"). The new entry-scoped calculation path
  above is the mutable layer this applies to for SuperScore — analogous to
  `bbbffl_matchup_calculation`, and just as mutable after recalculation or a
  ruling/lineup correction. Each SuperScore result revision must therefore
  freeze the equivalent entry-scoped snapshot (lineup version, calculation
  revision/fingerprint, and any DNP/Interchange/override rulings in effect)
  for every one of the ten entries at the moment that revision is
  published, not merely the derived score/rank/`is_joint_winner` fields —
  otherwise a later recalculation could leave an already-published
  SuperScore result unexplainable, exactly the gap `input_snapshot` exists
  to close for the ordinary case.
- **A third correction (Codex review, PR #196, eighteenth round): the
  publish/correction command must also re-verify the ten captured review
  versions inside the same transaction that writes the new
  leaderboard revision, or the same read-then-write race already fixed for
  bracket creation and advance-bracket recurs here.** If a lineup
  correction, ruling, or recalculation commits after the ten entry
  snapshots are assembled but before the leaderboard-revision `INSERT`
  commits, this requirement can still publish stale scores and snapshots
  as the new official revision. The ordinary path closes the identical gap
  by locking `bbbffl_matchup` rows (`_locked_matchups`) and comparing
  `expected_review_versions` against them inside `publish_results`'s own
  transaction — the check and the write can never race specifically
  because both happen against the same locked rows, not because the check
  merely runs inside a transaction.
- **A fourth correction (Codex review, PR #196, nineteenth round):
  comparing only the calculation revision above is not sufficient on its
  own — verified directly against `app/round_review.py`'s actual CAS key.**
  `publish_results`'s `expected_review_versions` checks `bbbffl_matchup.
  review_version`, a single counter every DNP/Interchange/override ruling
  bumps (`record_dnp_ruling`/`record_interchange_ruling`/`record_override`
  each `UPDATE bbbffl_matchup SET review_version=review_version+1`) — **a
  separate counter from `bbbffl_matchup_calculation.revision`, which only
  the calculation service itself advances.** A lineup correction or a
  ruling/override recorded through #192's entry-scoped ruling boundary need
  not touch the entry-scoped calculation row's own revision at all, so
  comparing only that calculation revision leaves exactly the same
  race open through the ruling/correction path: either can still commit,
  unnoticed by this check, after the ten snapshots are assembled but before
  the leaderboard-revision `INSERT` commits. SuperScore therefore uses the
  always-present per-round/per-entry review-state row created during lifecycle
  setup above, not a field on an optional calculation row. A lineup correction
  (including its stale-ruling invalidation) and a DNP/Interchange/override
  ruling must lock that entry's review-state row and advance its
  `review_version` in the **same transaction** as the mutation. A
  failed mutation advances nothing. **Calculation persistence is deliberately
  not in this list — see the twenty-sixth-round correction below, under
  "Scoring": a calculation locks and compares the row at persist time but
  never advances it, recording the version it was computed against instead.**
  This gives #192's lifecycle/ruling/
  correction work and #193's calculation/publication work one durable
  serialization point even before a first calculation exists.

  **A fifth correction (Codex review, PR #196, twenty-first round): "a
  lineup correction" above is too narrow — every effective lineup
  submission must advance this row, not only a post-lockout correction.**
  During a live SuperScore round, staged lockout still permits a coach to
  submit or resubmit still-unlocked positions through the ordinary
  `submit`/`submit_positions` path (not `submit_correction`, which is the
  distinct post-lockout admin pathway) — a normal, pre-lockout resubmission
  after some positions have already locked. If a calculation has already
  run against the entry's prior submission, and the coach then resubmits an
  unlocked position, that resubmission changes what the effective lineup
  *is* without going through `submit_correction` at all — invisible to a
  review-state advance scoped only to "correction," so the publisher's CAS
  check would not catch it, and a stale pre-resubmission calculation could
  become the official score. **Every write that changes an entry's
  effective submitted lineup for the round — the initial submission, any
  unlocked resubmission, and a post-lockout correction alike — must lock
  and advance that entry's review-state row in the same transaction**, not
  only the narrower "correction" case. (Equivalently, the publisher could
  instead revalidate each captured calculation's source lineup version
  under the publication locks rather than relying solely on the review-state
  counter to reflect every submission path — but the review-state-row
  approach is preferred for consistency with the rest of this mechanism,
  and to avoid a second, parallel validation rule.)

  The SuperScore publisher and correction command must `SELECT ... FOR
  UPDATE` all ten review-state rows in deterministic `season_entry_id` order,
  verify that all ten exist, and — per the twenty-sixth-round correction
  above — compare each row's *current* `review_version` against that
  entry's latest calculated snapshot's own `computed_as_of_review_version`,
  not against a value the publisher itself captured earlier. It aborts for
  the caller to rebuild/retry (recalculating first, if the mismatch is
  staleness rather than a mid-publish race) if any row is missing or its
  current version does not match its calculation's recorded one; only then
  may it insert the atomic leaderboard revision and advance the round
  toward `final`.

  **A further correction (Codex review, PR #196, twenty-eighth round): the
  review-state check above closes staleness from lineup/ruling changes, but
  not staleness from a same-`review_version` recalculation — the publisher
  must also lock and re-verify each entry's calculation row itself, not
  rely on the review-state check alone.** A recalculation can legitimately
  produce a new score without `review_version` changing at all — e.g. an
  upstream AFL-evidence correction changes the scoring inputs' *values*
  without any lineup, ruling, or correction touching the entry's effective
  submission — so the calculation's `computed_as_of_review_version` is
  unchanged (correctly: the entry's lineup/ruling truth genuinely didn't
  change), yet the calculation's own revision/fingerprint (and its score)
  did. If the publisher assembles its snapshot from an older calculation
  revision, and a newer one persists afterward — still against the same
  `review_version`, so the review-state check alone sees no staleness —
  the publisher would freeze the old score even though a newer, different
  calculation exists. **The publisher must therefore also, inside the same
  locked transaction, re-read (or lock and compare) each entry's
  calculation row's revision/fingerprint against the value captured when
  the snapshot was assembled**, exactly as required of the finals
  publish/correction path (see "Finals result publication" above) — the
  review-state row is the authority for *lineup/ruling* truth; the
  calculation row's own revision is the (separate) authority for whether
  the snapshot reflects the *latest computation* against that truth. Both
  checks are required; neither alone is sufficient. Calculation rows
  remain derived, never the lock/CAS authority for *review* state — but
  they are the authority the publisher must independently re-verify for
  *computation* freshness, via their own revision/fingerprint, not via
  `computed_as_of_review_version` (which cannot and does not encode this
  dimension).

  **A further correction (Codex review, PR #196, twenty-ninth round): none
  of the locking/recheck discipline above proves the calculations being
  published reflect *current* upstream AFL facts — it only proves nothing
  changed between snapshot assembly and the publish lock.** If AFL evidence
  changes but nothing has *triggered* a recalculation since, the
  calculation row, its revision/fingerprint, and `review_version` all stay
  exactly as they were — every check above passes cleanly — while the score
  about to be published is still built from outdated evidence. **The
  ordinary competition already solves exactly this problem, and the
  SuperScore publish/correction command must reuse the same discipline, not
  reinvent a weaker one: verified directly against `app/routes/
  round_review.py`'s `signoff` route, which recomputes every matchup
  (`state.calculations.calculate_round(round_id)`) immediately before
  validating readiness, inside one `afl_client.evidence_batch()` scope, and
  fails closed on `evidence_fresh` rather than trusting whatever was last
  calculated.** The SuperScore publish and correction commands must, inside
  that same evidence-batch scope, recompute all ten entries via this
  design's entry-scoped calculation path immediately before assembling the
  publish snapshot, and fail closed (refuse to publish) if the evidence
  batch reports itself not fresh — exactly mirroring `signoff`'s shape,
  adapted from five matchups to ten entries. This does not replace the
  review-state/calculation-row locking above (a residual, much narrower
  race between "recompute finishes" and "the publish transaction commits"
  still needs it) — it closes the larger, more fundamental gap that no
  amount of locking a stale calculation against itself can close: staleness
  relative to the outside world, not staleness relative to a prior read.
- **A further correction (Codex review, PR #196, twenty-ninth round,
  superseded — not merely amended — by the thirty-first round): the
  twenty-ninth/thirtieth round's "monotonic `upstream_revision`" guard
  cannot work against the real schema and must be replaced with
  serialization, not repaired.** The twenty-ninth round required comparing
  `upstream_revision` at persist time to stop an older-evidence calculation
  overwriting a newer one. **Verified directly against `app/calculations.py`
  and `app/afl_resilience.py`, this cannot be implemented as specified:**
  `upstream_revision`/`upstream_observed_at` are plain caller-supplied
  parameters to `calculate_round`/`calculate_matchup`, defaulting to `None`
  — and every real call site (`app/routes/round_review.py`'s `signoff` and
  its `calculate_round`/`calculate_matchup` calls) passes neither, so the
  column is never populated with an ordered value by any code that exists
  today. `_calculate` explicitly excludes both fields from the calculation's
  fingerprint hash, treating them as diagnostic provenance, not an ordering
  key. And `EvidenceBatch`, the only live evidence-freshness interface, only
  exposes `is_evidence_fresh()` — a boolean, never a comparable revision
  token. Comparing `upstream_revision` at persist time therefore compares
  `None` against `None` and can never detect an out-of-order overwrite.
  **The fix is to serialize each entry's compute-then-persist window
  instead of inventing a new ordering token no live evidence source
  provides:** the entry-scoped calculation path must acquire an exclusive
  lock on the entry's `superscore_entry_review_state` row (the same row the
  round-18-through-26 review-version CAS already locks) *before* reading
  any scoring/evidence inputs, and hold it through persisting its result
  and committing — not merely capture-then-recheck a value at the two
  endpoints, but hold the lock across the entire window between them. A
  second calculation for the same entry started while the first is still
  computing simply blocks until the first's transaction commits or aborts,
  so two calculations for the same entry can never interleave at all:
  whichever one starts (and therefore reads evidence) later is guaranteed
  to persist later too, making "persisted last" and "reflects the newest
  evidence" the same statement again, without requiring any orderable field
  the real evidence client does not provide. This closes exactly the gap
  the twenty-ninth round identified, through mutual exclusion instead of a
  comparison that cannot be implemented against the current
  `EvidenceBatch`/`upstream_revision` reality. The twenty-ninth round's
  separate fresh-evidence-batch-recompute-before-publish requirement above
  is unaffected and still stands — this replaces only the discarded
  "compare `upstream_revision`" mechanism, narrowing (not eliminating) how
  often the lock is contended at all, since the recompute-before-publish
  discipline already limits how many calculations for the same entry are
  in flight at once.
- **A further correction (Codex review, PR #196, thirty-second round,
  verified directly against `app/calculations.py`'s `_RoundFacts`): if the
  publish/correction command's fresh-evidence-batch bulk recompute (the
  twenty-ninth round's requirement above) implements its per-round
  AFL-facts fetch the same way `MatchupCalculationService.calculate_round`
  does — one shared facts cache built before iterating the ten entries,
  reused for every entry rather than re-fetched each time — the per-entry
  lock above does not by itself close the gap.** A lock correctly
  serializes *access to an entry's row*, but not *when its facts were
  fetched*: the shared cache could be populated while processing entry A,
  a separate single-entry recalculation could lock and persist entry B
  using genuinely fresher facts in the meantime, and the bulk job could
  then reach entry B, acquire its lock exactly as required, but recompute
  from its own already-stale cached facts and overwrite the corrected
  calculation anyway. **The bulk recompute path must therefore acquire
  every entry lock it will need, in deterministic order, before
  constructing or populating any shared facts cache for that batch** —
  the same discipline this section already requires SuperScore's publisher
  to apply when locking all ten `superscore_entry_review_state` rows, now
  applied one step earlier, before the batch even reads AFL facts. The
  identical requirement applies to #191's finals publish/correction bulk
  recompute (see "Handling of corrections" under "Main finals design"),
  since it faces the same `_RoundFacts` sharing risk directly.

### Publication; public/coach/Scorer views

- **Public:** a SuperScore leaderboard for the current round (and,
  eventually, all four rounds' history) — team, score, rank, joint-winner
  marker. The legacy `app/routes/superscore.py`'s `serialize_public_
  superscore` response shape is reasonable prior art for what to expose.
- **Coach:** identical weekly-lineup submission experience to the ordinary
  competition, under the SuperScore stream.
- **Scorer/operator:** DNP/Interchange/override review through the new
  entry-scoped ruling boundary described above (same validation/audit
  *shape* as `app.round_review`, different key), plus a leaderboard-
  confirmation publish step in place of a matchup sign-off.

### Progression/cumulative behaviour

None confirmed: each of the four SuperScore rounds is independent (its own
winner, its own prize). There is no cumulative SuperScore ladder or
aggregate four-round standings in any of the reviewed evidence
(`2027-season-model.md`, workbook findings). Do not invent one.

### Final SuperScore winner/result recording

Each of the four rounds records its own winner(s) (joint winners recorded
explicitly, not arbitrarily broken); there is no single "SuperScore season
champion" beyond the four independent per-round results, per the confirmed
rules above.

## Shared versus distinct behaviour

| Behaviour | Ordinary | Finals | SuperScore | Reuse |
|---|---|---|---|---|
| Nine-position lineup structure/validation | yes | yes | yes | `app.lineups`, `app.lineup_validation` |
| Round can even accept a submission (`bbbffl_round_lifecycle` exists) | yes | **conditional on #197** | **conditional on #197** | fixture-draw-linked schema fork; see "A foundational schema fork" |
| Staged lockout (selective/main triggers) | yes | conditional on #197 | conditional on #197 | `app.lockouts`, same prerequisite as above |
| Finals/SuperScore-participant eligibility enforcement | n/a (fixed 10) | **new** (top-5/bracket-participant check, not in `WeeklyLineupRepository`) | **new** (all-10 check) | finals module (#191) / SuperScore module (#192) each enforce their own |
| Matchup-level DNP/Interchange/override ruling storage | yes | conditional on #197 | **no** (entry-scoped, not `matchup_id`-keyed regardless of #197) | `app.round_review`'s matchup-keyed methods for ordinary, and for finals only if #197 loosens the schema; new entry-scoped ruling boundary for SuperScore always |
| Round-level review readiness/sign-off (`build_round_review`/`attempt_signoff`) | yes | **no, regardless of #197** | **no, regardless of #197** | both hard-code "exactly five matchups"; finals/SuperScore each need their own variable-count-aware adapter unconditionally |
| Participation evidence assessment | yes | yes | yes | `app.participation.assess_participation` unchanged (matchup-independent) |
| Scoring formulas | yes | yes | yes | `app.scoring` unchanged |
| Calculated-vs-official separation *implementation* | yes | conditional on #197 | **no** (entry-scoped regardless of #197) | `app.calculations.MatchupCalculationService` for ordinary, and for finals only if #197 loosens the schema; new entry-scoped calculation path for SuperScore always |
| Review/CAS serialization record | matchup row | matchup or #197 equivalent | **always-present round/entry review-state row** | SuperScore lifecycle setup creates all ten before open; calculations, rulings, corrections and publication lock/version it |
| Completed-season result write fence | required | required, including cascades | required | every result-changing transaction locks the owning season row and rejects `completed`; #195 supplies the shared guard/coverage |
| Player ownership/eligibility | yes | yes | yes | `app.player_pool` unchanged |
| Same-stream carry-forward | yes | yes | yes (SS2+) | `app.carry_forward` unchanged |
| Cross-stream carry-forward (most recent ordinary lineup) | n/a | **Week 1 and seed 1's Week 2** (confirmed rule) | **SS1 only** (confirmed rule) | one shared narrow function for both, see above |
| Fixed pre-drawn fixture (season_fixture_matchup) | yes | **no** | **no** | ordinary-only; cannot be reused |
| Head-to-head two-team official result | yes | yes | **no** | new leaderboard result shape for SuperScore |
| Pairing known before the round starts | yes | **no** (weeks 2-3 depend on prior result) | n/a (no pairing) | new finals bracket module |
| Elimination | no | yes | no | new, finals-only |
| Tied-round resolution | scorer ruling (ladder escalation) | higher seed wins (recommend, scorer confirms) | joint winners (no ruling needed) | three genuinely different rules — do not conflate |
| Counts toward all-time BBBFFL records | yes | yes | **no** (separate record context) | confirmed decision, `2027-season-decisions.md` |
| Participants | 10 | **top 5 only** | **all 10** | different eligible-entry sets per stream |

## Audit, correction and recovery

Every mutating finals/SuperScore operation follows the exact conventions
`docs/audit-events.md` already establishes and `app.finals_seeding`/`app.
midseason_draft` already demonstrate for this replay specifically — nothing
new needs inventing here, only applying:

- **New action names**, `<domain>.<entity>.<event>` (e.g.
  `finals.bracket.created`, `finals.bracket.advanced`,
  `finals.result.published`, `finals.result.corrected`,
  `finals.elimination.recorded`, `superscore.round.opened`,
  `superscore.result.published`, `superscore.result.corrected`,
  `season.premiership.recorded`, `season.wooden_spoon.recorded`,
  `season.completed`) — never repurposing an existing action.
- **Append-only, immutable published results**: finals and SuperScore
  official results follow the same "new version on correction, prior
  version protected by a database trigger" pattern as ordinary results and
  as `finals_seeding_snapshot`/`midseason_ladder_snapshot`.
- **Completed-season write fence**: `bbbffl_season.lifecycle_state =
  'completed'` is the durable boundary, not merely descriptive metadata. Every
  operation that can change official historical results or facts derived from
  them must, in its write transaction and before mutation, lock the owning
  season row and fail closed if it is completed. This includes ordinary result
  correction, finals correction and any cascade of pairings/eliminations,
  SuperScore correction/republication, and premiership/wooden-spoon
  re-recording. The completion command takes the **same season-row lock**,
  verifies the full round predicate, atomically materialises and verifies the
  two award records against their locked provenance, records the completion
  audit event, and changes the lifecycle state to `completed`. A correction
  already holding the lock finishes before completion can verify; a correction
  waiting behind completion acquires the lock afterward, observes `completed`,
  and writes nothing. Checking lifecycle state outside that transaction, or
  only at the route layer, is insufficient.

  There is no implicit administrative bypass. Reopening is outside this
  focused implementation; until a separate audited `reopen completed season`
  command is deliberately designed, completed-to-writable remains illegal. A
  future reopen pathway must itself take the season lock, require actor/reason
  audit provenance, and supersede/invalidate the prior final-checkpoint marker
  before any result-changing operation can proceed.
- **Checkpoint timing**: the same discipline as
  `docs/2026-second-half-replay-playbook.md` sections G/I/K — a paired
  database/checkpoint backup after the finals-seeding snapshot is applied,
  then after each finals week finalises and after each SuperScore round
  finalises. **Correction (Codex review, PR #196, twenty-first round): the
  post-apply backup above is not already done — verified directly against
  the provenance manifest.** `docs/evidence/2026-second-half-replay/
  provenance-manifest.md`'s "Round 20 / home-and-away boundary" section
  records only the "End-of-home-and-away database/checkpoint snapshots...
  retained privately **before** finals-seeding apply" — the pre-apply
  backup, not a subsequent one taken *after* the snapshot was applied. The
  manifest's separate "Finals-seeding snapshot" section records the
  snapshot's own audit/database facts (snapshot id, audit event id,
  replay-context validation) but no independent paired-backup confirmation
  after it. Treat the post-apply checkpoint as an **outstanding step**, not
  a completed one: #194 must take (and record in the evidence directory,
  per its own scope) a paired database/checkpoint backup after the
  finals-seeding snapshot before relying on it as a recovery point — do not
  assume it already exists, and do not skip it on the belief that this
  document's earlier draft already confirmed it. Final archival evidence
  follows this strict
  order: (1) lock the owning season row; (2) fail-closed verification that
  **every required finals round** is in lifecycle state `final` and that **all four
  named SuperScore rounds, SS1-SS4,** exist and are `final`; (3) lock the
  effective Grand Final result and frozen Round 20 ladder/bracket provenance,
  then idempotently create or supersede the premiership and wooden-spoon
  records so both exactly reference those effective versions; (4) record the
  completion audit event; (5) transition the season to `completed`; (6) commit
  that single transaction; then (7), only afterward, create the final
  database/checkpoint evidence from the now-fenced completed state and bind it
  to the completion event/version. Grand Final + SS4 alone must never imply
  completeness while an earlier finals or SuperScore round remains missing,
  `review`, or otherwise unfinished. The archival step must also assert it is
  reading the completed season version established by step 5 and identified by
  the completion event. **#195 owns steps 1-6 and ends by exposing that stable
  completed-season version/completion-event identifier. #194 alone owns step 7
  and its backup/recovery verification.**
- **Backup/recovery**: identical `pg_dump`/`pg_restore` procedure as the
  rest of the second-half playbook; a not-yet-written finals/SuperScore
  playbook (already anticipated by section L of the current playbook)
  should extend `docs/evidence/2026-second-half-replay/` or a new
  `docs/evidence/2026-finals-replay/` directory rather than inventing a new
  evidence format.
- **Replay restart/resume and idempotency**: every new repository method
  should follow `app.finals_seeding`/`app.replay_continuation`'s
  established shape — read-and-validate-before-write context checks that
  raise a specific typed error and mutate nothing on failure, and an
  `apply`-style command that is idempotent against an unchanged resolution
  (calling it again with the same inputs returns the existing state rather
  than erroring or duplicating).
- **What must be inspectable after completion**: the finals bracket
  (every week's pairing, result, and elimination), every SuperScore round's
  full leaderboard, the premiership/wooden-spoon record, and the audit
  trail linking every one of those back to the actor/reason that produced
  it — all through the existing `AuditEventRepository.list_events`/
  `get_event` read boundary, no new read mechanism required.
- **Provenance linking back to the H&A replay and the finals-seeding
  snapshot.** Correction (Codex review, PR #196, ninth round — a fourth-
  round fix to this exact paragraph still hadn't caught up with "Seed
  consumption"'s later, further-corrected mechanism): traceability is
  never established by calling `resolve_finals_seed_order` at all — "Seed
  consumption" above is explicit that the bracket module calls `app.
  finals_seeding.FinalsSeedingRepository.get_snapshot` or, failing that,
  makes **one** `app.ladder.LadderRepository.snapshot` call, and derives
  both the order and its exact provenance from that single read (never a
  direct call to the resolver function, which returns only a bare tuple
  with no provenance of its own and would require an unsafe second read to
  recover one). `finals_bracket` persists that provenance reference (the
  `finals_seeding_snapshot_id`, or the mathematical `LadderSnapshot`'s
  `result_references` and round numbers) at the moment it freezes the
  order, and every later reader (an audit payload, a display, a tie-break)
  reads that persisted reference on the bracket row — never a fresh read
  of any kind, resolver or otherwise.
  This is a small, explicit write this design requires, not something
  calling the resolver already gives for free.
- **End-of-season completion / archive**: this investigation found that
  `app.season.SeasonRepository.transition_lifecycle`'s `active -> completed`
  transition is **not currently enforced as a write-blocking gate anywhere**
  — no repository checks `Season.lifecycle_state` before allowing a mutation.
  Structural isolation between the 2026 replay and a future 2027 season is
  already sound (distinct `season_id`, `docs/season-competition-schema.md`'s
  documented invariant that nothing resolves an implicit "current" season),
  but nothing today would *prevent* an accidental write against the
  "completed" 2026 season once 2027 exists. **#195 must close that gap for all
  official-result-changing paths; the gate is no longer an optional follow-up
  decision.** Its completion command must take the same season-row lock those
  writers take, then lock/CAS-protect the relevant round lifecycle rows and
  fail closed unless every configured, required finals round is `final` and
  SS1, SS2, SS3 and SS4 are each `final`.
  Checking only the terminal labels (Grand Final and SS4), or merely checking
  that their results were published, is not valid evidence that the preceding
  rounds completed. The same full-set predicate gates the final archival
  checkpoint. The predicate and both provenance-bound award records are
  rechecked/materialised in the transaction that marks the season completed;
  the resulting lifecycle state is then enforced by every
  result-changing transaction, so a waiting correction cannot commit after
  the locks release and silently stale the checkpoint. Only after that durable
  fence commits may #194 create the final archival/checkpoint evidence; #195's
  completion acceptance ends at the committed, externally consumable completed
  version/event identifier and does not include archive creation.

## Historical gaps requiring Steve's confirmation

Distinguishing, as the issue asks: **confirmed historical rule** (repeatedly
evidenced across multiple independent sources), **behaviour strongly
evidenced by existing code/data** (one strong source, not independently
cross-checked), **implementation recommendation** (an engineering choice
consistent with confirmed rules but not itself a league rule), and
**unresolved historical fact requiring human confirmation**.

1. **Unresolved: real 2026 finals lineups/results.** Every historical fact
   used for Rounds 1-20 (fixtures, lineups, scores) came from recoverable
   WhatsApp/workbook/afl-api evidence. This investigation found no
   equivalent recovered evidence for the actual 2026 finals week
   lineups/results in this repository. **Question for Steve:** is there
   recoverable historical evidence (WhatsApp threads, the workbook's
   Results sheet for the finals rounds, or similar) for what each coach
   actually submitted and what each finals match actually scored in the
   real 2026 finals? If yes, please point to it (or supply it) before
   implementation begins, the same way first/second-half lineup evidence
   was supplied. If no, the replay will need to either operate the finals
   phase as a fresh (evidence-free) simulation from the known seed forward,
   or accept a documented gap — this materially changes what "replaying"
   the finals even means and should not be assumed either way.
2. **Unresolved: real 2026 SuperScore SS1-SS4 lineups/results.** Same
   question as above, specifically for the four historical SuperScore
   rounds. The 2026 workbook's `Superscore` sheet is confirmed to exist and
   confirmed to record real historical data (`2026-workbook-findings.md`),
   but this repository has not yet extracted or evidenced its actual
   entries. **Question for Steve:** can the workbook's SuperScore sheet
   contents (lineups and/or scores for SS1-SS4) be supplied as replay
   evidence, analogous to how the fixture-rotation table was extracted from
   the workbook for the ordinary season?
3. **Unresolved: which seed number a tied final resolves against.**
   `2027-season-model.md` says a tied final is won by "the team that
   finished higher on the final home-and-away ladder." For the 2026 replay
   specifically, that phrase is now ambiguous between two different,
   non-identical orderings that both exist in this database: the
   *mathematical* Round 20 ladder rank, and the *historical finals-seeding
   snapshot* order (they disagree for exactly the three teams named in
   issue #187 — Running Hots, Evil Absolutes, Motherruckers). Since the
   finals bracket itself is built from the seeding snapshot, the internally
   consistent choice is almost certainly "resolve a tie by the same seed
   order that determined the bracket" (i.e. the snapshot order when one
   exists) — but this is an **implementation recommendation**, not yet a
   confirmed rule, and should be confirmed explicitly given the whole point
   of issue #187 was to keep these two orderings from being silently
   conflated.
4. **Unresolved: correcting a finals result after the bracket has already
   advanced past it.** No source reviewed (season model, decisions,
   workbook findings) addresses what happens if a Week-1 result is
   corrected after Week 2's pairing (derived from it) has already been
   generated and possibly played. **Question for Steve:** should this be
   (a) blocked outright (a finals result becomes uncorrectable once the
   next week's round has opened), (b) require an explicit cascading
   re-derivation with its own audited confirmation, or (c) handled as a
   manual Scorer/quorum ruling recorded through the ordinary correction
   pathway with no automatic cascade? This should be settled before the
   finals-lifecycle follow-up issue is implemented, not discovered mid-PR.
   **Whichever answer Steve gives must also cover the separately-persisted
   `finals_bracket_elimination` record, not only downstream pairings —
   verified directly against "Progression and elimination" above (Codex
   review, PR #196, seventeenth round).** A correction that flips who won
   the Elimination Final, First Semi-Final, or Preliminary Final also flips
   who should be recorded as eliminated by it. Options (a)/(b)/(c) above
   were framed only in terms of blocking or re-deriving the *next week's
   pairing*; even under (b)'s cascading re-derivation, the elimination
   record itself must be included in that same cascade (a new elimination
   revision superseding the old one, audited the same way), or the
   explicit elimination history keeps naming the original, now-incorrect
   loser even after the pairing downstream of it has been fixed.
   **Option (b)'s scope is narrower than it needs to be even with the
   elimination fix above, if the downstream week has already been played
   or published — Codex review, PR #196, twenty-ninth round.** Superseding
   only the pairing and elimination records is not enough once the
   downstream week has its own submitted lineups, rulings, a calculation,
   and an immutable published official result attached to the *old*
   (now-incorrect) participants: a corrected Week 1 winner can produce a
   Week 2 pairing whose already-published official score was earned by
   teams that, after the correction, should never have played each other
   at all. **Option (b), if chosen, must therefore itself decide between
   two sub-options, and this document does not pick between them: (b-i)
   block the cascade once any downstream play state exists — lineups
   submitted, a ruling recorded, a calculation run, or a result published
   — reducing to something closer to option (a) from that point forward;
   or (b-ii) define an audited invalidation/versioning-and-replay
   procedure for every affected downstream artifact — lineups, rulings,
   calculation, and official result, not merely the pairing and
   elimination rows — for every week the correction's cascade reaches.**
   Whichever of (a), (b-i), (b-ii), or (c) Steve confirms, the answer must
   explicitly state which downstream artifacts a cascade is allowed to
   touch, not only "the pairing" as an earlier draft of this document
   implied.
5. **Unresolved: SuperScore prize amounts/configuration for the 2026
   replay.** `2027-season-model.md` confirms a monetary prize exists per
   SuperScore round but this investigation found no specific 2026 amount
   or allocation rule recorded anywhere in the repository.
   **Recommendation:** treat this as out of scope for the replay's
   *sporting* fidelity (result/winner recording) and track it, if at all,
   as a lightweight informational note rather than a blocking requirement
   — consistent with `2027-season-model.md`'s own "the system only needs
   lightweight fee administration unless requirements expand."
6. **Behaviour strongly evidenced, not yet independently cross-checked:**
   the exact AFL-round mapping for the four finals/SuperScore weeks (i.e.
   which real AFL rounds 21-24 map to BBBFFL finals weeks 1-4 for the 2026
   season specifically, including whether any AFL scheduling quirk
   analogous to Opening Round affects them). This should be confirmed
   against `afl-api` evidence the same way `app.round_mapping` was
   populated and validated for Rounds 1-20. **Correction (Codex review,
   PR #196, fourth round): this is not solely a SuperScore concern.** Every
   `bbbffl_round_lifecycle`-shaped row — a finals week's just as much as a
   SuperScore round's — needs its own accepted `round_afl_mapping_revision`
   before it can be created/opened at all, even when a finals week and a
   concurrent SuperScore round point at the same underlying AFL round
   number. #190 must confirm/create the four finals weeks' own mappings as
   part of building the finals round lifecycle, not defer this entirely to
   #192's SuperScore-only mapping work.
## Follow-up issues

Decomposed at boundaries that can each land as an independently reviewable,
independently testable PR, matching the granularity `app.finals_seeding`
and `app.midseason_draft` were each delivered at. Recommended execution
order (each row's "Depends on" names the prerequisite rows):

1. **[#197 — Generalize round-lifecycle and matchup storage for finals/SuperScore streams](https://github.com/JustPlausible/BBBFFL_Scoring/issues/197)**
   — a prerequisite discovered during Codex review of PR #196, not present
   in the original decomposition: resolves the `bbbffl_round_lifecycle`/
   `bbbffl_matchup` fixture-draw-linkage schema fork described in "A
   foundational schema fork" above, which blocks lineup submission for
   **both** finals and SuperScore, not just finals scoring. A shared
   platform-foundation package (schema plus the lifecycle/lookup adaptation
   required by the selected path); no bracket or SuperScore-specific logic.
   Depends on: this document. **Must land before #190 or #192 create any
   actual finals/SuperScore round.**
2. **[#190 — Finals bracket generation and lifecycle](https://github.com/JustPlausible/BBBFFL_Scoring/issues/190)**
   — `app.finals`: bracket creation via the single-read seed-resolution
   mechanism (`FinalsSeedingRepository.get_snapshot`, or one
   `LadderRepository.snapshot` call applying `resolve_finals_seed_order`'s
   exact fallback logic and both its fail-closed checks — never a direct
   call to that function followed by a second provenance read), frozen
   once with its exact provenance, never re-resolved; the
   four-week pairing/progression state machine, elimination recording, the
   finals round lifecycle (built on #197's chosen storage shape), and a
   CLI-first operator tool mirroring `scripts/finals_seeding_2026.py`'s
   preview/apply shape. Resolves historical-gap questions 3 and 4 above as
   an explicit design step before writing code, **and confirms/creates the
   four finals weeks' own accepted `round_afl_mapping_revision`s
   (historical-gap question 6's finals half) — lifecycle creation/opening
   for a finals week needs one regardless of SuperScore's own mapping
   work, so this is this issue's scope too, not deferred to #192.** Any
   correction-triggered cascade it owns must take the shared season-row lock
   and reject a completed season before changing pairings or elimination
   history. Depends on: #197.
3. **[#191 — Finals lineup, scoring and publication](https://github.com/JustPlausible/BBBFFL_Scoring/issues/191)**
   — weekly lineup submission and lockout wired to the `finals` competition
   stream once #197 has landed (including the shared cross-stream fallback
   extension and, if #197 chooses parallel matchup storage, the required
   in-transaction correction-invalidation hook; this is not merely route
   stream-scoping);
   explicit bracket-participant eligibility enforcement `WeeklyLineup
   Repository` itself does not provide; the confirmed Week-1/seed-1 Week-2
   cross-stream carry-forward fallback (shared with #192's SS1 mechanism);
   **a finals-specific review-readiness/sign-off adapter, required
   unconditionally regardless of #197's path** (`app.round_review.
   build_round_review`/`attempt_signoff` hard-code "exactly five
   matchups" independent of the schema fork — see "Scoring" above); the
   new variable-match-count (one or two matches per week, never a fixed
   five — 2/2/1/1 across weeks 1-4) publish/correction command, built on
   whichever matchup shape #197/#190 produced, **which must itself freeze
   an `input_snapshot`-equivalent scoring-input record per matchup** (not
   merely reuse `publish_results`' versioning/immutability shape, since
   `_freeze_matchup_inputs` belongs to `attempt_signoff`, not to
   `publish_results` itself), **lock the same prerequisite matchup row(s)
   #190's "advance bracket" step locks, and — inside its own publish/
   correction transaction, before committing — separately lock and
   revalidate each published matchup's calculation row (revision/
   fingerprint) alongside its `bbbffl_matchup.review_version`**, since
   `MatchupCalculationService._persist` advances the former without ever
   touching the latter, and either alone can go stale between snapshot
   assembly and commit; public/coach/Scorer views covering all six finals
   matches. **Acceptance requires every finals result correction/cascade
   entry point to take the owning season-row lock in its write transaction
   and fail closed once the season is completed.** **Acceptance also
   requires the publish/correction command to recompute the round's
   matchups under one fresh `evidence_batch()` scope immediately before
   assembling its input snapshots, failing closed if not `evidence_fresh`
   (mirroring `signoff` and SuperScore's publisher), and requires the
   finals calculation path to hold an exclusive lock on each matchup's
   serialization row (`bbbffl_matchup` under #197's shared-table path;
   whichever always-present matchup/serialization row #197's
   parallel-storage path exposes otherwise — never unconditionally
   `bbbffl_matchup`, which does not exist for a finals match under that
   path) across its entire compute-then-persist window
   (acquired before reading inputs, held through persisting and
   committing) rather than comparing an `upstream_revision` value no live
   call site actually populates with an ordered marker** — so a matchup
   calculation using older AFL evidence can never overwrite one already
   computed from newer evidence. **If the bulk recompute's AFL-facts fetch
   uses a shared per-round cache (as `MatchupCalculationService.
   calculate_round`'s `_RoundFacts` does), acceptance requires locking
   every matchup the batch will touch, in deterministic order, before
   constructing or populating that cache** — a per-matchup lock alone does
   not guarantee the facts used were fetched after the lock was acquired.
   Depends on: #190.
4. **[#192 — SuperScore roster, eligibility and lifecycle setup](https://github.com/JustPlausible/BBBFFL_Scoring/issues/192)**
   — the `superscore` competition-stream round lifecycle (built on #197's
   chosen storage shape), weekly lineup submission/lockout wired to it
   (including the `app.lineups` in-transaction correction-invalidation
   extension for entry-scoped rulings),
   explicit all-ten-entries eligibility enforcement, the narrow SS1
   cross-stream fallback function (shared with #191's finals Week-1/seed-1
   mechanism), and the new **entry-scoped** DNP/Interchange/override ruling
   boundary (not a reuse of `app.round_review`'s matchup-keyed methods —
   see "DNP, Interchange and loophole rulings" under "SuperScore design").
   **Acceptance requires lifecycle setup to atomically create and verify all
   ten durable round/entry review-state rows before open**, and requires
   *every* write that changes an entry's effective submitted lineup — the
   initial submission, any unlocked pre-lockout resubmission through the
   ordinary `submit`/`submit_positions` path, a post-lockout correction and
   its stale-ruling invalidation, and every ruling/override — to lock the
   affected state row and advance its `review_version` in the same
   transaction. Scoping this to "correction" alone leaves an ordinary
   unlocked resubmission invisible to #193's publish-time CAS check. The
   state must exist independently of any calculation row.
   Resolves historical-gap question 6 (round mapping) for the SuperScore
   rounds as part of setup. Depends on: #197 (no longer independent of the
   finals track at the schema level, though its bracket-specific work
   remains independent of #190/#191's).
5. **[#193 — SuperScore scoring, leaderboard and publication](https://github.com/JustPlausible/BBBFFL_Scoring/issues/193)**
   — **the new entry-scoped calculation path** (reusing `app.scoring`'s
   formulas directly, since `MatchupCalculationService.calculate_round`
   iterates `bbbffl_matchup` rows and SuperScore deliberately has none —
   see "Scoring" under "SuperScore design"), the new leaderboard-shaped
   official-result representation as **one atomic round-level revision
   covering all ten entries** (never versioned per-entry in isolation),
   with each revision **freezing every entry's scoring inputs** the same
   way `bbbffl_official_result.input_snapshot` does for the ordinary case,
   ranking/joint-winner computation, a publish/correction command that
   **locks and re-verifies, per entry, the always-present review-state row and
   its shared review-revision counter that *every* effective lineup write
   (initial submission, unlocked resubmission, and correction alike) and
   ruling advances** — **never recalculation itself** (a calculation
   derives from truth, it is not new truth, and must not advance the
   counter — see below) —
   not merely the calculation revision, which none of those need
   touch — inside the same transaction that writes the new revision (the
   same race already fixed for bracket creation and advance-bracket,
   applied here, coordinated with #192's entry-scoped ruling boundary which
   must also advance this counter), and public/coach/Scorer views. **Acceptance
   requires calculation persistence to capture the entry's `review_version`
   before reading scoring inputs, then compare it against the row's current
   value under lock at persist time — aborting rather than persisting if it
   changed while computing, and, on success, recording that captured value
   as the calculation's own `computed_as_of_review_version` rather than
   advancing the row itself; publication must lock all ten state rows in
   deterministic order, reject a missing row, and reject any row whose
   *current* `review_version` does not equal its latest calculation's
   `computed_as_of_review_version` (not merely whatever version the
   publisher itself captured moments earlier) — a stale calculation must
   never pass by virtue of nothing racing during the publish transaction
   alone — **and, separately, also re-verify each entry's calculation
   row's own revision/fingerprint against the value captured at snapshot
   assembly, since a same-`review_version` recalculation (e.g. an
   upstream AFL-evidence correction) changes the calculation without
   changing `review_version` at all, and the review-state check alone
   cannot see it.** **Acceptance also requires the publish/correction
   command to recompute all ten entries under one fresh `evidence_batch()`
   scope immediately before assembling the snapshot and fail closed if not
   `evidence_fresh` (mirroring `round_review.py`'s existing `signoff`
   route), and requires the entry-scoped calculation path to hold an
   exclusive lock on the entry's `superscore_entry_review_state` row across
   its *entire* compute-then-persist window — acquired before reading any
   scoring/evidence inputs, held through persisting the result and
   committing — rather than comparing an `upstream_revision`/`upstream_
   observed_at` value (a column no live call site actually populates with
   an ordered marker — verified against `app/calculations.py` and
   `app/afl_resilience.py`'s `EvidenceBatch`, which exposes only a boolean
   `is_evidence_fresh()`). Serializing the compute window this way makes
   two calculations for the same entry mutually exclusive, so a later
   calculation (and the newer evidence it read) can never be overwritten by
   an earlier one that is still finishing — so a stale or out-of-order
   calculation can never become the entry's latest row and the leaderboard
   can never publish evidence older than current AFL facts.** **If the
   fresh-evidence-batch bulk recompute's AFL-facts fetch uses a shared
   per-round cache (as `MatchupCalculationService.calculate_round`'s
   `_RoundFacts` does), acceptance requires locking all ten entries, in
   deterministic order, before constructing or populating that cache** — a
   per-entry lock alone does not guarantee the facts used were fetched
   after the lock was acquired. Every
   SuperScore correction/republication must also take the
   owning season-row lock and reject a completed season in that same write
   transaction.**
   Depends on: #192.
6. **[#195 — End-of-season completion, premiership/wooden-spoon recording and 2026/2027 archival isolation](https://github.com/JustPlausible/BBBFFL_Scoring/issues/195)**
   — the premiership/wooden-spoon audit event and record, the
   season-completion lifecycle transition, and the focused implementation of
   `Season.lifecycle_state == "completed"` as a durable write fence for
   official historical results. **Acceptance requires:** (a) inventorying and
   guarding every existing and new result-changing repository boundary,
   including ordinary corrections, finals corrections/cascades, SuperScore
   corrections/republication, and derived premiership/wooden-spoon changes;
   each takes the owning season-row lock in its write transaction and rejects
   `completed`; (b) the completion command takes that same lock and rechecks
   under lock/CAS that every required finals round and SS1-SS4 are all `final`—
   Grand Final + SS4 is explicitly insufficient; (c) while still in that
   transaction, it locks the effective Grand Final result and frozen Round 20
   ladder/bracket provenance, then idempotently creates or appends superseding
   premiership and wooden-spoon versions whose references exactly match those
   locked inputs, failing closed if either award cannot be derived/persisted or
   remains missing or inconsistent; (d) only after the awards are valid does it
   record the completion audit event and mark the season completed, then commit;
   (e) a correction queued behind completion observes `completed` and mutates
   nothing; and (f) the committed command exposes a stable completed-season
   version and completion-event identifier for downstream consumers. Award
   re-recording is available only while active. No reopen bypass is included;
   any future reopen requires a separately designed, reason-carrying audited
   path that invalidates/supersedes the previous checkpoint marker. **Archive
   creation is explicitly not part of #195's acceptance criteria.** Depends
   on: #191 and #193 (needs every finals and SuperScore round to be
   finalisable).
7. **[#194 — Operator audit/correction/recovery support for finals and SuperScore](https://github.com/JustPlausible/BBBFFL_Scoring/issues/194)**
   — the finals/SuperScore-specific audit action catalogue, checkpoint
   procedure extending the second-half playbook (or a new
   `2026-finals-replay` evidence directory), and the operator playbook itself
   (the "not-yet-written" document section L of the second-half playbook already
   anticipates). **#194 exclusively owns final archive/checkpoint creation.**
   Its acceptance criteria must document and test consuming #195's committed
   completed-season version/completion-event identifier, creating the archive
   only afterward, and verifying that the evidence is bound to that exact
   version/event and contains or identifies both award versions. It must never
   checkpoint Grand Final + SS4 alone, omit either award, or capture the final
   archive from an unfenced active season. Depends on: #191, #193 and #195
   (needs the real operations and #195's stable completed boundary).

Each issue should carry: the relevant confirmed-rule excerpts
from this document (not a re-derivation), the specific historical-gap
question(s) it depends on being answered first, its acceptance criteria in
the same shape as #187's (explicit preview/apply-style checks, audit
provenance, fail-closed context checks, idempotency, focused tests), and an
explicit non-goal restating that it must not touch the mathematical ladder,
the finals-seeding snapshot's write path, or introduce a 2027 deployment,
configuration deliverable, or replay exception. That non-goal does not mean
the shared architecture should cease to be reusable by a normally configured
future season.

## Explicit non-goals (restated)

- No change to `app.ladder`, `app.finals_seeding`'s write path, or the
  mathematical-ladder-vs-historical-seed decision. Finals/SuperScore code
  only ever *reads* the seed order and its provenance (via the single-read
  mechanism in "Seed consumption" above), and freezes that result once —
  never a write to either module.
- No generic ladder or seeding editor of any kind.
- No 2027 deployment/configuration deliverable or replay exception is
  introduced by this design or its follow-up issues. Every new repository is
  season-scoped through the existing `competition_stream`/`season_id`
  mechanism, exactly like the ordinary competition — nothing here is
  2026-specific in the way `app.finals_seeding`'s `REPLAY_YEAR` gate is.
  A 2027 season configuring `finals`/`superscore` competition streams is
  expected to use this same reusable architecture normally; only the
  *seeding snapshot* is replay-only, and it already isolates itself. The
  non-goal is a 2027 deployment/configuration deliverable or a new 2027
  replay exception in these follow-ups — not reusability of the underlying
  season-scoped design.
- No fresh finals database/season bootstrap. Every new table/repository is
  keyed by the same `season_id` the completed Rounds 1-20 replay already
  uses.
