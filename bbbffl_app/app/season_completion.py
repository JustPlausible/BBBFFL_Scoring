"""Issue #195: the atomic `active -> completed` season-completion command.

Read `docs/2026-finals-superscore-design.md`'s "Grand Final/season winner
recording and end-of-season completion" and "Checkpoint timing" sections
(under "Audit, correction and recovery") before changing anything here --
this module implements exactly the six-step sequence they specify, as ONE
transaction:

1. Lock the owning season row (`SeasonRepository.guard_writable`).
2. Fail closed unless every required finals week is `final` **and** all
   four named SuperScore rounds (SS1-SS4) exist and are `final`. Checking
   only the terminal labels (Grand Final, SS4), or merely that their
   results were published, is explicitly insufficient evidence that the
   preceding rounds completed.
3. Lock/read the effective Grand Final result and the live Round 20
   mathematical ladder, then idempotently create or supersede the
   premiership and wooden-spoon `season_award` records
   (`app.season_awards`) so both reference those effective versions.
4. Record the `season.completed` completion audit event.
5. Transition the season `active -> completed`
   (`SeasonRepository._transition_lifecycle_in_transaction`, which itself
   appends the existing `season.lifecycle.changed` event).
6. Commit.

This issue's acceptance ends here: a committed, externally consumable
completed-season version and completion-event identifier
(`CompletionResult.completed_season_version`/`completion_event_id`) for
issue #194 to consume when it later creates the final archival/checkpoint
evidence. Creating that evidence is explicitly out of scope for this
module -- see the module-level docstring boundary in
`docs/2026-finals-superscore-design.md`'s "the essential property" passage.

No administrative bypass and no reopen pathway exist here, or anywhere in
this issue's scope: once `completed`, `SeasonRepository.guard_writable`
refuses every further result-changing write, `complete_season` itself
included (calling it again against an already-`completed` season raises
`SeasonCompletedError`, not a second no-op completion).
"""

from dataclasses import dataclass

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction
from app.season import Season, SeasonRepository
from app.season_awards import SeasonAward, reconcile_premiership_in_transaction, reconcile_wooden_spoon_in_transaction

SEASON_COMPLETED = "season.completed"
ENTITY_TYPE_SEASON = "season"

REQUIRED_FINALS_WEEKS = (1, 2, 3, 4)
REQUIRED_SUPERSCORE_ROUND_KEYS = ("ss1", "ss2", "ss3", "ss4")


class SeasonCompletionError(RuntimeError):
    """Base class for this module's domain errors."""


class SeasonNotReadyError(SeasonCompletionError):
    """Season completion's fail-closed readiness gate (step 2) refused:
    some required finals week, or some SuperScore round (SS1-SS4), does
    not yet exist or is not yet `final`. Grand Final + SS4 alone is never
    sufficient evidence -- every required round is checked individually."""


@dataclass(frozen=True)
class CompletionResult:
    season: Season
    completed_season_version: int
    completion_event_id: str
    premiership_award: SeasonAward
    wooden_spoon_award: SeasonAward
    premiership_created: bool
    wooden_spoon_created: bool


def _required_round_ids(conn_or_database, database, season_id: str, *, for_update: bool) -> dict:
    """Resolve the `bbbffl_round_id` of every required finals week
    (1-4) and SuperScore round (SS1-SS4) for this season. Raises
    `SeasonNotReadyError` if the finals bracket, its full four weeks, the
    SuperScore stream, or any of SS1-SS4 do not exist yet -- a season
    cannot be completed before all of these are at least *created*, let
    alone `final`."""
    suffix = _for_update_suffix(database) if for_update else ""
    conn = conn_or_database
    bracket = conn.execute("SELECT bracket_id FROM finals_bracket WHERE season_id=?" + suffix, (season_id,)).fetchone()
    if bracket is None:
        raise SeasonNotReadyError(f"season {season_id} has no finals bracket yet; cannot complete season")
    weeks = conn.execute(
        "SELECT week_number, bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? ORDER BY week_number",
        (bracket["bracket_id"],),
    ).fetchall()
    finals_round_ids = {row["week_number"]: row["bbbffl_round_id"] for row in weeks}
    missing_weeks = sorted(set(REQUIRED_FINALS_WEEKS) - set(finals_round_ids))
    if missing_weeks:
        raise SeasonNotReadyError(f"season {season_id} finals bracket is missing week(s) {missing_weeks}")

    stream = conn.execute(
        "SELECT competition_id FROM superscore_stream WHERE season_id=?" + suffix, (season_id,)
    ).fetchone()
    if stream is None:
        raise SeasonNotReadyError(f"season {season_id} has no SuperScore stream yet; cannot complete season")
    ss_rows = conn.execute(
        "SELECT round_key, bbbffl_round_id FROM bbbffl_round WHERE competition_id=? AND round_key IN (?,?,?,?)",
        (stream["competition_id"], *REQUIRED_SUPERSCORE_ROUND_KEYS),
    ).fetchall()
    ss_round_ids = {row["round_key"]: row["bbbffl_round_id"] for row in ss_rows}
    missing_ss = sorted(set(REQUIRED_SUPERSCORE_ROUND_KEYS) - set(ss_round_ids))
    if missing_ss:
        raise SeasonNotReadyError(f"season {season_id} is missing SuperScore round(s) {missing_ss}")
    return {"finals": finals_round_ids, "superscore": ss_round_ids}


def _collect_round_states(conn_or_database, database, required: dict, *, for_update: bool) -> dict:
    """Read every required round's current lifecycle state (locked
    individually when `for_update`). Returns `{bbbffl_round_id:
    state_or_None}` for every required round -- unconditionally, so a
    caller (`preview_complete_season`'s diagnostic report included) always
    has the full picture regardless of whether it goes on to raise."""
    suffix = _for_update_suffix(database) if for_update else ""
    conn = conn_or_database
    all_round_ids = sorted({*required["finals"].values(), *required["superscore"].values()})
    states: dict[str, str | None] = {}
    for round_id in all_round_ids:
        row = conn.execute(
            "SELECT state FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?" + suffix, (round_id,)
        ).fetchone()
        states[round_id] = row["state"] if row is not None else None
    return states


def _require_all_final(states: dict) -> None:
    """Step 2's fail-closed finality check: raises `SeasonNotReadyError`
    naming every round in `states` that is not `final` -- never inferred
    from the Grand Final/SS4 alone."""
    not_final = sorted(round_id for round_id, state in states.items() if state != "final")
    if not_final:
        raise SeasonNotReadyError(
            f"season completion refused: round(s) {not_final} are not yet final -- every required finals week "
            "and all four SuperScore rounds (SS1-SS4) must be final, not merely published or partially complete"
        )


def preview_complete_season(database, season_id: str) -> dict:
    """Read-only report of whether `complete_season` would currently
    succeed -- never locks a row, never mutates. Mirrors `app.finals.
    FinalsBracketRepository.preview_create_bracket`'s shape."""
    report: dict = {"season_id": season_id, "ready": False, "diagnostic": None, "round_states": {}}
    season = SeasonRepository(database).get_season(season_id)
    if season is None:
        report["diagnostic"] = f"unknown season {season_id}"
        return report
    if season.lifecycle_state != "active":
        report["diagnostic"] = f"season must be active to complete (currently {season.lifecycle_state!r})"
        return report
    try:
        required = _required_round_ids(database, database, season_id, for_update=False)
    except SeasonNotReadyError as exc:
        report["diagnostic"] = str(exc)
        return report
    report["round_states"] = _collect_round_states(database, database, required, for_update=False)
    try:
        _require_all_final(report["round_states"])
    except SeasonNotReadyError as exc:
        report["diagnostic"] = str(exc)
        return report
    report["ready"] = True
    return report


def complete_season(database, season_id: str, *, actor: ActorContext, reason: str) -> CompletionResult:
    """The six-step atomic `active -> completed` transition. See module
    docstring. Raises `SeasonCompletedError` (from `guard_writable`) if the
    season is already `completed` -- this command is not itself
    idempotently re-callable once it has succeeded, matching "no implicit
    administrative bypass, no reopen pathway" (issue #195's explicit scope
    boundary). Raises `SeasonNotReadyError` (step 2) if any required round
    is not yet final, and whatever `app.season_awards` raises (step 3) if
    either award cannot be derived -- in every failure case, the whole
    transaction rolls back and nothing is written, including no partial
    award and no lifecycle transition."""
    if not reason or not reason.strip():
        raise SeasonCompletionError("season completion requires an explicit, substantive reason")
    seasons = SeasonRepository(database)
    with transaction(database) as conn:
        # Step 1: lock the owning season row; fails closed if already completed.
        season = seasons.guard_writable(conn, season_id)
        if season.lifecycle_state != "active":
            raise SeasonCompletionError(
                f"season {season_id} must be active to complete (currently {season.lifecycle_state!r})"
            )

        # Step 2: fail-closed readiness -- every finals week and SS1-SS4 final.
        required = _required_round_ids(conn, database, season_id, for_update=True)
        _require_all_final(_collect_round_states(conn, database, required, for_update=True))

        # Step 3: lock/derive the effective Grand Final result and the live
        # Round 20 ladder, then idempotently create-or-supersede both awards.
        premiership, premiership_created = reconcile_premiership_in_transaction(
            conn, database, season_id, actor=actor, reason=reason
        )
        wooden_spoon, wooden_spoon_created = reconcile_wooden_spoon_in_transaction(
            conn, database, season_id, actor=actor, reason=reason
        )

        # Step 4: record the completion audit event.
        completion_event = append_event(
            conn,
            actor=actor,
            action=SEASON_COMPLETED,
            entity_type=ENTITY_TYPE_SEASON,
            entity_id=season_id,
            reason=reason,
            after_state={"lifecycle_state": "completed"},
            payload={
                "premiership_award_id": premiership.award_id,
                "wooden_spoon_award_id": wooden_spoon.award_id,
                "finals_round_ids": required["finals"],
                "superscore_round_ids": required["superscore"],
            },
        )

        # Step 5: transition active -> completed (appends season.lifecycle.changed too).
        completed_season = seasons._transition_lifecycle_in_transaction(
            conn, season_id, "completed", actor=actor, reason=reason
        )
        # Step 6: commit -- implicit on `with transaction(...)` exit.

    return CompletionResult(
        season=completed_season,
        completed_season_version=completed_season.version,
        completion_event_id=completion_event.event_id,
        premiership_award=premiership,
        wooden_spoon_award=wooden_spoon,
        premiership_created=premiership_created,
        wooden_spoon_created=wooden_spoon_created,
    )
