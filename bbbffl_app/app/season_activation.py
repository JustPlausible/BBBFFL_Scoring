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
"is this season ready" definition. Every check below reads the same
tables the owning domain module already writes:

- **season entries** (`season_entry`) -- exactly `TEAM_COUNT` (10)
  required, the same constant and threshold `app.season_setup`/
  `app.admin_dashboard` already use for a fresh BBBFFL season;
- **player pool / completed squads** (`season_player_pool`,
  `season_squad_configuration`, `player_ownership_period`) -- the
  season's player pool is populated and every season entry owns exactly
  the configured squad limit's worth of active players;
- **competition state** (`competition_stream`, `bbbffl_round`) -- the
  ordinary competition stream and its Rounds 1..
  `regular_season_round_count` exist, the same shape
  `SeasonRepository.initialize_ordinary_competition` creates;
- **fixture state** (`season_fixture_draw`) -- the fixture-number draw
  is accepted and frozen;
- **preseason draft state** (`season_draft`, `draft_pick`,
  `season_preseason_window`) -- the preseason draft is finalized *and*
  the preseason trade window is closed. A finalized draft alone is only
  the prerequisite for *opening* that window
  (`app.preseason.PreseasonRepository.open_window`) -- `close_window` is
  the operation that validates every squad and freezes the authoritative
  opening-squad snapshot (see `app/preseason.py`'s module docstring,
  "draft finalized -> window OPEN -> [preseason trades] -> window
  CLOSED (+ opening snapshot frozen)"). Activation requires the window
  actually closed, not merely a finalized draft, so a season cannot go
  live with preseason trades still possible (Codex review, PR #254, P1).

Every read function takes the same connection-like object `conn` the
caller is already using -- `database` itself for the read-only preview,
or the transaction's own `conn` for `activate_season` -- and an
`for_update` flag that appends `SeasonRepository`/`app.season_completion`'s
existing `_for_update_suffix` on PostgreSQL. `preview_activate_season`
never locks a row and never mutates; `activate_season` re-verifies every
check *through the season-row-locked transaction itself*, so a concurrent
write to a locked prerequisite (e.g. `DraftRepository.reopen`, a
preseason-window closure/correction, a squad-limit change) either commits
first and is observed, or blocks until this transaction completes --
never an unlocked read racing the commit (Codex review, PR #254, P2).
This mirrors `app.season_completion.preview_complete_season`/
`complete_season`'s own dual-mode `_required_round_ids`/
`_collect_round_states` pattern exactly.

Not every prerequisite row is locked, though: `season_entry` and
`season_player_pool` are deliberately read unlocked always (see
`_entries_check`'s docstring) -- `app.identity`/`app.player_pool`/
`app.preseason`/`app.shortlist` each lock `season_entry` in mutually
different orders with no documented global convention, and a real
PostgreSQL deadlock against an ordinary `OwnershipRepository.acquire`/
`release` was reproduced and fixed by *not* locking these two tables here
(Codex review, PR #254, a fourth P2 round) -- see "Atomicity" in
`docs/season-activation.md` for what this module's locking does and does
not claim.

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
from app.db import _for_update_suffix, transaction
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


def _suffix(database, *, for_update: bool) -> str:
    return _for_update_suffix(database) if for_update else ""


def _entries_check(conn, database, season_id: str, *, for_update: bool) -> ActivationCheck:
    """Deliberately never locked (regardless of `for_update`): `app.identity`/
    `app.player_pool`/`app.preseason`/`app.shortlist` each lock `season_entry`
    rows in their own, mutually different orders relative to other tables
    (no documented global order exists for this table today), and a season's
    ten entries are established once, before drafting begins -- effectively
    stable by the time a season can be activation-ready. A stress run
    confirmed a real PostgreSQL deadlock between `activate_season` and an
    ordinary `OwnershipRepository.acquire` when this row was locked here
    (Codex review, PR #254, a fourth P2 round); an unlocked read removes
    that contention entirely rather than chasing one more pairwise order."""
    rows = conn.execute("SELECT season_entry_id FROM season_entry WHERE season_id=?", (season_id,)).fetchall()
    count = len(rows)
    ready = count == TEAM_COUNT
    detail = (
        f"{count} of {TEAM_COUNT} season entries established"
        if ready
        else f"exactly {TEAM_COUNT} season entries are required (currently {count})"
    )
    return ActivationCheck("entries", "Season entries", ready, detail)


def _ordinary_competition_check(conn, database, season: Season, *, for_update: bool) -> ActivationCheck:
    """The same target shape `SeasonRepository.initialize_ordinary_competition`
    creates -- one `ordinary`-typed competition stream with exactly Rounds
    1..`regular_season_round_count` -- read directly from the season model's
    own tables rather than through `app.season_setup`'s page-model helper
    (this module and `app.season_setup` are kept siblings, not a dependency
    of one another; see `tests/test_architecture.py`)."""
    suffix = _suffix(database, for_update=for_update)
    streams = conn.execute(
        "SELECT competition_id, label, stream_type FROM competition_stream WHERE season_id=?" + suffix,
        (season.season_id,),
    ).fetchall()
    competitions = [row for row in streams if row["stream_type"] == "ordinary"]
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
    rounds = conn.execute(
        "SELECT sequence, round_key, label FROM bbbffl_round WHERE competition_id=? ORDER BY sequence" + suffix,
        (competition["competition_id"],),
    ).fetchall()
    expected = [(n, f"round-{n}", f"Round {n}") for n in range(1, season.regular_season_round_count + 1)]
    actual = [(row["sequence"], row["round_key"], row["label"]) for row in rounds]
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
        f"{competition['label']}: Rounds 1-{season.regular_season_round_count}",
    )


def _fixture_check(conn, database, season_id: str, *, for_update: bool) -> ActivationCheck:
    draw = conn.execute(
        "SELECT state FROM season_fixture_draw WHERE season_id=?" + _suffix(database, for_update=for_update),
        (season_id,),
    ).fetchone()
    if draw is None:
        return ActivationCheck(
            "fixture_state", "Fixture-number draw", False, "the fixture-number draw has not been created yet"
        )
    if draw["state"] != "frozen":
        return ActivationCheck(
            "fixture_state", "Fixture-number draw", False, f"the fixture-number draw is {draw['state']!r}, not frozen"
        )
    return ActivationCheck("fixture_state", "Fixture-number draw", True, "accepted and frozen")


def _player_pool_check(conn, database, season_id: str, *, for_update: bool) -> ActivationCheck:
    """Player pool populated, and every season entry's active ownership
    count matches the configured squad limit -- `season_entry` and
    `player_ownership_period` (which carries its own `season_id`, so no
    join is needed) are read as two plain, unjoined row sets rather than
    one outer join: PostgreSQL refuses `FOR UPDATE` on an aggregate/
    `GROUP BY` *and* on the nullable side of an outer join, so the locked
    path locks each table's own rows directly and the per-entry counts are
    computed in Python.

    The pool-population existence check is deliberately never locked
    (regardless of `for_update`): `OwnershipRepository.acquire_in_
    transaction`/`release_in_transaction` lock a `season_player_pool` row
    *before* they request `season_preseason_window` (via
    `_assert_ownership_mutation_allowed`), the opposite of the window-
    before-everything-else order this check's siblings use below --
    locking an arbitrary pool row here could deadlock against an ordinary
    ownership mutation on PostgreSQL (Codex review, PR #254, a fourth P2
    round). Whether the pool has ever been populated is effectively
    monotonic (no caller un-populates it), so an unlocked read is safe:
    the worst case is observing "not yet populated" for a population that
    committed a moment later, which correctly refuses activation rather
    than risking anything unsafe."""
    suffix = _suffix(database, for_update=for_update)
    pool_row = conn.execute(
        "SELECT canonical_player_id FROM season_player_pool WHERE season_id=? LIMIT 1", (season_id,)
    ).fetchone()
    if pool_row is None:
        return ActivationCheck(
            "player_pool", "Player pool and squads", False, "the player pool has not been populated yet"
        )
    config = conn.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?" + suffix, (season_id,)
    ).fetchone()
    if config is None:
        return ActivationCheck(
            "player_pool", "Player pool and squads", False, "the season squad limit has not been configured yet"
        )
    squad_limit = config["squad_limit"]
    # `season_entry` is deliberately never locked here either -- see
    # `_entries_check`'s docstring.
    entries = conn.execute("SELECT season_entry_id FROM season_entry WHERE season_id=?", (season_id,)).fetchall()
    counts: dict[str, int] = {row["season_entry_id"]: 0 for row in entries}
    ownership = conn.execute(
        "SELECT season_entry_id FROM player_ownership_period WHERE season_id=? AND released_at IS NULL" + suffix,
        (season_id,),
    ).fetchall()
    for row in ownership:
        if row["season_entry_id"] in counts:
            counts[row["season_entry_id"]] += 1
    entry_count = len(counts)
    complete_count = sum(1 for n in counts.values() if n == squad_limit)
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


def _draft_check(conn, database, season_id: str, *, for_update: bool) -> ActivationCheck:
    """The preseason draft is finalized *and* its preseason trade window
    is closed -- a finalized draft alone only permits *opening* that
    window; `close_window` is what validates squads and freezes the
    authoritative opening-squad snapshot (Codex review, PR #254, P1;
    see `app/preseason.py`'s module docstring).

    Reads (and, on the locked path, locks) `season_preseason_window`
    *before* `season_draft`/`draft_pick` -- the same order
    `PreseasonRepository.close_window`/`correct_opening_snapshot` already
    lock window-before-draft/window-before-ownership -- and this check
    runs before `_player_pool_check` in `_evaluate_checks` for the same
    reason: one consistent lock order (window, then draft, then
    ownership) across every caller avoids a PostgreSQL deadlock between
    `activate_season` and a concurrent `close_window`/
    `correct_opening_snapshot` (Codex review, PR #254, second P2)."""
    suffix = _suffix(database, for_update=for_update)
    window = conn.execute(
        "SELECT closed_at FROM season_preseason_window WHERE season_id=?" + suffix, (season_id,)
    ).fetchone()
    draft = conn.execute(
        "SELECT draft_id, finalized_at FROM season_draft WHERE season_id=? AND draft_kind='preseason'" + suffix,
        (season_id,),
    ).fetchone()
    if draft is None:
        return ActivationCheck(
            "preseason_draft", "Preseason draft", False, "the preseason draft order has not been accepted yet"
        )
    picks = conn.execute(
        "SELECT completed_at FROM draft_pick WHERE draft_id=? AND superseded_by_draft_pick_id IS NULL" + suffix,
        (draft["draft_id"],),
    ).fetchall()
    total = len(picks)
    completed = sum(1 for row in picks if row["completed_at"] is not None)
    if not total or completed != total:
        return ActivationCheck("preseason_draft", "Preseason draft", False, f"{completed} of {total} pick(s) completed")
    if draft["finalized_at"] is None:
        return ActivationCheck(
            "preseason_draft",
            "Preseason draft",
            False,
            "every pick is complete, but the draft has not been finalized (opening-squad freeze) yet",
        )
    if window is None:
        return ActivationCheck(
            "preseason_draft",
            "Preseason draft",
            False,
            "the draft is finalized, but the preseason trade window has not been opened yet",
        )
    if window["closed_at"] is None:
        return ActivationCheck(
            "preseason_draft",
            "Preseason draft",
            False,
            "the preseason trade window is still open; close it to freeze the opening squads before activating",
        )
    return ActivationCheck(
        "preseason_draft",
        "Preseason draft",
        True,
        "finalized, with the preseason trade window closed and opening squads frozen",
    )


def _evaluate_checks(conn, database, season: Season, *, for_update: bool) -> list[ActivationCheck]:
    # Lock `season_squad_configuration` before anything else:
    # `OwnershipRepository.configure_squad_limit` locks it before
    # `season_draft` (Codex review, PR #254, third P2 round), and
    # `_draft_check` below locks `season_preseason_window` before
    # `season_draft`/`draft_pick`. No existing caller orders squad
    # configuration against the window, so locking it first here satisfies
    # `configure_squad_limit`'s order without disturbing the window-before-
    # draft-before-ownership order established below. `_player_pool_check`
    # re-reads the same row later; re-locking a row this transaction
    # already holds is a no-op, never a second wait.
    conn.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?"
        + _suffix(database, for_update=for_update),
        (season.season_id,),
    )
    # `_draft_check` runs next: it locks `season_preseason_window` before
    # `season_draft`, and every other check that could contend with a
    # concurrent preseason operation (`_player_pool_check`'s ownership
    # rows) must lock *after* it, matching `PreseasonRepository.
    # close_window`/`correct_opening_snapshot`'s own window-first lock
    # order (see `_draft_check`'s docstring).
    return [
        _draft_check(conn, database, season.season_id, for_update=for_update),
        _entries_check(conn, database, season.season_id, for_update=for_update),
        _player_pool_check(conn, database, season.season_id, for_update=for_update),
        _ordinary_competition_check(conn, database, season, for_update=for_update),
        _fixture_check(conn, database, season.season_id, for_update=for_update),
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
    checks = _evaluate_checks(database, database, season, for_update=False)
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
    *through that same transaction* (each check's own prerequisite rows
    locked too, on PostgreSQL -- see the module docstring), then
    transitions the lifecycle state via `SeasonRepository.
    _transition_lifecycle_in_transaction` (which appends the existing
    `season.lifecycle.changed` audit event with the actor, reason and
    before/after lifecycle state) -- all in the caller's one transaction.
    Raises `SeasonActivationError` if `reason` is empty,
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
        checks = _evaluate_checks(conn, database, season, for_update=True)
        blockers = [check for check in checks if not check.ready]
        if blockers:
            raise SeasonNotReadyToActivateError("activation refused: " + "; ".join(check.detail for check in blockers))
        activated = seasons._transition_lifecycle_in_transaction(conn, season_id, "active", actor=actor, reason=reason)
    return ActivationResult(season=activated, previous_lifecycle_state=season.lifecycle_state)
