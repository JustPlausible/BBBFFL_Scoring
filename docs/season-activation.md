# Season activation (the `setup → active` gate)

Issue #239. This is the **supported, production-safe** way to move a
correctly prepared BBBFFL season from `setup` to `active` through the
browser. It closes `docs/2027-live-season-readiness.md`'s remaining item 3:
the season lifecycle already supports this transition
(`SeasonRepository.transition_lifecycle`), but before this issue no browser
action ever performed it -- the 2026 replay season stayed `setup` through
the whole season and was only moved to `active` (then `completed`) via
CLI/domain calls at closeout.

- Page: `/scorer/season-activation/{season_id}`. Open it from the Season
  Centre's **Season activation** link, or from Season setup's own nav bar.
  The Administrator Dashboard also shows a **Season activation ready** card
  once every prerequisite below is satisfied.
- API: `/api/scorer/season-activation/{season_id}` (see "Routes" below).
- Service: `bbbffl_app/app/season_activation.py`. Route:
  `bbbffl_app/app/routes/season_activation.py`.

This module is a sibling of `app/season_completion.py` -- the same shape
(a read-only preview function plus an atomic command function) for the
`setup → active` transition that `season_completion.py` already has for
`active → completed`. It is deliberately not part of `app/season_setup.py`
(issue #237's fresh-season/phase initialization): activation has a
narrower authority (Scorer/Administrator only, not Secretary) and a
different readiness question (is the season *ready to run*, not merely
*initialized*), so it is kept a separate, composable application service.

## What "ready" means

The readiness preview never invents a second "is this season ready"
definition -- every check re-reads the same authoritative repositories
every other page already uses:

| Check | Source of truth | Ready when |
|---|---|---|
| **Season entries** | `IdentityRepository.list_entries` | Exactly ten season entries exist. |
| **Player pool and completed squads** | `PlayerPoolRepository.summary`, `player_ownership_period`/`season_squad_configuration` | The player pool has been populated, and every season entry owns exactly the configured squad limit's worth of active players. |
| **Ordinary competition** | `SeasonRepository.list_competitions`/`list_rounds` | Exactly one `ordinary` competition stream exists, with exactly Rounds 1 to `regular_season_round_count`. |
| **Fixture-number draw** | `FixtureRepository.get_draw` | A fixture draw exists and is `frozen`. |
| **Preseason draft** | `DraftRepository.status` | The preseason draft is finalized (every pick complete, and the opening-squad freeze has happened) -- not merely accepted. |

Every check is independent, and a blocked one names exactly what is
missing (for example "8 of 10 season entries are established" or "the
fixture-number draw has not been created yet"), not a generic `false`.
A contradictory state -- for example two `ordinary` competition streams --
is refused with its own explicit diagnosis, the same way
`app.season_setup` refuses an ambiguous structure.

None of this is a second readiness engine competing with `app.season_setup`'s
own read model: it reads the same underlying tables that model's steps
already read, just resolved to the stricter facts activation itself needs
(a *finalized* draft, not merely an *accepted* order).

## Activation is always explicit

Activation never happens as a side effect of another workflow -- not draft
completion, not fixture freezing, not season bootstrap, not team naming.
The only way a season moves from `setup` to `active` is a deliberate
Scorer/Administrator confirmation on this page: a reason is required, a
native browser confirmation dialog is shown before the request is sent, and
the resulting page immediately reflects the new lifecycle state.

## Repeated or invalid attempts

- Activating an already-`active` season is refused with a clear message
  ("season ... is already active; no further action is needed") -- it is
  never a silent no-op and never writes a second audit event.
- Activating a `completed` season is refused via the same completed-season
  write fence (`SeasonRepository.guard_writable`) every other
  result-changing write in this application uses.
- Activating a season with any missing or contradictory prerequisite is
  refused (409) with every blocking check named. Nothing is written.
- The lower-level domain capability
  (`SeasonRepository.transition_lifecycle(season_id, "active", ...)`) is
  unchanged and still directly usable -- this issue adds the browser gate
  and its readiness rules on top of it, it does not remove or restrict that
  existing capability.

## Audit

Activation reuses the season lifecycle's existing audit event
(`season.lifecycle.changed`, action constant
`app.season.SEASON_LIFECYCLE_CHANGED`) rather than inventing a second,
competing record of the same fact -- see
[`audit-events.md`](audit-events.md). One event is written, in the same
transaction as the lifecycle update, recording:

- the season (`entity_type="season"`, `entity_id=season_id`);
- the acting Scorer/Administrator (`ActorContext`, from the resolved
  session or the legacy shared token);
- the timestamp (`occurred_at`);
- the supplied reason;
- `before_state={"lifecycle_state": "setup"}` and
  `after_state={"lifecycle_state": "active"}`.

A rejected activation attempt -- missing prerequisites, wrong role, wrong
lifecycle state, a missing reason -- writes no audit event at all, and
leaves the season's `lifecycle_state` unchanged
(`tests/test_season_activation.py`, `tests/test_season_activation_api.py`).

## Authorization

Every route requires Scorer or Administrator authority
(`app.authorization.require_scorer_or_admin`) plus
`require_role_covers_season`. This is **stricter** than
`app.routes.season_setup`'s `roundsetup.manage` capability, which also
grants a Secretary -- issue #239 names only Scorer and Administrator, so a
Secretary (and a Replay Operator) is refused here even with a grant scoped
to the exact season. A season-scoped Scorer grant only reaches the season
it was actually issued for. Cookie-session writes need the double-submit
CSRF token. Coaches and spectators are refused (401/403), and every
refusal is proven to change neither the season's lifecycle state nor the
audit trail (`tests/test_season_activation_api.py`).

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/scorer/season-activation/{season_id}` | Page |
| GET | `/api/scorer/season-activation/{season_id}` | Read-only readiness preview: lifecycle state, ready/not-ready, every named check |
| POST | `/api/scorer/season-activation/{season_id}/activate` | `{reason}` -- the atomic `setup → active` transition |

## Evidence

- `tests/test_season_activation.py`: readiness preview (ready, and every
  specific/contradictory blocker), successful activation with its audit
  event, a missing reason, missing prerequisites (unchanged, no audit
  event), repeated activation, and the completed-season write fence.
- `tests/test_season_activation_api.py`: the page and JSON API through
  real HTTP requests and real Scorer/Administrator/Secretary/Coach
  sessions -- authorization (including Secretary and Replay Operator
  refusal, a season-scoped grant that does not reach a different season,
  and CSRF), 409/400 refusal shapes, and the 404 unknown-season case.
- `tests/test_architecture.py`'s `SEASON_ACTIVATION` group.

This is automated-test-proven. It has not been rehearsed in a real
production deployment or against the 2026 replay season (which was, and
remains, activated only through the historical CLI/domain call this issue
adds a browser alternative to -- see
`2026-finals-replay/workflow-findings.md` finding 10).

## Not in scope

- Season completion (`active → completed`) has its own domain command
  (`app.season_completion.complete_season`) but, per
  [`2027-live-season-readiness.md`](2027-live-season-readiness.md), still
  has no browser or production entry point -- a separate remaining item.
- A policy for exact ladder ties.
- Provisional (not-yet-afl-api) players.

These remain separate items in
[`2027-live-season-readiness.md`](2027-live-season-readiness.md).
