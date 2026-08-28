"""Explicit resolution and persistence of an exact carry-forward
submission from the previous relevant submitted lineup, for the same
BBBFFL season, competition stream and season entry (roadmap package 22,
issue #55).

Builds directly on `app.lineups.WeeklyLineupRepository.submit_positions` --
the same immutable submission/version boundary from #33, the same
`lock_guard`/compare-and-swap concurrency rule -- rather than a second
lineup model, a proxy-only selection store, or a parallel audit mechanism.
Nothing here computes lock state itself; a `lock_guard` (see
app/lockouts.py) is threaded straight through to `submit_positions`, so a
carried-forward assignment can never mutate a position app/lockouts.py has
already decided is locked or indeterminate (see `carry_forward` below).

## What "previous relevant" means

`resolve_source` finds the most recent BBBFFL round *before* the target
round, in the *same* `competition_id` (an ordinary lineup can therefore
never source a SuperScore submission, or vice versa -- see this module's
tests), for the *same* `season_entry_id`, that has a non-null
`effective_submission_version` -- i.e. an actual submitted version, whether
originally coach-submitted or itself already carried-forward, but never a
private draft (`weekly_lineup_draft_slot` is never consulted here, so an
unsubmitted newer draft for the *current* round is simply irrelevant, and
an unsubmitted draft in an earlier round is skipped as a source in favour
of the nearest round that actually has a submitted version).

A round with no such predecessor -- Round 1, or the first round of a new
competition stream such as a future SuperScore SS1 -- has no source at
all. `resolve_source` returns `None` and `carry_forward` refuses to invent
a default/optimised team: it raises `NoCarryForwardSourceError`, an
explicit state that requires scorer/admin confirmation or proxy entry
(see app/lineup_proxy.py) rather than silent invention.

## Exactness

The source submission's positions are copied verbatim into the new
submission: the same player in the same position, never optimised,
substituted, reordered or "repaired". Any resulting ownership,
availability or DNP question is left entirely to the normal downstream
validation/scoring workflow -- `submit_positions` still runs the same
`_validate_players`/`_validate_ownership` checks a coach's own submission
does, so a source player no longer owned by this entry fails the attempt
explicitly rather than being silently substituted or dropped.
"""

from dataclasses import dataclass

from app.lineups import LineupIntegrityError, WeeklyLineupRepository

CARRY_FORWARD_SOURCE_TYPE = "carry_forward"


class CarryForwardError(LineupIntegrityError):
    """Base class for this module's domain errors."""


class NoCarryForwardSourceError(CarryForwardError):
    """No earlier submitted lineup exists in this competition stream for
    this entry -- Round 1, or a stream's first round (e.g. a future SS1).
    Requires explicit scorer/admin confirmation or proxy entry; never an
    invented default/optimised team (see this module's docstring)."""


@dataclass(frozen=True)
class CarryForwardSource:
    """The previous relevant submitted lineup a carry-forward would copy
    (or did copy) from -- also the shape persisted into a carry-forward
    submission's `source_detail` (see `carry_forward`/
    `read_carry_forward_provenance`)."""

    source_bbbffl_round_id: str
    source_lineup_id: str
    source_version: int
    positions: dict


class CarryForwardService:
    def __init__(self, database):
        self.database = database
        self._lineups = WeeklyLineupRepository(database)

    def resolve_source(
        self, season_id: str, competition_id: str, bbbffl_round_id: str, season_entry_id: str
    ) -> CarryForwardSource | None:
        """Stream- and entry-scoped lookup of the previous relevant
        submitted lineup for `bbbffl_round_id`. Returns `None` when there is
        none (see this module's docstring)."""
        target = self.database.execute(
            "SELECT sequence FROM bbbffl_round WHERE bbbffl_round_id=? AND competition_id=?",
            (bbbffl_round_id, competition_id),
        ).fetchone()
        if target is None:
            raise LineupIntegrityError("unknown BBBFFL round for this competition stream")
        # competition_id is joined on both weekly_lineup and bbbffl_round so
        # a stream mismatch (impossible under normal writes, since both
        # columns are always set from the same submission scope) can never
        # silently widen the search -- this is the stream-isolation
        # boundary: an ordinary lineup can only ever source another
        # ordinary lineup, and likewise for SuperScore.
        row = self.database.execute(
            "SELECT wl.lineup_id, wl.effective_submission_version, r.bbbffl_round_id AS source_round_id "
            "FROM weekly_lineup wl "
            "JOIN bbbffl_round r ON r.bbbffl_round_id = wl.bbbffl_round_id AND r.competition_id = wl.competition_id "
            "WHERE wl.season_id=? AND wl.competition_id=? AND wl.season_entry_id=? "
            "AND r.sequence < ? AND wl.effective_submission_version IS NOT NULL "
            "ORDER BY r.sequence DESC LIMIT 1",
            (season_id, competition_id, season_entry_id, target["sequence"]),
        ).fetchone()
        if row is None:
            return None
        submission = self._lineups.get_submission(row["lineup_id"], row["effective_submission_version"])
        return CarryForwardSource(
            row["source_round_id"], row["lineup_id"], submission.version, dict(submission.positions)
        )

    def carry_forward(
        self,
        season_id: str,
        competition_id: str,
        bbbffl_round_id: str,
        season_entry_id: str,
        *,
        expected_submission_version: int,
        actor,
        reason: str | None = None,
        lock_guard=None,
    ):
        """Resolve and persist an exact carry-forward submission for
        `bbbffl_round_id`/`season_entry_id`, if (and only if) a previous
        relevant submitted lineup exists. Raises `NoCarryForwardSourceError`
        otherwise -- see this module's docstring.

        `expected_submission_version` is the caller's last-known effective
        submission version for the *target* round/entry (0 if it has never
        been submitted) -- the same optimistic-concurrency contract as
        `WeeklyLineupRepository.submit`/`submit_positions`. A racing
        submission (coach, another carry-forward, or a proxy submission)
        that lands first makes this one fail with `LineupConflictError`
        rather than silently overwrite newer authoritative state.

        `resolve_source` above is a plain, unlocked read, so the source
        round's own submission could in principle be resubmitted between
        that read and this method's commit. `submit_positions` closes that
        window: it re-locks and re-checks the source lineup's effective
        version *inside* the same transaction as the target write (via
        `require_unchanged`), so a source resubmitted mid-flight makes this
        call fail with `LineupConflictError` rather than silently carrying
        forward a now-stale snapshot -- never a second, independent
        read-then-write race.
        """
        source = self.resolve_source(season_id, competition_id, bbbffl_round_id, season_entry_id)
        if source is None:
            raise NoCarryForwardSourceError(
                f"no previous submitted lineup exists for entry {season_entry_id} in competition "
                f"{competition_id} before round {bbbffl_round_id}; explicit scorer/admin action is required"
            )
        lineup_id, _ = self._lineups.get_or_create_header(season_id, competition_id, bbbffl_round_id, season_entry_id)
        source_detail = {
            "source_bbbffl_round_id": source.source_bbbffl_round_id,
            "source_lineup_id": source.source_lineup_id,
            "source_version": source.source_version,
        }
        submitted = self._lineups.submit_positions(
            lineup_id,
            source.positions,
            expected_submission_version=expected_submission_version,
            actor=actor,
            source_type=CARRY_FORWARD_SOURCE_TYPE,
            source_detail=source_detail,
            reason=reason,
            lock_guard=lock_guard,
            require_unchanged=(source.source_lineup_id, source.source_version),
        )
        return submitted, source


def read_carry_forward_provenance(submission) -> CarryForwardSource | None:
    """Read-model helper: decode a `SubmittedLineup`'s persisted
    `source_detail` back into its `CarryForwardSource`, or `None` if
    `submission` is not itself a carry-forward submission -- so a caller
    (an admin diagnostic surface, a test) never needs to know
    `source_detail`'s JSON shape directly."""
    if submission is None or submission.source_type != CARRY_FORWARD_SOURCE_TYPE:
        return None
    detail = submission.source_detail or {}
    return CarryForwardSource(
        detail["source_bbbffl_round_id"],
        detail["source_lineup_id"],
        detail["source_version"],
        dict(submission.positions),
    )
