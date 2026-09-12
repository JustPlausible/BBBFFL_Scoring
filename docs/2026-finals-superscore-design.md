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
  one integration seam any finals/SuperScore code must call for seed order.
  It already handles both cases (a 2026 snapshot exists; no snapshot exists,
  so fall back to the mathematical ladder) with no special-casing required
  at the call site, and every non-2026 season takes the second path
  automatically because a snapshot can only ever exist for `year == 2026`.
  **Finals/SuperScore implementation must consume this function's return
  value; it must never recompute or override seed order itself.**
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

Every place the finals bracket needs "the top five" or "which seed beat
which seed" must call `app.finals_seeding.resolve_finals_seed_order` exactly
once per decision and treat its returned tuple as the authoritative seed
order — never re-deriving from `app.ladder.LadderRepository.snapshot`
directly, and never hard-coding the 2026 team names or IDs. This is what
makes the same bracket-generation code correct for a future season that has
no seeding snapshot at all (falls through to the mathematical ladder
automatically).

**Which `competition_id` to pass matters and is easy to get wrong.**
`resolve_finals_seed_order(database, season_id, competition_id)` resolves
the snapshot (or the mathematical-ladder fallback) *for that
`competition_id`* — and the 2026 finals-seeding snapshot is scoped to the
**ordinary** home-and-away `competition_id` (`9de8e7d3-8d56-4c5c-
afd0-803b787e4055`, per `provenance-manifest.md`), because that is the
`competition_id` `FinalsSeedingRepository.apply` was called against. A new
`finals`-typed `competition_stream` is a *different* `competition_id`.
Calling `resolve_finals_seed_order` with the finals `competition_id`
instead of the ordinary one will not find the snapshot (it is scoped to a
different ID) and will silently fall through to computing a mathematical
ladder *for the finals competition* — which has no Round 1-20 results at
all, since those live under the ordinary stream. **The finals bracket
module must therefore retain and pass the ordinary home-and-away
`competition_id` to `resolve_finals_seed_order`, separately from the new
`finals` `competition_id` it creates its own rounds/rulings under.** This
is not an edge case to discover during implementation; #190's acceptance
criteria must test it explicitly (Codex review, PR #196).

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

Reuse `app.lineups.WeeklyLineupRepository` unchanged, scoped to the `finals`
competition stream's rounds. The nine-position structure, private-draft-vs-
submit distinction, and resubmission-while-unlocked rules are identical to
an ordinary round. Failure-to-submit carry-forward for a finals round
sources from that entry's most recent finals-stream submission — but two
cases have **no** finals-stream predecessor at all and therefore no
same-stream source: every finalist in Week 1 (the finals stream's first
round), and seed 1 specifically again in Week 2, since seed 1's only
finals-stream history at that point is a bye with no submitted lineup of
its own. See historical-gap question 7 below; this is not resolved by this
document and must not be silently invented by #191.

### Scoring

Reuse `app.calculations.MatchupCalculationService`/`app.scoring` unchanged
— the same nine-position formulas, the same DNP/Interchange/override
machinery (`app.round_review`), the same calculated-vs-official-result
separation. Nothing about BBBFFL scoring changes for finals.

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

### Public, coach and Scorer/operator views

- **Public:** a finals bracket page (who plays whom, this week's/each
  week's result, who is eliminated, who advances) — the legacy Grand Final
  prototype's single-matchup detail view is reasonable evidence for what
  the "click a match to see live positional detail" experience should look
  like, generalised to whichever of the four matches is selected.
  `docs/plans/2027-season-model.md`'s "Public spectator scope" already
  confirms a public finals bracket is intended.
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
This needs a small, distinct, explicitly-named function (e.g. `app.
superscore.resolve_ss1_fallback_source` or similar, in whichever module ends
up owning SuperScore) that performs exactly one narrow, audited cross-stream
copy — SS1 only, ordinary-competition source only, coach's most recent
*submitted* ordinary lineup only — never a generic "carry forward from any
other stream" capability. This is a small, well-bounded addition, not a
reason to widen `app.carry_forward` itself; flagging it here so the
SuperScore follow-up issue does not have to rediscover it.

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
`app.lineup_validation` unchanged, scoped to the `superscore` competition
stream's rounds. `app.participation.assess_participation` is also reusable
unchanged — it is a pure, stateless evidence-assessment function with no
`matchup_id` dependency at all.

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
| Nine-position lineup structure/validation | yes | yes | yes | `app.lineups`, `app.lineup_validation` unchanged |
| Staged lockout (selective/main triggers) | yes | yes | yes | `app.lockouts` unchanged |
| DNP/Interchange/override ruling storage | yes | yes | **no** (entry-scoped, not `matchup_id`-keyed) | `app.round_review` reused for ordinary/finals only; new entry-scoped ruling boundary for SuperScore |
| Participation evidence assessment | yes | yes | yes | `app.participation.assess_participation` unchanged (matchup-independent) |
| Scoring formulas | yes | yes | yes | `app.scoring` unchanged |
| Calculated-vs-official separation *implementation* | yes | yes | **no** (entry-scoped, not `matchup_id`-keyed) | `app.calculations.MatchupCalculationService` reused for ordinary/finals only; new entry-scoped calculation path for SuperScore |
| Player ownership/eligibility | yes | yes | yes | `app.player_pool` unchanged |
| Same-stream carry-forward | yes | yes | yes (SS2+) | `app.carry_forward` unchanged |
| Cross-stream carry-forward (ordinary Round 20 -> SS1) | n/a | n/a | **SS1 only** | new narrow function, see above |
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
  snapshot**: every finals/SuperScore repository call that needs a seed
  order calls `resolve_finals_seed_order`, so the resulting bracket/
  leaderboard rows are inherently traceable to the same `finals_seeding_
  snapshot_id` (or, for a season with none, the same Round 20 `LadderSnapshot`)
  already recorded in `provenance-manifest.md`. No separate cross-reference
  mechanism needs to be built.
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
   populated and validated for Rounds 1-20, as an early step of the finals-
   lifecycle follow-up issue, not assumed from the general "AFL Rounds
   21-24" description in the season model.
7. **Unresolved: the finals-stream Week 1 (and seed 1's Week 2) lineup
   fallback** (raised during Codex review of PR #196). A finals-stream
   round's normal fallback is carry-forward from that entry's own previous
   finals-stream submission (see "Coach lineup/submission behaviour"
   above), but two cases genuinely have no such predecessor: every
   finalist's very first finals-stream round (Week 1), and seed 1
   specifically again in Week 2 (their only finals-stream history at that
   point is a bye, which has no submitted lineup at all). No source
   reviewed (season model, decisions, workbook findings) states whether
   this should (a) fall back to that entry's most recent *ordinary*
   lineup — the same cross-stream pattern already confirmed for SuperScore
   SS1, which would mean the narrow cross-stream fallback function
   described under "SuperScore design" needs a finals-aware sibling or
   generalisation rather than being SuperScore-only; (b) require an
   explicit scorer/proxy-entered lineup with no automatic carry-forward,
   matching `app.carry_forward`'s existing "Round 1 has no predecessor"
   behaviour (`NoCarryForwardSourceError`); or (c) something else. Confirm
   with Steve, or propose one explicitly with tradeoffs, before #191
   implements finals lineup submission — it must not be silently invented
   mid-PR.

## Follow-up issues

Decomposed at boundaries that can each land as an independently reviewable,
independently testable PR, matching the granularity `app.finals_seeding`
and `app.midseason_draft` were each delivered at. Recommended execution
order (each row's "Depends on" names the prerequisite rows):

1. **[#190 — Finals bracket generation and lifecycle](https://github.com/JustPlausible/BBBFFL_Scoring/issues/190)**
   — `app.finals`: bracket creation from `resolve_finals_seed_order`, the
   four-week pairing/progression state machine, elimination recording, the
   finals round lifecycle, and a CLI-first operator tool mirroring
   `scripts/finals_seeding_2026.py`'s preview/apply shape. Resolves
   historical-gap questions 3 and 4 above as an explicit design step before
   writing code. Depends on: this document.
2. **[#191 — Finals lineup, scoring and publication](https://github.com/JustPlausible/BBBFFL_Scoring/issues/191)**
   — weekly lineup submission and lockout wired to the `finals` competition
   stream (no new lineup code, only stream-scoping); the new single-match
   publish/correction command described above; public/coach/Scorer views.
   Depends on: #190.
3. **[#192 — SuperScore roster, eligibility and lifecycle setup](https://github.com/JustPlausible/BBBFFL_Scoring/issues/192)**
   — the `superscore` competition-stream round lifecycle, weekly lineup
   submission/lockout wired to it, the new narrow SS1 cross-stream fallback
   function, and DNP/Interchange reuse. Resolves historical-gap question 6
   (round mapping) for the SuperScore rounds as part of setup. Depends on:
   this document (independent of #190-#191; can run in parallel).
4. **[#193 — SuperScore scoring, leaderboard and publication](https://github.com/JustPlausible/BBBFFL_Scoring/issues/193)**
   — the new leaderboard-shaped official-result representation,
   ranking/joint-winner computation, publish/correction command, and
   public/coach/Scorer views. Depends on: #192.
5. **[#194 — Operator audit/correction/recovery support for finals and SuperScore](https://github.com/JustPlausible/BBBFFL_Scoring/issues/194)**
   — the finals/SuperScore-specific audit action catalogue, checkpoint
   procedure extending the second-half playbook (or a new
   `2026-finals-replay` evidence directory), and the operator playbook
   itself (the "not-yet-written" document section L of the second-half
   playbook already anticipates). Depends on: #191 and #193 (needs real
   operations to document).
6. **[#195 — End-of-season completion, premiership/wooden-spoon recording and 2026/2027 archival isolation](https://github.com/JustPlausible/BBBFFL_Scoring/issues/195)**
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
  only ever *reads* `resolve_finals_seed_order`'s result.
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
