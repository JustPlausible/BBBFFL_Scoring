# Season completion (the `active → completed` gate) and archival verification

Issue #240. This is the **supported, production-safe** way to complete a
correctly finished BBBFFL season through the browser, and to verify its
archival checkpoint afterward. It closes
`docs/2027-live-season-readiness.md`'s remaining item 4: `app.season_
completion.preview_complete_season`/`complete_season` and `app.season_
archival.verify_season_completed_for_archival` were already fully domain-
and replay-proven (issues #194/#195), but their only wired entry points were
the 2026-replay-only operator CLIs (`scripts/season_completion_2026.py`,
`scripts/season_archival_checkpoint_2026.py`), both of which refuse outright
under `BBBFFL_ENVIRONMENT=production` -- correctly, since they are
2026-replay tooling, not live-season tooling. No browser route ever called
either domain function, so a live 2027 production deployment had no
supported way to complete a season or verify its archival checkpoint at
all.

- Page: `/scorer/season-completion/{season_id}`. Open it from the Season
  Centre's **Season completion** link, or from the Administrator Dashboard's
  **Season completion ready** card once every prerequisite is satisfied.
- API: `/api/scorer/season-completion/{season_id}` (see "Routes" below).
- Service: `app/season_completion.py` (unchanged domain logic) plus
  `app/season_archival.py` (unchanged read-only verification). Route:
  `app/routes/season_completion.py`.

This module is a thin HTTP translation layer only -- it reimplements no
readiness rule, no award derivation and no archival check. It is the exact
same shape as `app.routes.season_activation`'s browser gate for the sibling
`setup → active` transition: a read-only preview endpoint, an explicit
confirmation endpoint, and a page that shows one before ever allowing the
other.

## What "ready" means

`preview_complete_season` never invents a second readiness engine -- it
re-derives, fresh on every call, exactly what `complete_season` itself would
check:

| Check | Ready when |
|---|---|
| **Season lifecycle** | The season is currently `active` (not `setup`, and not already `completed`). |
| **Finals** | A finals bracket exists, with all four weeks (Grand Final included) mapped to a BBBFFL round, and every one of those rounds is `final`. |
| **SuperScore** | The SuperScore stream exists, with SS1-SS4 all present and `final`. |

Checking only the terminal labels (the Grand Final, SS4) is explicitly
insufficient -- every required round is checked individually, and a blocked
preview names exactly which round(s) are not yet final.

**"Ordinary-season requirements incomplete" is the same fact as "no finals
bracket exists yet."** A finals bracket can only be created once every
regular-season round is final (`FinalsBracketRepository.create_bracket`'s
own ladder-seed gate) -- so an ordinary season that has not finished cannot
have reached Finals yet, and `preview_complete_season` reports "season ...
has no finals bracket yet; cannot complete season" for it. This is the
existing domain readiness rule, reused as-is; this page does not add a
second, redundant ordinary-round-finality check.

**An internal-consistency finding can still surface at completion time even
once every round above is final.** Finals seeding is frozen once, at
bracket creation (never re-read), but the *live* ordinary ladder
`app.season_awards` reads for the wooden spoon is not. If an ordinary result
is corrected after the bracket was created and produces an exact tie for
last place, `complete_season`'s award-derivation step (not the round-state
readiness gate) refuses with `UnresolvedWoodenSpoonTieError`. This route
maps that -- and the sibling `AwardNotReadyError` -- to a 409, not a 500;
see `tests/test_season_completion_api.py`'s and `tests/test_season_
completion.py`'s identical constructions of this scenario.

## Completion is always explicit

Completion never happens as a side effect of a plain page load or the
readiness preview -- only `GET` requests are involved in reaching and
reading this page's state. The only way a season moves from `active` to
`completed` is a deliberate Scorer/Administrator confirmation: a reason is
required, a native browser confirmation dialog warns that the action is
irreversible, and the resulting page immediately reflects the new lifecycle
state, the completed-season version and the completion-event identifier.

## What completion does (unchanged from issue #195)

`complete_season` is the existing, unmodified six-step atomic transaction:

1. Lock the owning season row (`SeasonRepository.guard_writable`) -- fails
   closed if the season is not `active` (including if it is already
   `completed`; there is no reopen pathway).
2. Fail closed unless every required finals week and all four SuperScore
   rounds (SS1-SS4) are `final`.
3. Idempotently create or supersede the Premiership and Wooden Spoon
   `season_award` records, referencing the effective Grand Final result and
   the live Round-N ladder.
4. Record the `season.completed` completion audit event.
5. Transition the season `active → completed` (appending the existing
   `season.lifecycle.changed` event too).
6. Commit.

Any failure at any step rolls back the whole transaction -- no partial
award, no completion event, no lifecycle change. This route surface adds no
new write path; it only calls this transaction and translates its result
(or its typed exceptions) to HTTP.

## Terminal state and completion-event identity

A successful completion response shows:

- the resulting season record, including `lifecycle_state: "completed"`;
- `completed_season_version` and `completion_event_id` -- the exact
  identifiers `app.season_archival.verify_season_completed_for_archival`
  independently re-derives, so an operator never has to trust a value
  copied from a log;
- the Premiership and Wooden Spoon winners, resolved to their team names
  (via the existing `IdentityRepository.get_public_team` read model -- the
  same reuse `app.finals_superscore_dashboard.build_finals_progression_
  preview` already relies on for team-name display, not a second identity
  derivation), and whether each award was newly recorded or already
  matched an existing one (idempotency).

Revisiting the page after the season is already `completed` (in a later
session, or after a page refresh) shows the terminal lifecycle state and
automatically re-confirms the completion identity via the archival
verification endpoint below -- the completion event is never something only
the operator who happened to click the button can see again.

## Archival verification

`GET /api/scorer/season-completion/{season_id}/archival-verification`
wraps `app.season_archival.verify_season_completed_for_archival` unchanged.
It is:

- **read-only** -- it takes no lock, starts no transaction, and never
  migrates the schema. It is safe to call repeatedly, from any number of
  operators, without side effects (`tests/test_season_completion_api.py`'s
  `test_archival_verification_after_completion_reports_the_completion_
  identity` calls it three times in a row and asserts nothing changes).
- **production-safe by construction, independent of the 2026-replay-only
  `scripts/season_archival_checkpoint_2026.py`** -- that script's own
  production guard is untouched by this issue; this route is a genuinely
  separate entry point into the same underlying verification function, not
  a bypass of that script's refusal.
- **explicit about what it verified**: it reports `completed_season_
  version`, `completion_event_id`, when that completion event occurred, and
  when this verification ran. Optionally, an operator can pass
  `expected_completion_event_id` (a value already recorded elsewhere, e.g.
  from provenance notes) and get a 409 if the currently observed completion
  event does not match -- catching the case where archival evidence would
  otherwise silently bind itself to a different completion than the one the
  operator reviewed.
- refused with a clear 409 if the season is not `completed` yet -- the
  archival checkpoint must never be taken against a season completion has
  not actually observed.

This is the production path for the same procedure
`2026-finals-replay/workflow-findings.md` describes for the replay
environment: verify the completion identity, record it, *then* take the
paired database backup -- never the other way around.

## Repeated or invalid attempts

- Completing an already-`completed` season is refused (409) via
  `app.season.SeasonCompletedError` -- `complete_season` is not
  idempotently re-callable once it has succeeded (issue #195's explicit
  scope: no reopen pathway), so a second attempt is a clear refusal, never
  a silent second success.
- Completing a season with any required finals week or SuperScore round not
  yet `final` is refused (409) with the blocking round(s) named. Nothing is
  written.
- A missing or blank reason is refused (409). Nothing is written.
- The lower-level domain capability (`app.season_completion.complete_
  season`) is unchanged and still directly usable by anything that already
  called it (the 2026 replay tooling included) -- this issue adds the
  browser gate on top of it, it does not remove or restrict that existing
  capability.

## Authorization

Every route requires Scorer or Administrator authority
(`app.authorization.require_scorer_or_admin`) plus
`require_role_covers_season` -- the identical authority boundary issue
#239's season activation uses, narrower than `app.routes.season_setup`'s
Secretary-inclusive capability. A season-scoped Scorer grant only reaches
the season it was actually issued for. Cookie-session writes (the `POST
.../complete`) need the double-submit CSRF token; the read-only `GET
.../archival-verification` does not, since it never mutates. Coaches and
spectators are refused (401/403), and every refusal is proven to change
neither the season's lifecycle state nor the audit trail
(`tests/test_season_completion_api.py`).

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/scorer/season-completion/{season_id}` | Page |
| GET | `/api/scorer/season-completion/{season_id}` | Read-only readiness preview: lifecycle state, ready/not-ready, every required round's state |
| POST | `/api/scorer/season-completion/{season_id}/complete` | `{reason}` -- the atomic `active → completed` transition |
| GET | `/api/scorer/season-completion/{season_id}/archival-verification` | Read-only: confirms the season is `completed` and reports its completion identity. Optional `?expected_completion_event_id=` |

## What this does not change

- `app.season_completion.py`/`app.season_awards.py`/`app.season_archival.py`
  are unchanged except for one additive field: `preview_complete_season`'s
  report now also carries `lifecycle_state` (the season's current lifecycle
  state), needed so the browser page can tell "not yet active", "active,
  not ready", "active, ready" and "already completed" apart without parsing
  `diagnostic`'s free text -- the same field `app.season_activation.
  ActivationReadiness` already exposes for the sibling gate. No readiness
  rule, award-derivation rule or archival check changed.
- `scripts/season_completion_2026.py` and
  `scripts/season_archival_checkpoint_2026.py` are untouched, including
  their own `BBBFFL_ENVIRONMENT=production` refusal. They remain 2026-
  replay-only tooling; this issue adds a separate, production-safe browser
  path to the same underlying domain functions, not a way to make the
  replay scripts run in production.
- A policy for exact ladder ties (`docs/2027-live-season-readiness.md`
  remaining item 7) is still not implemented -- this issue only ensures the
  existing refusal surfaces cleanly (409, atomic, no partial write) through
  the browser rather than as an unhandled 500.

## Evidence

- `tests/test_season_completion.py`: the six-step atomic command itself
  (unchanged from issue #195), plus two new regression cases added for
  issue #240 -- refusal when no finals bracket exists yet (the concrete
  shape of "ordinary-season requirements incomplete"), and atomic refusal
  of an internal-consistency wooden-spoon tie introduced by a result
  correction *after* the finals bracket was already frozen.
- `tests/test_season_completion_api.py`: the page and JSON API through real
  HTTP requests and real Scorer/Administrator/Secretary/Coach sessions --
  readiness preview, browser-preview-vs-explicit-confirmation semantics,
  successful completion with terminal-state/award/team-name display,
  refusal for each of ordinary/Finals/SuperScore incompleteness and the
  internal-consistency tie (all 409, never 500, all unchanged/no mutation),
  idempotency, archival verification (read-only, production-safe,
  completion-identity matching/mismatch), authorization (including
  Secretary/Replay Operator refusal, a season-scoped grant that does not
  reach a different season, and CSRF) and the 404 unknown-season case.
- `tests/test_season_archival.py`/`tests/test_season_archival_checkpoint_
  cli.py`: the underlying verification function and its 2026-replay-only
  CLI, unchanged.
- `tests/test_architecture.py`'s `SEASON_COMPLETION`/`ROUTES` groups.

This is automated-test-proven. It has not been rehearsed in a real
production deployment or against the 2026 replay season (which was, and
remains, completed only through the historical CLI/domain call this issue
adds a browser alternative to).
