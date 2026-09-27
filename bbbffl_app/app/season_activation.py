"""Issue #239: the explicit, auditable `setup -> active` season-activation
gate.

`docs/2027-live-season-readiness.md`'s remaining item 3: the season
lifecycle already supports `setup -> active`
(`app.season.SeasonRepository.transition_lifecycle`/
`LEGAL_TRANSITIONS`), and `active -> completed` already has its own
browser-free but fully domain/audit-proven atomic command
(`app.season_completion.complete_season`). No browser workflow has ever
exercised `setup -> active` -- the 2026 replay season stayed `setup`
through the whole season and was only moved to `active` (then
`completed`) via CLI/domain calls at closeout
(`2026-finals-replay/workflow-findings.md` finding 10). This module is
the missing Scorer/Administrator browser gate for exactly that one
transition: a read-only readiness preview (`preview_activate_season`)
and the atomic activation command (`activate_season`) -- the same two-
function shape as `app.season_completion`'s `preview_complete_season`/
`complete_season` for the sibling `active -> completed` transition.

## Readiness

Reuses existing authoritative state; this module never invents a second
"is this season ready" definition. Every check below re-reads persisted
state fresh:

- **season entries** -- `app.identity.IdentityRepository.list_entries`,
  exactly `TEAM_COUNT` (10) required, the same constant and threshold
  `app.season_setup`/`app.admin_dashboard` already use for a fresh BBBFFL
  season;
- **player pool / completed squads** -- the season's player pool is
  populated (`app.player_pool.PlayerPoolRepository.summary`) and every
  season entry owns exactly the configured squad limit's worth of active
  players (`player_ownership_period`/`season_squad_configuration`);
- **competition state** -- the ordinary competition stream and its
  Rounds 1..`regular_season_round_count` exist
  (`app.season.SeasonRepository.list_competitions`/`list_rounds`), the
  same shape `SeasonRepository.initialize_ordinary_competition` creates;
- **fixture state** -- the fixture-number draw is accepted and frozen
  (`app.fixtures.FixtureRepository.get_draw`);
- **preseason draft state** -- the preseason draft is finalized (the
  opening-squad freeze), not merely accepted
  (`app.draft.DraftRepository.status`, `.is_complete`/`.is_finalized`).

`preview_activate_season` never locks a row and never mutates.
`activate_season` re-verifies every check inside the same season-row-
locked transaction as the lifecycle transition itself, so the preview is
advisory only -- exactly like `app.season_setup`'s own commands and
`app.season_completion.preview_complete_season`/`complete_season`.

## Safety properties

- Activation is refused outright -- never a silent no-op -- once the
  season is already `active` (a repeat activation attempt) or
  `completed`; `guard_writable`'s existing completed-season write fence is
  reused rather than re-implemented, and every message names the
  season's current state.
- The lifecycle transition and its audit event are the existing, single
  `app.season.SeasonRepository._transition_lifecycle_in_transaction` call
  (`season.lifecycle.changed`, with the previous/resulting
  `lifecycle_state`, actor and reason) -- this module never writes a
  second, competing audit record for the same fact.
- Every mutation requires an explicit, non-empty reason and is attributed
  to the acting Scorer/Administrator (`ActorContext`).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.audit import ActorContext
from app.db import transaction
from app.draft import DraftRepository
from app.fixtures import FixtureRepository
from app.identity import IdentityRepository
from app.player_pool import PlayerPoolRepository
from app.season import Season, SeasonCompletedError, SeasonNotFoundError, SeasonRepository

# BBBFFL is a fixed ten-team league -- the same structural constant
# `app.season_setup.TEAM_COUNT`/`app.admin_dashboard.BBBFFL_TEAM_COUNT`
# already use for a fresh season, kept local here rather than imported
# across an application-service boundary (see this module's own
# `tests/test_architecture.py` grouping).
TEAM_COUNT = 10


class SeasonActivationError(ValueError):
    """Base class for this module's domain errors (issue #239). A bare
    instance (e.g. a missing reason) maps to the generic 400 `ValueError`
    fallback in `app.main`; the more specific subclasses below register
    their own 409 handler, matching `app.draft.DraftOrderError`'s own
    base-class-plus-specific-subclasses convention."""


class SeasonActivationStateError(SeasonActivationError):
    """Activation was attempted against a season that is not currently
    `setup` -- already `active` (a repeat activation attempt) or
    `completed`. Never mutates; the message names the season's current
    lifecycle state."""


class SeasonNotReadyToActivateError(SeasonActivationError):
    """Activation's fail-closed readiness gate refused: at least one
    prerequisite is missing or contradictory. Never partially applied --
    nothing is written, and the message names every blocking check."""


@dataclass(frozen=True)
class ActivationCheck:
    key: str
    label: str
    ready: bool
    detail: str


@dataclass(frozen=True)
class ActivationReadiness:
    season_id: str
    lifecycle_state: str
    ready: bool
    checks: list[ActivationCheck]
    diagnostic: str | None


@dataclass(frozen=True)
class ActivationResult:
    season: Season
    previous_lifecycle_state: str


def _entries_check(database, season_id: str) -> ActivationCheck:
    count = len(IdentityRepository(database).list_entries(season_id))
    ready = count == TEAM_COUNT
    detail = (
        f"{count} of {TEAM_COUNT} season entries established"
        if ready
        else f"exactly {TEAM_COUNT} season entries are required (currently {count})"
    )
    return ActivationCheck("entries", "Season entries", ready, detail)


def _ordinary_competition_check(database, season: Season) -> ActivationCheck:
    """The same target shape `SeasonRepository.initialize_ordinary_competition`
    creates -- one `ordinary`-typed competition stream with exactly Rounds
    1..`regular_season_round_count` -- read directly from the season model
    rather than through `app.season_setup`'s own page-model helper (this
    module and `app.season_setup` are kept siblings, not a dependency of
    one another; see `tests/test_architecture.py`)."""
    seasons = SeasonRepository(database)
    competitions = [c for c in seasons.list_competitions(season.season_id) if c.stream_type == "ordinary"]
    if len(competitions) > 1:
        return ActivationCheck(
            "competition_state",
            "Ordinary competition",
            False,
            f"season {season.year} has {len(competitions)} ordinary competition streams; exactly one is supported",
        )
    if not competitions:
        return ActivationCheck(
            "competition_state", "Ordinary competition", False, "the ordinary competition has not been initialized yet"
        )
    competition = competitions[0]
    rounds = seasons.list_rounds(competition.competition_id)
    expected = [(n, f"round-{n}", f"Round {n}") for n in range(1, season.regular_season_round_count + 1)]
    actual = [(r.sequence, r.round_key, r.label) for r in rounds]
    if actual != expected:
        return ActivationCheck(
            "competition_state",
            "Ordinary competition",
            False,
            f"the ordinary competition does not have exactly Rounds 1-{season.regular_season_round_count} "
            f"({len(rounds)} round(s) found)",
        )
    return ActivationCheck(
        "competition_state",
        "Ordinary competition",
        True,
        f"{competition.label}: Rounds 1-{season.regular_season_round_count}",
    )


def _fixture_check(database, season_id: str) -> ActivationCheck:
    draw = FixtureRepository(database).get_draw(season_id)
    if draw is None:
        return ActivationCheck(
            "fixture_state", "Fixture-number draw", False, "the fixture-number draw has not been created yet"
        )
    if draw.state != "frozen":
        return ActivationCheck(
            "fixture_state", "Fixture-number draw", False, f"the fixture-number draw is {draw.state!r}, not frozen"
        )
    return ActivationCheck("fixture_state", "Fixture-number draw", True, "accepted and frozen")


def _squad_completion(database, season_id: str) -> tuple[int, int | None, int]:
    """`(entries_with_a_complete_squad, configured_squad_limit,
    total_entries)` -- read directly from ownership/squad-configuration,
    the same tables `app.draft.DraftRepository.finalize`'s own "resulting
    squads do not match the configured squad size" defence-in-depth check
    reads (never counting draft picks alone, which say nothing about
    ownership released out-of-band)."""
    config = database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
    ).fetchone()
    squad_limit = config["squad_limit"] if config else None
    entries = database.execute("SELECT season_entry_id FROM season_entry WHERE season_id=?", (season_id,)).fetchall()
    if squad_limit is None or not entries:
        return 0, squad_limit, len(entries)
    complete = database.execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT se.season_entry_id, COUNT(p.ownership_period_id) AS owned"
        "  FROM season_entry se"
        "  LEFT JOIN player_ownership_period p"
        "    ON p.season_entry_id = se.season_entry_id AND p.released_at IS NULL"
        "  WHERE se.season_id = ?"
        "  GROUP BY se.season_entry_id"
        "  HAVING owned = ?"
        ") complete_entries",
        (season_id, squad_limit),
    ).fetchone()["n"]
    return complete, squad_limit, len(entries)


def _player_pool_check(database, season_id: str) -> ActivationCheck:
    pool_total = PlayerPoolRepository(database).summary(season_id)["total"]
    if not pool_total:
        return ActivationCheck(
            "player_pool", "Player pool and squads", False, "the player pool has not been populated yet"
        )
    complete_count, squad_limit, entry_count = _squad_completion(database, season_id)
    if squad_limit is None:
        return ActivationCheck(
            "player_pool", "Player pool and squads", False, "the season squad limit has not been configured yet"
        )
    if entry_count == 0 or complete_count != entry_count:
        return ActivationCheck(
            "player_pool",
            "Player pool and squads",
            False,
            f"{complete_count} of {entry_count} team(s) have a complete {squad_limit}-player squad",
        )
    return ActivationCheck(
        "player_pool", "Player pool and squads", True, f"every team has a complete {squad_limit}-player squad"
    )


def _draft_check(database, season_id: str) -> ActivationCheck:
    status = DraftRepository(database).status(season_id)
    if status is None:
        return ActivationCheck(
            "preseason_draft", "Preseason draft", False, "the preseason draft order has not been accepted yet"
        )
    if not status.is_complete:
        return ActivationCheck(
            "preseason_draft",
            "Preseason draft",
            False,
            f"{status.completed_picks} of {status.total_picks} pick(s) completed",
        )
    if not status.is_finalized:
        return ActivationCheck(
            "preseason_draft",
            "Preseason draft",
            False,
            "every pick is complete, but the draft has not been finalized (opening-squad freeze) yet",
        )
    return ActivationCheck("preseason_draft", "Preseason draft", True, "finalized (opening-squad freeze complete)")


def _evaluate_checks(database, season: Season) -> list[ActivationCheck]:
    return [
        _entries_check(database, season.season_id),
        _player_pool_check(database, season.season_id),
        _ordinary_competition_check(database, season),
        _fixture_check(database, season.season_id),
        _draft_check(database, season.season_id),
    ]


def preview_activate_season(database, season_id: str) -> ActivationReadiness:
    """Read-only report of whether `activate_season` would currently
    succeed -- never locks a row, never mutates. Mirrors
    `app.season_completion.preview_complete_season`'s shape. Raises
    `app.season.SeasonNotFoundError` for an unknown `season_id`."""
    season = SeasonRepository(database).get_season(season_id)
    if season is None:
        raise SeasonNotFoundError(season_id)
    if season.lifecycle_state != "setup":
        diagnostic = (
            "this season is already active; no further activation action is needed"
            if season.lifecycle_state == "active"
            else f"this season is {season.lifecycle_state!r}; only a season in 'setup' can be activated"
        )
        return ActivationReadiness(season_id, season.lifecycle_state, False, [], diagnostic)
    checks = _evaluate_checks(database, season)
    blockers = [check for check in checks if not check.ready]
    ready = not blockers
    diagnostic = None if ready else "; ".join(check.detail for check in blockers)
    return ActivationReadiness(season_id, season.lifecycle_state, ready, checks, diagnostic)


def activate_season(database, season_id: str, *, actor: ActorContext, reason: str | None) -> ActivationResult:
    """The atomic `setup -> active` transition (issue #239). Locks the
    owning season row first (`SeasonRepository.guard_writable`, the same
    global lock order every other result-changing write in this
    application uses), refuses outright -- never a silent no-op -- if the
    season is not currently `setup`, re-verifies every readiness check
    under that lock, then transitions the lifecycle state via
    `SeasonRepository._transition_lifecycle_in_transaction` (which appends
    the existing `season.lifecycle.changed` audit event with the actor,
    reason and before/after lifecycle state) -- all in the caller's one
    transaction. Raises `SeasonActivationError` if `reason` is empty,
    `SeasonActivationStateError` if the season is already `active` or is
    `completed`, `SeasonNotReadyToActivateError` if any prerequisite is
    missing or contradictory, and `app.season.SeasonNotFoundError` for an
    unknown `season_id`. Nothing is written in any failure case."""
    if not reason or not reason.strip():
        raise SeasonActivationError("season activation requires an explicit reason")
    reason = reason.strip()
    seasons = SeasonRepository(database)
    with transaction(database) as conn:
        try:
            season = seasons.guard_writable(conn, season_id)
        except SeasonCompletedError as exc:
            raise SeasonActivationStateError(str(exc)) from exc
        if season.lifecycle_state == "active":
            raise SeasonActivationStateError(f"season {season_id} is already active; no further action is needed")
        if season.lifecycle_state != "setup":
            raise SeasonActivationStateError(
                f"season {season_id} cannot be activated from its current lifecycle state ({season.lifecycle_state!r})"
            )
        checks = _evaluate_checks(database, season)
        blockers = [check for check in checks if not check.ready]
        if blockers:
            raise SeasonNotReadyToActivateError("activation refused: " + "; ".join(check.detail for check in blockers))
        activated = seasons._transition_lifecycle_in_transaction(conn, season_id, "active", actor=actor, reason=reason)
    return ActivationResult(season=activated, previous_lifecycle_state=season.lifecycle_state)
