# Round Preflight (issue #152)

Round Preflight (`app/round_preflight.py`, `app/routes/round_preflight.py`,
`app/templates/round_preflight.html`) is the operator surface for deciding,
per BBBFFL round: which AFL season/round it maps to, which AFL matches
constitute its lockout plan, and whether it is safe to open. This document
describes the evidence-backed recommendation workflow issue #152 added on
top of the pre-existing authoritative mapping/trigger model
([`round-afl-mapping.md`](round-afl-mapping.md), [`lockouts.md`](lockouts.md)).
Nothing here changes that underlying model: `RoundMappingRepository` and
`LockoutTriggerRepository` remain the only persistence/audit boundaries, and
every mutation still goes through them, with a reason recorded exactly as
before.

## The problem this solves

Before issue #152, an operator configured a round's AFL mapping by typing a
raw `afl_season_id`/`afl_round_id` pair, and its lockout plan by typing
comma-separated AFL match IDs -- both opaque provider identities with no
on-screen evidence of what they meant, and no protection against
typing/pasting a match ID that did not actually belong to this round's
mapped AFL round. Round Preflight now shows human-readable labels first,
keeps provider IDs only as secondary/reference detail, and offers
deterministic, explained recommendations wherever the evidence supports
one -- while treating every recommendation as strictly advisory.

## Recommendation vs. authoritative acceptance

**Nothing is ever recommended and then silently applied.** Every
recommendation this page shows is computed fresh on every read, from
whatever the configured AFL client (live `AflApiClient` or the replay
`ReplayAflDataSource` -- see "Live/replay parity" below) currently reports,
and is never itself persisted:

- **Mapping recommendation** (`app.round_mapping.recommend_mapping`): the
  one AFL season whose published year matches this BBBFFL season, and
  within it the one AFL round whose published round number matches this
  BBBFFL round's sequence -- exactly the "normal case" documented in
  [`round-afl-mapping.md`](round-afl-mapping.md). If zero or more than one
  season/round matches, no recommendation is shown; there is no partial or
  best-guess answer.
- **Lockout plan recommendation** (`app.round_preflight.recommend_lockout_plan`):
  groups the mapped round's matches by scheduled start time. If every match
  shares one start time, a single main/remaining trigger covering all of
  them is suggested; otherwise the earliest-starting group becomes a
  suggested selective trigger and every other match becomes the suggested
  main trigger. Any match with a missing scheduled start or an unrecognised
  status makes the whole suggestion unsafe, so none is shown.
- **Replay checkpoint recommendations** (`app.round_preflight._replay_checkpoint_recommendations`):
  see "Replay checkpoint recommendations" below.

Accepting a mapping requires the operator to explicitly tick a confirmation
control and supply a reason (`accept_preflight_mapping`'s `confirmed`/
`reason`), regardless of whether the chosen season/round matches the
recommendation or not -- there is no "accept recommendation" shortcut that
skips this. Configuring a lockout trigger likewise always requires an
explicit `configure_preflight_trigger` call with the operator's own chosen
match selection; clicking "Use this stage" on a recommended plan only fills
the trigger form's fields (key/type/sequence/checked matches) and never
itself issues a request (see `app/templates/round_preflight.html`'s
`useLockoutStage`/`useMappingRecommendation`, and the Node-driven regression
coverage in `tests/test_round_preflight_client_requests.py` proving neither
function calls `fetch`). Mapping acceptance and trigger configuration are
both server-side mutations (`POST /api/admin/round-preflight/{round_id}/mapping`
and `.../lockout-trigger`) -- never implemented purely in browser JavaScript.

## Deliberate mapping divergence

BBBFFL and AFL round numbering can legitimately diverge -- the clearest
case being BBBFFL finals, which map to specific late AFL home-and-away
rounds rather than following BBBFFL's own week numbering (see
[`round-afl-mapping.md`](round-afl-mapping.md)'s 2026 evidence). An operator
can always choose a season/round other than the recommended one (via the
dropdowns, or the advanced manual AFL-ID fallback) and accept it, provided
they give an explicit reason. `recommend_mapping` deliberately returns no
recommendation at all for such rounds -- it is gated to the ordinary
(home-and-away) stream and returns `None` unconditionally for any other
stream (finals, superscore, ...), since those streams' own sequence
numbering can coincidentally match an unrelated AFL round number and would
otherwise produce a confidently-labelled but wrong recommendation (see
[`round-afl-mapping.md`](round-afl-mapping.md)). So "diverging from the
recommendation" and "there simply is no recommendation" look the same to
the operator: an unprompted, unaided, but always-required explicit
decision.

## Chronological, human-readable match selection

Once a mapping is accepted, `build_round_preflight` loads that AFL round's
matches and always presents them in scheduled-start chronological order
(a match with no scheduled start sorts last, never first or arbitrarily).
Each match shows:

- a human-readable matchup (`"{home} v {away}"`);
- its scheduled UTC start time;
- the operator's own browser-local time for that same instant, computed
  client-side (`new Date(start_time_utc).toLocaleString()`) -- never
  computed or stored server-side, since "local" is a property of the
  viewer, not the round;
- its AFL match ID, shown only as secondary/reference detail;
- its currently observed AFL status (`UPCOMING`/`LIVE`/`POSTGAME`/etc.);
- separately, whether any configured BBBFFL lockout trigger covering this
  match has actually (durably) activated -- see the next section.

## Selective/main lockout configuration

Configuring a lockout trigger now means checking off matches from the
mapped round's own match list (human matchup labels as the primary
representation, AFL match ID secondary) rather than typing comma-separated
IDs. `configure_preflight_trigger` enforces, server-side, on every
create/replace:

- **Membership**: every selected AFL match ID must belong to the round's
  *currently* accepted mapping's match evidence -- a match ID that is not
  part of that mapping (a stale copy-paste, or a match belonging to a
  different round entirely) is rejected outright
  (`TriggerValidationError`).
- **An accepted mapping must exist** before any trigger can be configured
  at all.
- **Sequence ordering**: sequence numbers are unique within a round; every
  selective trigger's sequence must precede the round's one main/remaining
  trigger's sequence, and the main trigger's sequence must follow every
  selective trigger's. This is validated server-side regardless of what the
  browser sends -- the underlying model (`app.lockouts`) still allows zero
  or more selective stages followed by exactly one main trigger, and this
  validation only adds strict, deterministic ordering on top of that
  existing invariant.

Where the evidence allows one, a recommended selective/main plan is shown
(see above) -- but, as with mapping, it is never auto-persisted; the
operator must still explicitly configure (and give a reason for) each
stage they choose to adopt.

### Observed AFL status vs. persisted BBBFFL activation

A configured trigger's currently observed AFL match evidence (status,
scheduled start) is never the same thing as whether that trigger has
*actually* durably activated. `build_round_preflight` reads the trigger's
persisted activation record (`bbbffl_round_lockout_trigger_activation`,
written only by the deterministic lock evaluation in `app.lockouts` --
see [`lockouts.md`](lockouts.md)'s "Historical irreversibility") directly
and read-only: viewing this page never itself materializes or backdates an
activation. A trigger can activate purely because the evaluated instant
reached its covered match's scheduled start time, even while that match's
own AFL status still reads `UPCOMING` (`evaluate_match_lock`'s time-based
fallback) -- exactly the scenario `tests/test_round_preflight.py::test_trigger_activation_is_shown_separately_from_observed_afl_status`
exercises. Each match's own row also shows this same distinction per
trigger that covers it (`lockout_trigger_coverage`), so an operator never
has to infer BBBFFL lockout state from AFL status alone.

## Affected-player scope and readiness

Each configured trigger's view names which AFL clubs its covered matches
involve (`participating_clubs`) alongside its existing scope description
("players involved in the activating AFL match(es)" for a selective
trigger, "all remaining selections" for main). The existing readiness
blockers/advisories (missing mapping, invalid fixture, stale evidence,
unresolved lockout matches, an incomplete main trigger, Opening Round
dependencies, etc.) are unchanged by issue #152 and remain the sole
authority on whether "Open Round" is enabled.

## Replay checkpoint recommendations

Where the configured AFL client carries replay metadata (a `clock`
attribute -- present only on `app.replay.ReplayAflDataSource`, never on the
live `AflApiClient`), Round Preflight also shows advisory replay checkpoint
instants, using the same two stage values `app/replay_checkpoint.py`'s
checkpoint schema already recognises:

- one "just after" each currently configured trigger's earliest covered
  match (`stage: "scheduled"`) -- safe to derive from scheduled start time
  alone, since a trigger's own lock boundary is itself schedule-based
  (`evaluate_match_lock`);
- one safe final-results checkpoint (`stage: "final-results"`), but **only**
  once every relevant match's own currently observed status already reads
  as concluded (postgame/completed) -- recommended as "right now"
  (`afl_client.clock.now()`), never as a projected future instant from a
  match's scheduled *start* time. Recommending the latest match's start as
  a "final results" instant would suggest finalising the round the moment
  its last match begins, before it has actually concluded; lacking any
  other conclusion-time evidence, "now, once everything already shows
  concluded" is the only claim this can safely make, so no final-results
  recommendation is shown at all until that is true.

These are suggestions only:

- they never expose a host filesystem path (only `stage`,
  `recommended_effective_at`, and human-readable `evidence` text);
- Round Preflight never writes a checkpoint file or advances the replay
  clock itself -- applying a suggested instant remains a separate, explicit
  step through the existing replay checkpoint tooling
  ([`replay-checkpoint-2026.md`](replay-checkpoint-2026.md),
  [`replay-harness.md`](replay-harness.md)).

## Advanced/manual fallback

The season/round dropdowns (backed by `GET .../afl-seasons` and
`GET .../afl-seasons/{afl_season_id}/afl-rounds`) are the primary UI, but an
"Advanced: manual AFL season/round IDs" fallback remains available for
whenever human-readable listing is unavailable or an operator already knows
the exact provider IDs they need (e.g. a documented finals exception). The
manual fields, when both filled, take precedence over the dropdowns for
that submission. Either path submits through the same
`accept_preflight_mapping` server-side validation
(`AflApiReferenceValidator`, confirmation, reason, stale-revision check) --
the manual fallback is not a weaker path, only a different source for the
same two integers.

## Concurrency/audit correctness

The existing audited mapping/trigger revision model
([`round-afl-mapping.md`](round-afl-mapping.md), [`lockouts.md`](lockouts.md))
remains fully authoritative and untouched. Issue #152 adds optimistic
concurrency on top of it so a stale browser tab can never silently overwrite
a newer decision:

- `POST .../mapping` accepts `expected_revision` (0 meaning "no accepted
  mapping observed yet"). If the mapping's current revision no longer
  matches, `RoundMappingRepository.accept`/`correct` raises
  `StaleMappingRevisionError` (HTTP 409) instead of proceeding, and the
  existing mapping is left completely untouched.
- `POST .../lockout-trigger` accepts `expected_revision` per trigger key (0
  meaning "this trigger key does not exist yet"). A mismatch raises
  `StaleTriggerRevisionError` (HTTP 409) the same way, from
  `LockoutTriggerRepository.configure`.

Both checks are performed *inside* the same row-locked transaction that
advances the mapping's/trigger's `current_revision` -- not as a separate
read-then-compare step beforehand. That distinction matters: two concurrent
requests that both observe the same (soon-to-be-stale) revision would each
pass a standalone comparison before either commits, so only checking the
revision atomically, under the same lock that serializes the actual writes,
closes that race (issue #152 review). `LockoutTriggerRepository.configure`
additionally reads every trigger currently configured for the round under
that same lock, so the sequence-ordering/uniqueness validation above is
checked against a live, not stale, snapshot too.

Both checks are opt-in at the function level (a caller that omits
`expected_revision` skips them, preserving compatibility with any other
caller of `RoundMappingRepository`/`LockoutTriggerRepository`), but the
browser client always supplies the revision it last observed, so a normal
operator session is always protected. Either rejection still triggers the
page's existing reload-after-failure behaviour (issue #153): the browser
reloads the authoritative view rather than continuing to display what it
rendered before the rejected attempt.

## Live/replay parity

Every endpoint above (`afl-seasons`, `afl-seasons/{id}/afl-rounds`,
matches, checkpoint recommendations) is served through
`request.app.state.afl_client` -- the same duck-typed contract
`app.afl_client.AflApiClient` and `app.replay.ReplayAflDataSource` both
already implement (`get_seasons`, `get_rounds`, `get_matches`). A client
that does not support `get_seasons` (an older duck-typed test double, for
example) is treated as "listing unsupported", not an error: no season
dropdown options are shown, and no mapping recommendation is produced, but
nothing else in the page degrades.

## Out of scope

Dashboard/coach-facing presentation of any of this is explicitly out of
scope for issue #152; Round Preflight remains an operator/admin surface
only (`roundsetup.manage` capability, season-scoped role check).
