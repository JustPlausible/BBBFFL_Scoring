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

## Main finals design

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

### Team naming and matchup presentation

Reuse `app.identity.IdentityRepository`'s existing public season-entry/team-
name projection (`season_entry_team_name_history`) exactly as the ordinary
Round Centre does — no new team-naming concept. Bracket display should show
seed number alongside team name (e.g. "1. Running Hots"), matching the
workbook's own presentation convention.

### Round lifecycle and lockout

Reuse `app.lockouts.LockoutTriggerRepository`/`LockoutRepository` unchanged.
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
finals-stream predecessor exists at all: every finalist in Week 1, and
seed 1 specifically again in Week 2 (their only finals-stream history at
that point is a bye, which has no submitted lineup of its own). This is
the *same* cross-stream mechanism SuperScore's SS1 needs (see "SuperScore
design" below) — #191 and #192 should share one narrow cross-stream
fallback function rather than each building their own, and #191's
implementation should treat this as a confirmed requirement to build, not
an unresolved question to raise with Steve (historical-gap question 7,
below, is updated accordingly).

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
have (`docs/competition-lifecycle.md`). **A correction that changes who
advanced after the bracket has already progressed is a genuinely hard case**
(see "Historical gaps" below) and must not be silently resolved by this
design — it needs an explicit Scorer-facing decision path (recompute
downstream pairings vs. flag for manual review), which is exactly the kind
of thing `docs/plans/2027-season-model.md`'s design principle 8 ("audit
exceptional changes rather than silently rewriting history") anticipates
but does not itself resolve.

### Grand Final/season winner recording and end-of-season completion

New: an explicit `season.premiership.recorded` (or similar) audit event and
a small persisted record (premier `season_entry_id`, runner-up, Grand Final
official-result reference) once the Grand Final is published — this is the
"season winner" fact the 2027 season model's "Historical records" section
expects to exist. The wooden spoon (last place, 10th on the home-and-away
ladder — **not** finals-related) should be recorded the same way at the
same time, since both are facts available as soon as Round 20's ladder is
locked, independent of finals: no design tension there, just a small
addition. See "End-of-season completion" in the shared audit section below
for how this interacts with `Season.lifecycle_state`.

**Unresolved: what happens to these two records if the result they were
derived from is later corrected — flagged (Codex review, PR #196, tenth
round), not resolved by this document.** The ordinary competition supports
a post-final result correction (a new reason-carrying official-result
version, per `docs/competition-lifecycle.md`). If a Grand Final correction
changes the winner, or a Round 20 home-and-away correction changes who
finished last, the separately persisted premiership/wooden-spoon record
would keep naming the original entry unless something re-derives it — and
unlike a mid-bracket finals result, the Grand Final has no downstream
pairing to cascade through, so this is a distinct question from historical-
gap #4 above, not the same one restated. #195 must decide: (a) derive these
facts live from effective results on every read rather than persisting a
frozen record at all, or (b) persist a frozen record but give it its own
audited re-recording path triggered by a relevant correction. Confirm with
Steve which is intended, or propose one explicitly with tradeoffs, before
#195 implements either.

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
  `season.premiership.recorded`) — never repurposing an existing action.
- **Append-only, immutable published results**: finals and SuperScore
  official results follow the same "new version on correction, prior
  version protected by a database trigger" pattern as ordinary results and
  as `finals_seeding_snapshot`/`midseason_ladder_snapshot`.
- **Checkpoint timing**: the same discipline as
  `docs/2026-second-half-replay-playbook.md` sections G/I/K — a paired
  database/checkpoint backup after the finals-seeding snapshot is applied
  (already done, see the provenance manifest's "Round 20 / home-and-away
  boundary"), then after each finals week finalises and after each
  SuperScore round finalises, and a final end-of-season checkpoint once the
  Grand Final and SS4 are both published.
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
  "completed" 2026 season once 2027 exists. Whether this needs to become an
  enforced gate (and, if so, whether it belongs to this decomposition or a
  separate platform-hardening issue) is listed as a follow-up decision
  below rather than resolved here.

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
7. **No longer unresolved — reclassified as a confirmed historical rule
   after further Codex review of PR #196.** An earlier draft of this
   document listed the finals-stream Week 1 (and seed 1's Week 2) lineup
   fallback as an open question with no source stating the answer. That
   was incorrect: `docs/plans/2027-season-model.md`'s "Failure to submit"
   section does state it, using "premiership" as its own umbrella term for
   "ordinary home-and-away plus finals" (see that document's "All-time
   record scope" section for the same usage) — "for the normal home-and-away
   competition and continuing finals participation, this normally means the
   coach's previous premiership/home-and-away lineup," read together with
   the structurally identical, immediately-following SS1 rule. The
   mechanical rule is therefore: same-finals-stream carry-forward when a
   finals-stream predecessor exists (the common case), falling back
   cross-stream to the entry's most recent **ordinary** submission only
   when it doesn't (every finalist's Week 1, and seed 1's Week 2 again).
   This is now a **confirmed requirement** for #191 to implement (see
   "Coach lineup/submission behaviour" above), sharing its cross-stream
   fallback function with SuperScore's SS1 mechanism (#192) rather than
   each building its own. Nothing here remains to ask Steve about, unless
   implementation uncovers a genuine further ambiguity the season model
   text does not resolve (e.g. an exact tie-break if both an ordinary and a
   finals-stream predecessor arguably exist in some edge case).

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
   **both** finals and SuperScore, not just finals scoring. Pure
   platform/schema package; no bracket or SuperScore-specific logic.
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
   an explicit design step before writing code. Depends on: #197.
3. **[#191 — Finals lineup, scoring and publication](https://github.com/JustPlausible/BBBFFL_Scoring/issues/191)**
   — weekly lineup submission and lockout wired to the `finals` competition
   stream (no new lineup code, only stream-scoping, once #197 has landed);
   explicit bracket-participant eligibility enforcement `WeeklyLineup
   Repository` itself does not provide; the confirmed Week-1/seed-1 Week-2
   cross-stream carry-forward fallback (shared with #192's SS1 mechanism);
   **a finals-specific review-readiness/sign-off adapter, required
   unconditionally regardless of #197's path** (`app.round_review.
   build_round_review`/`attempt_signoff` hard-code "exactly five
   matchups" independent of the schema fork — see "Scoring" above); the
   new variable-match-count (never one, never five — 2/2/1/1 across
   weeks 1-4) publish/correction command, built on whichever matchup shape
   #197/#190 produced; public/coach/Scorer views covering all six finals
   matches. Depends on: #190.
4. **[#192 — SuperScore roster, eligibility and lifecycle setup](https://github.com/JustPlausible/BBBFFL_Scoring/issues/192)**
   — the `superscore` competition-stream round lifecycle (built on #197's
   chosen storage shape), weekly lineup submission/lockout wired to it,
   explicit all-ten-entries eligibility enforcement, the narrow SS1
   cross-stream fallback function (shared with #191's finals Week-1/seed-1
   mechanism), and the new **entry-scoped** DNP/Interchange/override ruling
   boundary (not a reuse of `app.round_review`'s matchup-keyed methods —
   see "DNP, Interchange and loophole rulings" under "SuperScore design").
   Resolves historical-gap question 6 (round mapping) for the SuperScore
   rounds as part of setup. Depends on: #197 (no longer independent of the
   finals track at the schema level, though its bracket-specific work
   remains independent of #190/#191's).
5. **[#193 — SuperScore scoring, leaderboard and publication](https://github.com/JustPlausible/BBBFFL_Scoring/issues/193)**
   — **the new entry-scoped calculation path** (reusing `app.scoring`'s
   formulas directly, since `MatchupCalculationService.calculate_round`
   iterates `bbbffl_matchup` rows and SuperScore deliberately has none —
   see "Scoring" under "SuperScore design"), the new leaderboard-shaped
   official-result representation, ranking/joint-winner computation,
   publish/correction command, and public/coach/Scorer views. Depends on:
   #192.
6. **[#194 — Operator audit/correction/recovery support for finals and SuperScore](https://github.com/JustPlausible/BBBFFL_Scoring/issues/194)**
   — the finals/SuperScore-specific audit action catalogue, checkpoint
   procedure extending the second-half playbook (or a new
   `2026-finals-replay` evidence directory), and the operator playbook
   itself (the "not-yet-written" document section L of the second-half
   playbook already anticipates). Depends on: #191 and #193 (needs real
   operations to document).
7. **[#195 — End-of-season completion, premiership/wooden-spoon recording and 2026/2027 archival isolation](https://github.com/JustPlausible/BBBFFL_Scoring/issues/195)**
   — the premiership/wooden-spoon audit event and record, the
   season-completion lifecycle transition, and a decision (with
   implementation if warranted) on whether `Season.lifecycle_state ==
   "completed"` should become an enforced write-blocking gate before 2027
   begins. Depends on: #191 and #193 (needs the Grand Final and SS4 to
   actually be publishable), and generally follows #194.

Each issue, when filed, should carry: the relevant confirmed-rule excerpts
from this document (not a re-derivation), the specific historical-gap
question(s) it depends on being answered first, its acceptance criteria in
the same shape as #187's (explicit preview/apply-style checks, audit
provenance, fail-closed context checks, idempotency, focused tests), and an
explicit non-goal restating that it must not touch the mathematical ladder,
the finals-seeding snapshot's write path, or introduce any 2027 capability.

## Explicit non-goals (restated)

- No change to `app.ladder`, `app.finals_seeding`'s write path, or the
  mathematical-ladder-vs-historical-seed decision. Finals/SuperScore code
  only ever *reads* the seed order and its provenance (via the single-read
  mechanism in "Seed consumption" above), and freezes that result once —
  never a write to either module.
- No generic ladder or seeding editor of any kind.
- No 2027/live-season capability, flag, or exception introduced by this
  design or its follow-up issues. Every new repository described above is
  season-scoped through the existing `competition_stream`/`season_id`
  mechanism, exactly like the ordinary competition — nothing here is
  2026-specific in the way `app.finals_seeding`'s `REPLAY_YEAR` gate is.
  A 2027 season configuring `finals`/`superscore` competition streams is
  expected to use this same code normally; only the *seeding snapshot* is
  replay-only, and it already isolates itself.
- No fresh finals database/season bootstrap. Every new table/repository is
  keyed by the same `season_id` the completed Rounds 1-20 replay already
  uses.
