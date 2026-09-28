"""Issue #241: audited manual resolution of an exact mathematical ladder tie
the configured ladder criteria (competition points, percentage, points for)
cannot separate. Covers the domain module (`app.ladder_tie_ruling`) directly
-- recording, idempotency/supersede, staleness, the Scorer Operations
preview report, and the "never a hidden fallback ordering" guarantee.
Consumption by Finals seeding and season awards is covered in
tests/test_finals_seeding.py, tests/test_finals.py and
tests/test_season_completion.py, next to their own existing tie coverage.
HTTP-level authorization/CSRF is covered in tests/test_ladder_tie_ruling_api.py."""

import pytest

from app.audit import ActorContext
from app.ladder import LadderRepository
from app.ladder_tie_ruling import (
    LadderTieRulingConflictError,
    LadderTieRulingError,
    LadderTieRulingRepository,
    UnresolvedTieError,
    preview,
    resolve_full_ladder_order,
    resolve_ordinary_context,
    resolve_tie,
)
from app.season import SeasonRepository
from tests.finals_helpers import correct_official_result
from tests.finals_seeding_helpers import build_2026_replay_season
from tests.ladder_tie_ruling_helpers import build_tied_season

ACTOR = ActorContext.anonymous_operator("test")


def _activate(database, season_id):
    SeasonRepository(database).transition_lifecycle(season_id, "active", actor=ACTOR, reason="activate for test")


def _repo(built):
    return LadderTieRulingRepository(built["database"])


# -- Non-tied ladders are unaffected -----------------------------------------


def test_untied_ladder_needs_no_ruling_and_resolve_returns_the_natural_order():
    built = build_2026_replay_season(year=6100)
    database = built["database"]
    ladder = LadderRepository(database).snapshot(built["competition"].competition_id, 20)
    assert not any(row.tied for row in ladder.rows)

    order = resolve_full_ladder_order(database, ladder)
    assert order == tuple(row.season_entry_id for row in ladder.rows)
    assert _repo(built).list_active_for_season(built["season"].season_id) == []

    report = preview(database, built["season"].season_id)
    assert report["open_ties"] == []


# -- Recording a ruling -------------------------------------------------------


def test_record_ruling_requires_a_reason():
    built = build_tied_season(year=6101, tied_ranks=(9, 10))
    _activate(built["database"], built["season"].season_id)
    with pytest.raises(LadderTieRulingError, match="reason"):
        _repo(built).record_ruling(
            built["season"].season_id,
            built["ordinary_competition_id"],
            20,
            built["tied_pair"],
            actor=ACTOR,
            reason="   ",
        )


def test_record_ruling_requires_at_least_two_entries():
    built = build_tied_season(year=6102, tied_ranks=(9, 10))
    _activate(built["database"], built["season"].season_id)
    with pytest.raises(LadderTieRulingError, match="at least two"):
        _repo(built).record_ruling(
            built["season"].season_id,
            built["ordinary_competition_id"],
            20,
            built["tied_pair"][:1],
            actor=ACTOR,
            reason="only one entry",
        )


def test_record_ruling_rejects_a_duplicate_entry():
    built = build_tied_season(year=6103, tied_ranks=(9, 10))
    _activate(built["database"], built["season"].season_id)
    entry = built["tied_pair"][0]
    with pytest.raises(LadderTieRulingError, match="repeat"):
        _repo(built).record_ruling(
            built["season"].season_id,
            built["ordinary_competition_id"],
            20,
            [entry, entry],
            actor=ACTOR,
            reason="duplicate",
        )


def test_record_ruling_refuses_a_group_that_is_not_currently_tied():
    """Never a general-purpose ladder editor: entries that are not, right
    now, exactly tied on the recomputed ladder cannot get a ruling recorded
    for them -- there is no caller-supplied tie_group to spoof."""
    built = build_2026_replay_season(year=6104)  # no tie anywhere
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    entries = [e.season_entry_id for e in built["entries"][:2]]
    with pytest.raises(LadderTieRulingConflictError, match="no exact tie"):
        _repo(built).record_ruling(
            season_id, built["competition"].competition_id, 20, entries, actor=ACTOR, reason="not actually tied"
        )


def test_record_ruling_succeeds_for_a_genuine_last_place_tie():
    built = build_tied_season(year=6105, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    decided_order = built["tied_pair"]

    ruling, created = _repo(built).record_ruling(
        season_id, built["ordinary_competition_id"], 20, decided_order, actor=ACTOR, reason="coin toss, minuted"
    )
    assert created is True
    assert ruling.status == "active"
    assert list(ruling.decided_order) == decided_order
    assert list(ruling.tie_group) == sorted(decided_order)
    assert ruling.reason == "coin toss, minuted"
    assert ruling.created_by == ACTOR.actor_id

    active = _repo(built).get_active(season_id, built["ordinary_competition_id"], 20, decided_order)
    assert active.ruling_id == ruling.ruling_id


def test_record_ruling_accepts_the_tie_group_in_either_order():
    built = build_tied_season(year=6106, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    reversed_order = list(reversed(built["tied_pair"]))

    ruling, created = _repo(built).record_ruling(
        season_id, built["ordinary_competition_id"], 20, reversed_order, actor=ACTOR, reason="reversed input order"
    )
    assert created is True
    assert list(ruling.decided_order) == reversed_order


def test_recording_the_identical_ruling_again_is_a_no_op():
    built = build_tied_season(year=6107, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    decided_order = built["tied_pair"]
    repo = _repo(built)

    first, created_first = repo.record_ruling(
        season_id, built["ordinary_competition_id"], 20, decided_order, actor=ACTOR, reason="first recording"
    )
    assert created_first is True
    second, created_second = repo.record_ruling(
        season_id, built["ordinary_competition_id"], 20, decided_order, actor=ACTOR, reason="re-recorded, unchanged"
    )
    assert created_second is False
    assert second.ruling_id == first.ruling_id
    assert len(repo.history(season_id, built["ordinary_competition_id"], 20, decided_order)) == 1


def test_recording_a_different_order_supersedes_the_previous_ruling():
    built = build_tied_season(year=6108, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    decided_order = built["tied_pair"]
    reversed_order = list(reversed(decided_order))
    repo = _repo(built)

    first, _ = repo.record_ruling(
        season_id, built["ordinary_competition_id"], 20, decided_order, actor=ACTOR, reason="initial ruling"
    )
    second, created = repo.record_ruling(
        season_id, built["ordinary_competition_id"], 20, reversed_order, actor=ACTOR, reason="reversed on review"
    )
    assert created is True
    assert second.ruling_id != first.ruling_id

    history = repo.history(season_id, built["ordinary_competition_id"], 20, decided_order)
    assert {r.ruling_id for r in history} == {first.ruling_id, second.ruling_id}
    old = next(r for r in history if r.ruling_id == first.ruling_id)
    assert old.status == "superseded"
    assert old.superseded_by_ruling_id == second.ruling_id
    assert repo.get_active(season_id, built["ordinary_competition_id"], 20, decided_order).ruling_id == second.ruling_id


def test_record_ruling_requires_the_season_to_be_writable():
    """Issue #241 requirement: recording a ruling goes through the same
    completed-season write fence as every other result-changing write."""
    from app.season import SeasonCompletedError

    built = build_tied_season(year=6109, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id
    with database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season_id,))
    with pytest.raises(SeasonCompletedError):
        _repo(built).record_ruling(
            season_id, built["ordinary_competition_id"], 20, built["tied_pair"], actor=ACTOR, reason="too late"
        )


# -- Fail-closed resolution, and no hidden fallback --------------------------


def test_unresolved_tie_fails_closed_with_no_ruling_recorded():
    built = build_tied_season(year=6110, tied_ranks=(9, 10))
    database = built["database"]
    ladder = LadderRepository(database).snapshot(built["ordinary_competition_id"], 20)
    tie_group = next(row.tie_group for row in ladder.rows if row.tied)

    with pytest.raises(UnresolvedTieError) as excinfo:
        resolve_tie(database, ladder, tie_group)
    assert excinfo.value.stale is False
    assert set(excinfo.value.tie_group) == set(built["tied_pair"])

    with pytest.raises(UnresolvedTieError):
        resolve_full_ladder_order(database, ladder)


def test_resolution_never_falls_back_to_team_name_id_or_database_order():
    """The tied entries' own `season_entry_id` values and creation order
    are exactly what `app.ladder`'s module docstring says must never be
    treated as a real decision. Recording a ruling whose decided order is
    the *reverse* of every incidental ordering (alphabetical entry id,
    creation/database order) and confirming that exact reverse order comes
    back out proves no silent fallback is in play -- a fallback to any of
    those incidental fields would instead return the forward order."""
    built = build_tied_season(year=6111, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    incidental_order = sorted(built["tied_pair"])  # id/db-order/tie_group's own serialization order
    decided_order = list(reversed(incidental_order))
    assert decided_order != incidental_order

    _repo(built).record_ruling(
        season_id, built["ordinary_competition_id"], 20, decided_order, actor=ACTOR, reason="deliberately reversed"
    )
    ladder = LadderRepository(database).snapshot(built["ordinary_competition_id"], 20)
    tie_group = next(row.tie_group for row in ladder.rows if row.tied)
    assert resolve_tie(database, ladder, tie_group).decided_order == tuple(decided_order)
    assert resolve_tie(database, ladder, tie_group).decided_order != tuple(incidental_order)


# -- Staleness ----------------------------------------------------------------


def test_ruling_becomes_stale_after_the_underlying_results_change_and_history_is_retained():
    built = build_tied_season(year=6112, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    decided_order = built["tied_pair"]
    repo = _repo(built)

    original, _ = repo.record_ruling(
        season_id, built["ordinary_competition_id"], 20, decided_order, actor=ACTOR, reason="original ruling"
    )

    # Correct one of the pair's own matches, breaking the exact tie the
    # ruling was recorded against -- issue #241's "underlying results
    # change" case.
    match = database.execute(
        "SELECT matchup_id FROM bbbffl_matchup WHERE home_season_entry_id=? OR away_season_entry_id=? LIMIT 1",
        (decided_order[0], decided_order[0]),
    ).fetchone()
    correct_official_result(database, match["matchup_id"], 900, 1, reason="material correction after ruling")

    ladder = LadderRepository(database).snapshot(built["ordinary_competition_id"], 20)
    with pytest.raises(UnresolvedTieError) as excinfo:
        resolve_tie(database, ladder, tuple(sorted(decided_order)))
    assert excinfo.value.stale is True
    assert excinfo.value.ruling.ruling_id == original.ruling_id

    # The stale ruling is never deleted -- it stays active (nothing new has
    # superseded it) and fully queryable history.
    still_active = repo.get_active(season_id, built["ordinary_competition_id"], 20, decided_order)
    assert still_active is not None
    assert still_active.ruling_id == original.ruling_id

    report = preview(database, season_id)
    if report["open_ties"]:
        # If the correction happened to leave the pair still exactly tied
        # (just on different underlying facts), the preview must report it
        # as stale, never silently resolved.
        assert all(tie.status != "resolved" for tie in report["open_ties"])


def test_a_fresh_ruling_supersedes_a_stale_one_and_preserves_history():
    built = build_tied_season(year=6113, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    decided_order = built["tied_pair"]
    repo = _repo(built)

    original, _ = repo.record_ruling(
        season_id, built["ordinary_competition_id"], 20, decided_order, actor=ACTOR, reason="original ruling"
    )
    match = database.execute(
        "SELECT matchup_id FROM bbbffl_matchup WHERE home_season_entry_id IN (?,?) AND away_season_entry_id IN (?,?)",
        (decided_order[0], decided_order[1], decided_order[0], decided_order[1]),
    ).fetchone()
    # Correct the pair's own head-to-head match but keep it a draw, so the
    # exact tie group is unchanged while `result_references` still differs.
    correct_official_result(database, match["matchup_id"], 41, 41, reason="re-correct head-to-head, still a draw")

    fresh, created = repo.record_ruling(
        season_id, built["ordinary_competition_id"], 20, decided_order, actor=ACTOR, reason="re-recorded after change"
    )
    assert created is True
    assert fresh.ruling_id != original.ruling_id

    history = repo.history(season_id, built["ordinary_competition_id"], 20, decided_order)
    assert {r.ruling_id for r in history} == {original.ruling_id, fresh.ruling_id}
    assert next(r for r in history if r.ruling_id == original.ruling_id).status == "superseded"

    ladder = LadderRepository(database).snapshot(built["ordinary_competition_id"], 20)
    tie_group = next(row.tie_group for row in ladder.rows if row.tied)
    assert resolve_tie(database, ladder, tie_group).decided_order == tuple(decided_order)


# -- Scorer Operations preview report ----------------------------------------


def test_preview_reports_last_place_ties_as_affecting_the_wooden_spoon_and_finals():
    built = build_tied_season(year=6114, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id

    report = preview(database, season_id)
    assert len(report["open_ties"]) == 1
    tie = report["open_ties"][0]
    assert tie.status == "unresolved"
    assert tie.affects_finals_seeding is True
    assert tie.affects_wooden_spoon is True
    assert set(tie.tie_group) == set(built["tied_pair"])


def test_preview_reports_a_mid_ladder_tie_as_affecting_finals_only():
    built = build_tied_season(year=6115, tied_ranks=(3, 4))
    database, season_id = built["database"], built["season"].season_id

    report = preview(database, season_id)
    assert len(report["open_ties"]) == 1
    tie = report["open_ties"][0]
    assert tie.affects_finals_seeding is True
    assert tie.affects_wooden_spoon is False


def test_preview_reports_finals_seeding_unaffected_once_a_bracket_already_exists():
    """Codex review (PR #257, P2): once Finals seeding has already been
    frozen (a bracket exists, or a 2026-style snapshot does), the live
    ladder no longer feeds it at all -- a mid-ladder tie away from last
    place then blocks nothing, and must not be reported as a mandatory
    decision the Scorer still owes an operation that no longer consumes it.
    Recording the ruling first (required for `create_bracket` itself to
    succeed past this tie) and then re-checking `preview` proves this: the
    ladder still reports the identical tie (nothing here ever rewrites
    results), but it no longer *blocks* anything."""
    from app.finals import FinalsBracketRepository
    from tests.ladder_tie_ruling_helpers import build_tied_finals_ready_season

    built = build_tied_finals_ready_season(year=6118, tied_ranks=(3, 4))
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    _repo(built).record_ruling(
        season_id,
        built["ordinary_competition_id"],
        20,
        built["tied_pair"],
        actor=ACTOR,
        reason="resolve before bracket",
    )
    FinalsBracketRepository(database).create_bracket(
        season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="freeze finals seeding",
    )

    report = preview(database, season_id)
    assert len(report["open_ties"]) == 1
    tie = report["open_ties"][0]
    assert tie.affects_finals_seeding is False
    assert tie.affects_wooden_spoon is False


def test_preview_reports_an_unresolved_mid_ladder_tie_as_blocking_nothing_once_bracket_exists():
    """The realistic shape of the P2 finding above: a tie introduced by a
    correction *after* the bracket already exists never needed a ruling to
    unblock anything, and `preview` must say so (`status='unresolved'`,
    both `affects_*` flags `False`) rather than presenting it as a pending
    decision."""
    from app.finals import FinalsBracketRepository
    from tests.finals_helpers import build_finals_ready_season
    from tests.ladder_tie_ruling_helpers import force_tie

    built = build_finals_ready_season(year=6119)
    database, season_id = built["database"], built["season"].season_id
    FinalsBracketRepository(database).create_bracket(
        season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="freeze finals seeding while the ladder is still untied",
    )
    force_tie(database, built["entries"], built["ordinary_competition_id"], tied_ranks=(3, 4))

    report = preview(database, season_id)
    assert len(report["open_ties"]) == 1
    tie = report["open_ties"][0]
    assert tie.status == "unresolved"
    assert tie.affects_finals_seeding is False
    assert tie.affects_wooden_spoon is False


def test_preview_reports_resolved_status_once_a_fresh_ruling_exists():
    built = build_tied_season(year=6116, tied_ranks=(9, 10))
    database, season_id = built["database"], built["season"].season_id
    _activate(database, season_id)
    _repo(built).record_ruling(
        season_id, built["ordinary_competition_id"], 20, built["tied_pair"], actor=ACTOR, reason="resolved"
    )

    report = preview(database, season_id)
    assert len(report["open_ties"]) == 1
    assert report["open_ties"][0].status == "resolved"
    assert report["open_ties"][0].ruling.decided_order == tuple(built["tied_pair"])


def test_resolve_ordinary_context_refuses_an_unknown_season():
    built = build_tied_season(year=6117, tied_ranks=(9, 10))
    with pytest.raises(KeyError):
        resolve_ordinary_context(built["database"], "no-such-season")
