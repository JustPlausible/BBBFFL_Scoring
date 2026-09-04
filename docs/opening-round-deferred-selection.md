# Opening Round deferred selection and compensating-bye scoring

**Status:** implemented, issue [#69](https://github.com/JustPlausible/BBBFFL_Scoring/issues/69) (follow-up to #31's exceptional round mapping); discoverability/presentation/readiness extended by issue [#131](https://github.com/JustPlausible/BBBFFL_Scoring/issues/131); nomination read-model duplication fixed and explicit per-entry submission confirmation added by issue [#133](https://github.com/JustPlausible/BBBFFL_Scoring/issues/133)<br>
**Implementation:** `bbbffl_app/app/opening_round.py` (domain writes, plus
issue #131's `describe_accepted_rule(s)`/`resolve_afl_club_name` presentation
helpers, issue #133's `OpeningRoundSubmissionRepository` confirmation
boundary and `list_for_season_entry` read model, and
`build_opening_round_readiness`'s confirmation-based readiness read model),
integrated into `bbbffl_app/app/calculations.py` (per-slot scoring-source
resolution), `bbbffl_app/app/lineup_validation.py` (bye/DNP distinction),
`bbbffl_app/app/lineups.py`'s `WeeklyLineupRepository.submit`/
`submit_positions` (via the same optional `lock_guard` integration point
`app/lockouts.py` uses), `bbbffl_app/app/routes/delegated_operations.py` (the
represented-operator Opening Round Operations API/page, including the
confirm/reopen endpoints), `bbbffl_app/app/season_centre.py`/
`routes/season_centre.py` (discoverability and season-wide confirmation
readiness), and `bbbffl_app/app/round_preflight.py` (confirmation/integrity
readiness gates for a dependent ordinary round)<br>
**Schema:** `bbbffl_app/migrations/versions/0020_opening_round_deferral.py`
(`opening_round_rule`/`opening_round_rule_revision`, `opening_round_nomination`)
and `bbbffl_app/migrations/versions/0023_opening_round_submission.py`
(`opening_round_submission`/`opening_round_submission_revision`, issue
#133's explicit confirmation boundary), see
[`database-migrations.md`](database-migrations.md) -- issue #131 added no
schema, only read-model resolution over existing storage<br>
**Evidence:** [`docs/evidence/opening-round/`](evidence/opening-round/) (raw
2024/2025/2026 captures), `bbbffl_app/tests/opening_round_evidence.py`
(distilled facts), `bbbffl_app/tests/test_opening_round.py`,
`bbbffl_app/tests/test_opening_round_operations_api.py` (issue #131 HTTP
coverage)<br>
**Depends on:** the accepted BBBFFL-to-AFL round mapping
([`round-afl-mapping.md`](round-afl-mapping.md), issue #31), weekly lineup
drafts/submissions ([`weekly-lineups.md`](weekly-lineups.md), issue #33),
staged AFL-match lockouts ([`lockouts.md`](lockouts.md), issue #34), and the
canonical scoring/DNP boundaries ([`scoring-calculations.md`](scoring-calculations.md),
[`dnp-interchange-recommendations.md`](dnp-interchange-recommendations.md))

## Purpose

The AFL introduced an "Opening Round" (AFL round number 0) in 2024, 2025 and
2026, contested by only some clubs each year; every participating club later
received a compensating bye in a different, season-specific ordinary round.
BBBFFL's historical rule let a coach (or, in this pre-authentication
codebase, a scorer/admin acting as their proxy) **nominate** an Opening
Round player they owned into a specific future BBBFFL lineup slot. When that
later, compensating-bye round was scored, that one slot drew its statistics
from the player's **Opening Round** match rather than being an ordinary
bye/DNP -- while every other slot in the same lineup continued to score from
the round's ordinarily mapped AFL round, exactly as before.

This is a **season-scoped capability, never a hard-coded year**. A season
that never configures it (2027 with a conventional Round 1, or any earlier
season) behaves identically to one without this module at all -- see
"Explicit season configuration" below.

## Two kinds of mapping (do not confuse them)

### Whole-round AFL→BBBFFL mapping (issue #31, `app.round_mapping`)

The normal AFL round supplying fixture/stat context for an *entire* BBBFFL
round. Every ordinary slot in every lineup in that round draws its facts
from the same one AFL round.

### Player-level deferred scoring-source mapping (this document, issue #69)

One individual BBBFFL slot instead draws its facts from a *different* AFL
round -- the player's own Opening Round match -- because that player was
previously nominated/locked and their AFL club's compensating bye lands in
the round being scored. This is never a whole-round remap:

```text
BBBFFL Round N
F1 -> AFL Round N statistics
F2 -> AFL Round N statistics
M1 -> AFL Opening Round statistics
      ↑
      previously nominated Opening Round player
      whose club has its compensating bye
      in the AFL round associated with BBBFFL Round N
R  -> AFL Round N statistics
```

A BBBFFL round can, and often does, contain **both** kinds of source in one
lineup at once -- see "Mixed-source scoring" below. `docs/round-afl-mapping.md`
cross-references this document for exactly this distinction.

## Evidence: what the supplied captures establish (and don't)

Three raw upstream AFL captures were supplied for this issue and are
preserved verbatim under [`docs/evidence/opening-round/`](evidence/opening-round/)
(see that directory's README for why they are not reshaped into
`tests.afl_evidence`'s `afl-api` v1 fixture contract). They are
**AFL-side facts, classified `known_fact`**: which clubs played Opening
Round each year, and which AFL round carried each participating club's
compensating bye.

| Season | AFL Opening Round | Participating clubs | Compensating bye round(s) |
|---|---|---|---|
| 2024 | round id 954 | BL, CARL, COLL, GCFC, GWS, MELB, RICH, SYD | R2 (956): BL, CARL · R3 (957): GCFC, GWS · **R5 (959): COLL, SYD** · **R6 (960): MELB, RICH** |
| 2025 | round id 1146 | COLL, GWS, HAW, SYD | R2 (1148): GWS · R3 (1149): COLL, SYD · R4 (1150): HAW |
| 2026 | round id 1343 | BL, CARL, COLL, GCFC, GEEL, GWS, HAW, STK, SYD, WB | R2 (1345): BL, CARL, COLL, GEEL · R3 (1346): GCFC, HAW, SYD, WB · R4 (1347): GWS, STK |

2024's later R5/R6 compensating byes are the clearest evidence against any
`Opening Round -> R2..R4` shortcut: the pattern is season-specific and must
be configured, never assumed. 2025's much smaller four-club structure shows
the same for participation itself.

### Evidence boundary

These captures establish **AFL facts** (`AFL season -> Opening Round
participation -> later AFL club bye`) only. They do **not**, by themselves,
prove any BBBFFL-side fact: the exact historical BBBFFL round that received
a deferred score, an individual coach's historical nomination, the selected
BBBFFL position, the actor who made it, or whether every eligible player was
actually nominated. This repository holds no historical BBBFFL nomination
record for any of the three seasons -- the workbook findings
(`docs/plans/2026-workbook-findings.md`) confirm the *mechanism* existed but
do not specify exact historical nominations. Every nomination exercised in
`tests/test_opening_round.py` is therefore an explicitly **synthetic test
scenario** (see "Replay evidence classifications" below), never a claimed
historical fact. `docs/round-afl-mapping.md`'s existing "2026 evidence"
section already anticipated this exact gap ("Opening Round performances
were historically deferred to a club's later bye, but does not fully
specify a generally safe mapping rule... belongs in a separate follow-up");
this document is that follow-up, and remains equally conservative about
what it claims as fact.

## Domain model

Two tables, reusing existing season/player/ownership/mapping/audit
identities rather than inventing parallel ones:

### `opening_round_rule` / `opening_round_rule_revision` -- configuration

Season+club scoped, versioned exactly like `round_afl_mapping`
(`app.round_mapping.RoundMappingRepository`): the header is stable identity
(`season_id`, `afl_club_id`); each revision carries `state`
(`unresolved`/`ambiguous`/`accepted`), the AFL season/Opening Round/
compensating-bye-round identities, the corresponding BBBFFL target round,
and an evidence classification. **Acceptance is the only activation
boundary** -- a `propose()`d `unresolved`/`ambiguous` revision is
non-operational, exactly like an unresolved `round_afl_mapping` revision.

### `opening_round_nomination` -- decision

The player-level record: which owned season player, nominated by which
operator (acting as proxy), into which slot of which BBBFFL round, under
which accepted rule, resolved against which Opening Round AFL match. Unlike
the rule table, a nomination is corrected **in place** (an audited UPDATE),
because `app.audit`'s append-only event log already retains the
before/after/actor/reason trail a correction needs -- a second parallel
revision-history table would duplicate that. Two partial-unique invariants
(mirroring `weekly_lineup_draft_slot`'s "a player cannot occupy multiple
scoring positions") ensure at most one nomination per target slot and one
nomination per target player, both scoped to `(bbbffl_round_id,
season_entry_id)`.

A third invariant, `UNIQUE(rule_id, season_entry_id)`, existed from
migration 0020 until migration 0024 (issue #135) removed it: an accepted
rule maps one AFL club's Opening Round to its compensating-bye target
round, but that says nothing about how many of that club's *owned* players
a coach may nominate into that target round's distinct slots -- a genuine
historical submission can and did nominate more than one same-club player
in one target round (e.g. two Collingwood players in distinct Round 2
slots), each still subject to the two slot/player invariants above.
`OpeningRoundNominationRepository.nominate` no longer pre-checks
`(rule_id, season_entry_id)` uniqueness either -- only the slot and player
checks remain.

## Explicit season configuration

Nothing in `app.opening_round` infers activation from `season == 2024`,
`2025` or `2026`; an AFL round numbered/named "Opening Round"; or a club
having *any* later bye. `OpeningRoundRuleRepository.accept()` is the only
activation path, and it validates both the Opening Round and compensating
bye round references through the same public `afl-api` v1 boundary
`app.round_mapping` uses (`AflReferenceValidator`). A season/club with no
accepted rule cannot have a nomination created against it at all --
`OpeningRoundNominationRepository.nominate()` raises `UnknownRuleError`.

## Nomination workflow

`OpeningRoundNominationRepository.nominate(rule_id, season_entry_id,
position, season_player_id, afl_client, actor=..., reason=...)` validates,
in order:

1. the rule is `accepted`;
2. `position` is a legal BBBFFL scoring slot;
3. the player belongs to the rule's season;
4. the player is currently owned by the nominating entry
   (`player_ownership_period`);
5. the player's cached AFL club matches the rule's `afl_club_id`;
6. the AFL Opening Round evidence actually resolves that club to exactly
   one match (`app.lockouts.resolve_match` -- reused, not reimplemented);
7. the two slot-uniqueness invariants above (target slot, target player) --
   deliberately **not** a limit on how many nominations one entry may have
   under the same rule (issue #135).

Every write requires `actor.actor_type == "anonymous_operator"` with
`actor_role` `scorer`/`admin` (`UnauthorizedNominationActorError`
otherwise) -- the same pre-authentication proxy convention
`app.lineup_proxy` already established: **a scorer/replay operator acting
as proxy is never recorded as though the historical coach personally
authenticated and performed the action.**

## Locked future position

`OpeningRoundNominationRepository.preload_target_lineup(...)` seeds a
nominated slot into the target round's private draft
(`weekly_lineup_draft_slot`, via `WeeklyLineupRepository.save_draft`) --
persisted domain state, not a UI-only flag -- so the slot already appears
filled before a coach (or proxy) ever visits that round. It is idempotent
and touches only positions with an active nomination.

`OpeningRoundSelectionGuard` is a `lock_guard`-shaped object -- the exact
integration point `app/lockouts.py`'s `LockGuard` already uses on
`WeeklyLineupRepository.submit`/`submit_positions` -- that rejects any
submitted change to a locked slot, whatever the submission source
(ordinary coach/proxy edit, resubmission, or carry-forward's verbatim
copy). A carry-forward whose source round would overwrite a locked slot
with a different player therefore fails explicitly
(`DeferredSlotLockedError`) rather than silently invalidating the
nomination.

Read-model inspectability: `OpeningRoundNominationRepository.
deferred_context(bbbffl_round_id, season_entry_id, position)` returns the
rule identity, source Opening Round, evidence classification and
provenance for one slot, and `app.lineup_validation`'s
`opening_round`/`deferred_selection_active` message (see below) surfaces
the same on the ordinary validation surface.

## Interaction with staged lockout

Opening Round deferred locking is deliberately independent of, and does not
weaken, `app.lockouts`' staged AFL-match lockout (issue #34).
`OpeningRoundSelectionGuard` accepts an `inner` lock_guard (an ordinary
`app.lockouts.LockGuard`): it enforces its own absolute lock on nominated
slots, then **excludes those slots** before delegating everything else to
`inner`. This exclusion matters mechanically, not just semantically: the
deferred player's club is, by construction, on its ordinary bye in the
*target* round, so it has no AFL match there for the ordinary lockout's
`resolve_match` to resolve against -- attempting to evaluate it under the
ordinary rules would fail with `MatchResolutionError`, not merely
"unnecessary". Every other, non-deferred position in the same lineup keeps
its normal staged-lock behaviour (editable until its own match's lock
boundary, then frozen) untouched. `tests/test_opening_round.py::
test_deferred_lock_and_ordinary_staged_lockout_coexist_in_one_lineup`
demonstrates both mechanisms active in one lineup.

## Per-player scoring-source resolution (the central mechanism)

`app.calculations.MatchupCalculationService` no longer assumes every slot
in a round draws from the same AFL round. `_RoundFacts` is a small
per-AFL-round match/stat cache keyed by AFL round ID; `_entry()` looks up
`_deferred_positions()` for the lineup's round/entry and, for a deferred
slot, resolves `_RoundFacts.matches(afl_opening_round_id)` instead of the
round's ordinary mapped matches. Both paths then call the **same**
`app.scoring.score_position` formula -- there is no separate Opening Round
scoring engine, and no arbitrary historical fantasy score is ever injected.
Each calculated slot's evidence records `scoring_source`
(`"ordinary"`/`"opening_round_deferred"`) and `source_afl_round_id`, so a
mixed-source round's provenance stays inspectable in the persisted
`bbbffl_matchup_calculation` snapshot `app.round_review` already reads.

### Mixed-source scoring

```text
BBBFFL target round
ordinary slot
-> current/mapped AFL round
-> current AFL match statistics
-> canonical BBBFFL scoring
deferred slot
-> historical Opening Round nomination
-> locked future position
-> club's configured compensating bye
-> Opening Round AFL match statistics
-> same canonical BBBFFL scoring
```

Both branches coexist in the same lineup/result -- see
`tests/test_opening_round.py::test_mixed_source_lineup_uses_canonical_scoring_for_both_sources`.

## Bye and DNP semantics

A valid deferred player is **not** an ordinary bye/unavailable player,
**not** automatically DNP, **not** automatically zero, and **not**
automatically eligible for Interchange replacement merely because the
target round has no ordinary AFL match for them:

- `_entry()` assesses a deferred slot's participation with
  `bye_team_ids=None` (never the *ordinary* round's bye list, which -- by
  the very nature of the rule -- does include the deferred player's club).
  Real Opening Round statistics, when resolved, classify as
  `played_with_stats`/`participated_zero_stats` exactly like any other
  played match, tagged `source="opening-round-deferred"` for provenance.
- If the Opening Round evidence itself cannot resolve a match/stat line for
  the player (evidence has gone missing or was never resolvable), the slot
  is `unknown`/`review_required` with a reason naming the Opening Round
  explicitly -- never silently zero, never silently DNP.
- A genuinely ordinary bye elsewhere in the *same* lineup (a different,
  non-deferred player whose club has an ordinary scheduled bye) is
  completely unaffected -- `app.participation.assess_participation`'s
  `club_bye` classification and `app.lineup_validation`'s `afl_club_bye`
  warning are unchanged.
- `app.lineup_validation.LineupValidationService._add_availability`
  distinguishes the two on the ordinary validation surface: a slot with an
  active nomination gets `category="opening_round"`,
  `code="deferred_selection_active"` instead of the generic
  `afl_club_bye` warning, so a scorer/coach never confuses a valid,
  already-scoring deferred slot with an ordinary unavailable one.

## Weekly lineup and carry-forward interaction

- `preload_target_lineup` applies the persisted nomination to its slot when
  the target round's lineup is created/read.
- `OpeningRoundSelectionGuard` prevents the prior week's ordinary player
  (via carry-forward) or any ordinary coach/proxy edit from replacing that
  slot -- both fail with `DeferredSlotLockedError`, never a silent
  overwrite.
- Every other position in the lineup follows normal
  draft/submission/resubmission/carry-forward rules untouched.
- If no valid nomination exists for a round/entry, nothing is preloaded and
  nothing is locked -- a season/round never fabricates a deferred
  assignment merely because a player's club happens to have a bye.

## Corrections and audit

`OpeningRoundNominationRepository.correct(nomination_id, position=...,
season_player_id=..., actor=..., reason=...)` requires an operator actor
and a reason, and appends an `opening_round.nomination.corrected` audit
event recording the original (`before_state`) and replacement
(`after_state`) position/player alongside actor and timestamp -- the
existing append-only `app.audit` boundary, not a duplicated correction
mechanism. It never touches a published official result; if a correction
is needed *after* a round has been signed off, that is issue #58's
separate scorer round-review/correction workflow, not this module's
concern.

## Replay evidence classifications

The same four classifications `docs/roadmap/2027-season-roadmap.md`
establishes for replay evidence generally (`app.opening_round.
EVIDENCE_CLASSIFICATIONS`):

1. **known_fact** -- the AFL-side captures in `docs/evidence/opening-round/`
   (participation, Opening Round identity, compensating bye round).
2. **reconstructable_behaviour** -- a scoring result deterministically
   derived from a known nomination and confirmed AFL facts.
3. **synthetic_scenario** -- every nomination in `tests/test_opening_round.py`
   (no historical BBBFFL nomination record exists in this repository).
4. **unresolved_scorer_input** -- a nomination whose evidence cannot be
   resolved at scoring time (see "Bye and DNP semantics" above).

`opening_round_rule_revision.evidence_classification` persists this
classification for the AFL-side rule itself (defaulted to `known_fact` for
the 2024/2025/2026 captures), inspectable via
`OpeningRoundRuleRepository.resolve()`/`deferred_context()`.

## Unresolved historical BBBFFL mappings

No exact historical BBBFFL round/slot/coach mapping is established for any
2024/2025/2026 Opening Round deferral in this repository -- only the
AFL-side facts above. This is recorded here explicitly, not silently
guessed, and remains open input for #66/#67's replay work: any 2026 replay
that needs a specific historical nomination must supply it as
scorer-confirmed evidence (`known_fact`) or explicitly flag it
`unresolved_scorer_input`, never infer it from the CFS fixture alone.
## 2026 first-half replay bootstrap activation

The 2026 first-half replay bootstrap (issue #126, `bbbffl_app/app/replay_bootstrap.py`) establishes the ten accepted 2026 Opening Round rules -- one per participating club -- through this module's ordinary `OpeningRoundRuleRepository.accept()` before the draft, using an explicit `opening_round` replay-config section and a local-evidence-backed validator (never a live AFL-api client, never ad-hoc SQL). It creates no nominations: those remain later, session-native operator input entered through the workflow below only once the draft has produced owned players. See `docs/2026-first-half-replay-bootstrap.md`'s "Opening Round rule activation" section for the full contract.

## Replay operator browser workflow

After selecting an authorised represented entry in shared acting context, open
**Opening Round Operations** from its **Season Centre** link (issue #131) --
visible only once at least one Opening Round rule is accepted for the season,
never before that, and never requiring the operator to know or type
`/operations/seasons/<season-id>/opening-round` directly. Season Centre also
shows confirmation progress there (e.g. `7/10 teams confirmed`) and which
entries still require action, driven by the same `app.opening_round.
build_opening_round_readiness` read model the Opening Round Operations page
and Round Preflight's incomplete-confirmation blocker both reuse -- one
readiness computation, not three.

### Explicit submission confirmation (issue #133)

The historical BBBFFL process was always a **partial** Opening Round
submission: a coach/proxy could nominate zero or more eligible owned players
and deliberately leave the rest vacant. Owning an eligible player, or an
accepted rule existing for a club represented in an entry's squad, never
itself requires a nomination for that club -- the Opening Round Operations
page shows owned players and accepted rules purely as *selection guidance*,
never as a checklist of required nominations.

Readiness therefore keys entirely off an explicit, persisted
per-`(season, season entry)` **submission confirmation**
(`app.opening_round.OpeningRoundSubmissionRepository`), not off how many
nominations exist. The operator flow is:

1. Enter zero or more nominations for the represented entry (as before).
2. Click **Confirm Opening Round submission** -- valid with zero
   nominations, or any partial legal set. This records confirmation time and
   the authenticated replay operator's provenance (never the historical
   coach), and is idempotent: confirming an already-confirmed submission
   again is a no-op.
3. Once confirmed, the submission is locked: creating or correcting a
   nomination for that entry is refused (`SubmissionConfirmedError`, HTTP
   409) until an operator explicitly **reopens** it with a reason -- an
   audited operation (`OpeningRoundSubmissionRepository.reopen`) that writes
   a new `draft` revision rather than silently mutating the confirmed one.
   The full confirm/reopen history remains readable on the page.
4. Season/round readiness (`N/10 teams confirmed`) counts confirmed season
   entries; a dependent ordinary round stays blocked until every season
   entry has confirmed.

Integrity diagnostics (duplicate/mismatched/conflicting nominations) remain
a defensive check over persisted state, entirely independent of
confirmation completeness: a confirmed entry whose nomination data is later
corrupted (a trade-away, a rule correction race) still shows as confirmed,
but the corruption is reported separately and still blocks the round
(`opening_round_integrity_conflict`). Since issue #135, "duplicate" here
means a genuine duplicate target slot or duplicate target player within one
`(bbbffl_round_id, season_entry_id)` -- both already structurally
impossible through `OpeningRoundNominationRepository.nominate`/`correct` by
the two remaining schema-level unique constraints, so this is belt-and-
braces re-verification, not primary enforcement. Multiple distinct
nominations sharing one accepted rule for one entry are valid and are never
reported as duplicates.

If schema support was required for this boundary, it is a normal migration
(`migrations/versions/0023_opening_round_submission.py`) adding
`opening_round_submission`/`opening_round_submission_revision`, versioned
exactly like `opening_round_rule`/`opening_round_rule_revision` -- there is
no row at all until an entry's submission is first confirmed.

### Represented-entry nomination read model (issue #133)

The represented-entry GET response previously iterated every accepted rule
and called `OpeningRoundNominationRepository.list_for_round(rule.
bbbffl_round_id)` once per rule, re-emitting a persisted nomination once for
every accepted rule that happened to share its target BBBFFL round -- e.g.
four accepted rules targeting the same compensating-bye round rendered that
round's one nomination four times. `list_for_season_entry(season_id,
season_entry_id)` now queries `opening_round_nomination` directly by its own
season/entry columns, returning each persisted nomination exactly once
(deterministically ordered), with its correction/audit history rendered
once alongside it. This was a read-path defect only: the underlying
persisted nominations were never duplicated, and no rollback or data
correction was required.

The page lists only accepted Opening Round rule revisions, rendered with a
human-readable primary label (`app.opening_round.describe_accepted_rule`),
e.g. `GWS Giants — Opening Round → compensating AFL Round 4 → BBBFFL Round
2`, resolved from the already-cached `season_player_pool.afl_team_name`, the
persisted `bbbffl_round.label`, and `afl_client.get_rounds` -- never a raw AFL
club ID or BBBFFL round UUID as the primary label (those remain visible as
secondary diagnostics). The owned-player list is server-scoped to the
represented entry's authoritative current squad only (`season_player_id`,
display name, AFL club ID and name, canonical player ID) -- never an
arbitrary player query filtered client-side. Choosing an owned player
identifies the matching accepted club rule automatically and previews the
compensating AFL round, target BBBFFL round and target position before
confirmation; the browser still submits the resolved `rule_id`, and the
server independently revalidates the player's club, ownership, season and
slot -- the browser's preview is never trusted as the authority. Enter the
replay reason and create the nomination: the existing nomination repository
validates rule, club, ownership, slot, player and conflicts exactly as
before, and the existing preload operation places the selection in the
target weekly draft. The delegated lineup displays it as **Opening Round
deferred — locked** and submission remains protected by the server-side
deferred-selection guard.

Corrections use the page's audited correction operation and require a
reason. Recorded nominations render with the same human-readable player
name/club, represented team, source Opening Round/compensating AFL
round/target BBBFFL round, target position and correction history as the
primary display; internal identifiers (nomination/rule/player IDs) remain
available as secondary diagnostics only. Browser-created records are
labelled **replay/reconstructed input entered by the authenticated
operator**. This wording is intentional: the available AFL evidence
establishes the Opening Round structure, but does not establish that a
historical BBBFFL coach made a particular nomination -- and nominations must
never be inferred, guessed, or backfilled from later AFL statistics or match
results.

## Where an ordinary round depends on Opening Round nominations (issues #131, #133)

`app.round_preflight.build_round_preflight` checks whether any accepted
Opening Round rule targets the round being prepared. If so, and any season
entry has not yet explicitly confirmed its Opening Round submission, opening
is blocked with an `opening_round_nominations_incomplete` readiness blocker
naming the unconfirmed entries and linking directly back to Opening Round
Operations for that season -- never silently inferred or auto-created, and
never derived from how many nominations exist or how many eligible clubs an
entry's squad happens to represent (issue #133). Separately, a
`opening_round_integrity_conflict` blocker reports any duplicate/mismatched/
conflicting nomination affecting this round's rules -- confirmation
completeness and nomination integrity are independent signals, so a
confirmed entry whose nomination data is later corrupted (a trade-away, a
rule correction race) still blocks the round via the integrity blocker, not
by reverting to "incomplete". The same preflight view's existing "Opening
Round exceptions" section (already-locked deferred selections for this
round) is rendered with the same human-readable player/club/round labels
described above, not raw identifiers.
