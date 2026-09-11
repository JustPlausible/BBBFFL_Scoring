"""Issue #187: the replay-only 2026 finals-seeding snapshot.

Covers: preview without mutation, the mathematical ladder staying exactly
unchanged, the exact historical seed order, successful apply, actor/time/
reason and before/after audit provenance, repeated identical application,
conflicting/invalid second application failing closed, incomplete/invalid
home-and-away state, incorrect season/context, deterministic finals
consumption of the snapshot, the normal mathematical pathway when no
snapshot exists, live/2027 isolation, and that this cannot be used as a
generic ladder editor.
"""

import inspect
import tempfile
from pathlib import Path

import pytest

from app.audit import ActorContext, AuditEventRepository
from app.db import connect
from app.finals_seeding import (
    FINALS_SEEDING_SNAPSHOT_CREATED,
    HISTORICAL_FINALS_SEED_TEAM_NAMES,
    FinalsSeedingConflictError,
    FinalsSeedingContextError,
    FinalsSeedingRepository,
    FinalsSeedingResolutionError,
    UnresolvedLadderTieError,
    resolve_finals_seed_order,
)
from app.identity import IdentityRepository
from app.ladder import LadderRepository
from app.migrations import migrate
from tests.finals_seeding_helpers import all_draws, build_2026_replay_season
from tests.midseason_draft_helpers import build_season

ACTOR = ActorContext.anonymous_operator("replay_operator")

HISTORICAL_ORDER_NAMES = [
    "Running Hots",
    "Bridesmaids",
    "JHAS",
    "Wolverines",
    "Evil Absolutes",
    "The Crabs",
    "One Percenters",
    "Motherruckers",
    "Pommy Rules",
    "The Plague",
]


def _overviews(ctx):
    identities = IdentityRepository(ctx["database"])
    return [identities.get_public_team(entry.season_entry_id) for entry in ctx["entries"]]


def _entries_by_name(ctx):
    return {entry.team_name: entry.season_entry_id for entry in _overviews(ctx)}


def _repo(ctx):
    return FinalsSeedingRepository(ctx["database"])


# -- 1. Preview never mutates -------------------------------------------


def test_preview_reports_readiness_without_writing_anything():
    ctx = build_2026_replay_season()
    report = _repo(ctx).preview(ctx["season"].season_id, ctx["competition"].competition_id)

    assert report["replay_context_ready"] is True
    assert report["diagnostic"] is None
    assert report["snapshot_exists"] is False
    assert report["apply_permitted"] is True
    assert [row["team_name"] for row in report["historical_seed_order"]] == HISTORICAL_ORDER_NAMES
    assert len(report["mathematical_order"]) == 10

    # No table this module owns has a row yet.
    assert ctx["database"].execute("SELECT COUNT(*) AS n FROM finals_seeding_snapshot").fetchone()["n"] == 0


def test_preview_reports_a_clean_diagnostic_before_the_owning_migration_has_run():
    """Codex review (PR #188): `finals_seeding_snapshot` did not exist
    before `0028_finals_seeding` -- a database still at the prior head must
    get a clean diagnostic from `preview`, not an unhandled database
    error, matching every other fail-closed diagnostic in this module."""
    path = Path(tempfile.mkstemp(suffix=".db")[1])
    migrate(f"sqlite:///{path}", "0027_midseason_draft")
    database = connect(f"sqlite:///{path}")
    ctx = build_2026_replay_season(database=database)

    report = _repo(ctx).preview(ctx["season"].season_id, ctx["competition"].competition_id)
    assert report["replay_context_ready"] is False
    assert report["snapshot_exists"] is False
    assert report["apply_permitted"] is False
    assert "0028_finals_seeding" in report["diagnostic"]


def test_preview_reports_material_differences_for_the_three_named_teams():
    ctx = build_2026_replay_season()
    report = _repo(ctx).preview(ctx["season"].season_id, ctx["competition"].competition_id)
    by_team = {row["team_name"]: row for row in report["material_differences"]}
    assert set(by_team) == {"Running Hots", "Evil Absolutes", "Motherruckers"}
    assert by_team["Running Hots"]["historical"] == {"wins": 13, "losses": 7, "competition_points": 52}
    assert by_team["Evil Absolutes"]["historical"] == {"wins": 10, "losses": 10, "competition_points": 40}
    assert by_team["Motherruckers"]["historical"] == {"wins": 9, "losses": 11, "competition_points": 36}
    # The mathematical numbers come straight from the live ladder, not from
    # the historical constants above.
    ladder = LadderRepository(ctx["database"]).snapshot(ctx["competition"].competition_id, 20)
    entries_by_name = _entries_by_name(ctx)
    ladder_by_entry = {row.season_entry_id: row for row in ladder.rows}
    for team_name, diff in by_team.items():
        row = ladder_by_entry[entries_by_name[team_name]]
        assert diff["mathematical"] == {
            "wins": row.wins,
            "losses": row.losses,
            "competition_points": row.competition_points,
        }


# -- 2/3. Mathematical ladder unchanged; exact historical seed order -----


def test_apply_leaves_the_mathematical_ladder_exactly_unchanged():
    ctx = build_2026_replay_season()
    before = LadderRepository(ctx["database"]).snapshot(ctx["competition"].competition_id, 20)

    _repo(ctx).apply(
        ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="issue #187 regression test"
    )

    after = LadderRepository(ctx["database"]).snapshot(ctx["competition"].competition_id, 20)
    assert after == before
    # The mathematical order equals season-entry creation order under
    # `dominant_scores` -- Bridesmaids first, not Running Hots.
    assert [row.season_entry_id for row in after.rows] == [e.season_entry_id for e in ctx["entries"]]


def test_apply_produces_exactly_the_required_historical_seed_order():
    ctx = build_2026_replay_season()
    result = _repo(ctx).apply(
        ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="issue #187 regression test"
    )
    assert result["created"] is True
    entries_by_name = _entries_by_name(ctx)
    expected = [(position, entries_by_name[HISTORICAL_ORDER_NAMES[position - 1]]) for position in range(1, 11)]
    assert result["seed_positions"] == expected

    snapshot = _repo(ctx).get_snapshot(ctx["season"].season_id)
    assert [(row.seed_position, row.season_entry_id) for row in snapshot.seed_rows] == expected
    assert snapshot.through_round == 20
    assert snapshot.competition_id == ctx["competition"].competition_id


def test_pommie_and_pommy_rules_spelling_both_resolve_to_the_same_position():
    ctx = build_2026_replay_season()
    identities = IdentityRepository(ctx["database"])
    entries_by_name = _entries_by_name(ctx)
    pommy_id = entries_by_name["Pommy Rules"]
    identities.rename_team(pommy_id, "Pommie Rules", actor=ACTOR, reason="alternate historical spelling")

    result = _repo(ctx).apply(
        ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="issue #187 regression test"
    )
    assert (9, pommy_id) in result["seed_positions"]


# -- 4/5. Successful apply; audit provenance -----------------------------


def test_apply_records_actor_reason_and_before_after_provenance():
    ctx = build_2026_replay_season()
    result = _repo(ctx).apply(
        ctx["season"].season_id,
        ctx["competition"].competition_id,
        actor=ACTOR,
        reason="issue #187: historical Round 12/13 Scorer-error finals-seeding exception",
    )
    events = AuditEventRepository(ctx["database"]).list_events(action=FINALS_SEEDING_SNAPSHOT_CREATED)
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "anonymous_operator"
    assert event.actor_role == "replay_operator"
    assert event.reason == "issue #187: historical Round 12/13 Scorer-error finals-seeding exception"
    assert event.entity_id == result["snapshot_id"]
    assert event.occurred_at

    entries_by_name = _entries_by_name(ctx)
    assert event.before_state["mathematical_order"][0]["season_entry_id"] == entries_by_name["Bridesmaids"]
    assert event.after_state["historical_seed_order"][0] == {
        "seed_position": 1,
        "season_entry_id": entries_by_name["Running Hots"],
    }
    assert "Round 12" in event.payload["historical_rationale"]
    assert "Round 13" in event.payload["historical_rationale"]


def test_apply_requires_an_explicit_substantive_reason():
    ctx = build_2026_replay_season()
    with pytest.raises(ValueError):
        _repo(ctx).apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="")
    with pytest.raises(ValueError):
        _repo(ctx).apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="   ")


# -- 6. Repeated identical application is idempotent ----------------------


def test_repeated_identical_apply_is_idempotent():
    ctx = build_2026_replay_season()
    repo = _repo(ctx)
    first = repo.apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="first pass")
    second = repo.apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="second pass")

    assert first["created"] is True
    assert second["created"] is False
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["seed_positions"] == first["seed_positions"]

    assert ctx["database"].execute("SELECT COUNT(*) AS n FROM finals_seeding_snapshot").fetchone()["n"] == 1
    events = AuditEventRepository(ctx["database"]).list_events(action=FINALS_SEEDING_SNAPSHOT_CREATED)
    assert len(events) == 1


# -- 7. Conflicting/invalid second application fails closed --------------


def test_conflicting_second_application_fails_closed_without_mutation():
    ctx = build_2026_replay_season()
    repo = _repo(ctx)
    repo.apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="first pass")

    identities = IdentityRepository(ctx["database"])
    entries_by_name = _entries_by_name(ctx)
    running_hots_id = entries_by_name["Running Hots"]
    plague_id = entries_by_name["The Plague"]
    # Swap two team names -- still resolves to a complete, unambiguous set
    # of ten entries, but a *different* one than the existing snapshot.
    identities.rename_team(running_hots_id, "Temp Name", actor=ACTOR, reason="test swap")
    identities.rename_team(plague_id, "Running Hots", actor=ACTOR, reason="test swap")
    identities.rename_team(running_hots_id, "The Plague", actor=ACTOR, reason="test swap")

    with pytest.raises(FinalsSeedingConflictError):
        repo.apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="second pass")

    assert ctx["database"].execute("SELECT COUNT(*) AS n FROM finals_seeding_snapshot").fetchone()["n"] == 1
    events = AuditEventRepository(ctx["database"]).list_events(action=FINALS_SEEDING_SNAPSHOT_CREATED)
    assert len(events) == 1


def test_reapplying_against_an_unrelated_competition_id_fails_closed():
    """A nonexistent/unrelated competition_id fails the same replay-context
    check `apply` always runs first -- it never reaches the "does this match
    the existing snapshot" conflict check with an invalid context."""
    ctx = build_2026_replay_season()
    repo = _repo(ctx)
    repo.apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="first pass")
    with pytest.raises(FinalsSeedingContextError):
        repo.apply(ctx["season"].season_id, "a-different-competition-id", actor=ACTOR, reason="second pass")
    assert ctx["database"].execute("SELECT COUNT(*) AS n FROM finals_seeding_snapshot").fetchone()["n"] == 1


# -- 8. Incomplete/invalid home-and-away state ----------------------------


def test_apply_refuses_when_round_20_is_not_yet_complete():
    ctx = build_2026_replay_season(trigger_round=19)
    with pytest.raises(FinalsSeedingContextError):
        _repo(ctx).apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="too early")
    report = _repo(ctx).preview(ctx["season"].season_id, ctx["competition"].competition_id)
    assert report["replay_context_ready"] is False
    assert "20" in report["diagnostic"]
    assert ctx["database"].execute("SELECT COUNT(*) AS n FROM finals_seeding_snapshot").fetchone()["n"] == 0


def test_apply_refuses_when_season_is_not_configured_as_twenty_rounds():
    ctx = build_2026_replay_season(trigger_round=9, regular_season_round_count=9)
    with pytest.raises(FinalsSeedingContextError):
        _repo(ctx).apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="wrong shape")


# -- 9. Incorrect season/context (live/2027 isolation) --------------------


def test_apply_refuses_for_a_season_that_is_not_the_2026_replay_year():
    ctx = build_2026_replay_season(year=2027)
    with pytest.raises(FinalsSeedingContextError, match="2026"):
        _repo(ctx).apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="2027 attempt")
    assert ctx["database"].execute("SELECT COUNT(*) AS n FROM finals_seeding_snapshot").fetchone()["n"] == 0


def test_apply_refuses_for_an_unknown_season_id():
    ctx = build_2026_replay_season()
    with pytest.raises(KeyError):
        _repo(ctx).apply("no-such-season", ctx["competition"].competition_id, actor=ACTOR, reason="bogus")


def test_apply_refuses_when_team_names_do_not_resolve():
    ctx = build_2026_replay_season()
    identities = IdentityRepository(ctx["database"])
    entries_by_name = _entries_by_name(ctx)
    identities.rename_team(entries_by_name["The Plague"], "Something Else Entirely", actor=ACTOR, reason="test")
    with pytest.raises(FinalsSeedingResolutionError):
        _repo(ctx).apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="renamed")


# -- 10. Deterministic finals consumption; absence-of-snapshot pathway ----


def test_resolve_finals_seed_order_uses_the_snapshot_when_one_exists():
    ctx = build_2026_replay_season()
    entries_by_name = _entries_by_name(ctx)
    _repo(ctx).apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="apply")

    order = resolve_finals_seed_order(ctx["database"], ctx["season"].season_id, ctx["competition"].competition_id)
    assert order == tuple(entries_by_name[name] for name in HISTORICAL_ORDER_NAMES)


def test_resolve_finals_seed_order_falls_back_to_the_mathematical_ladder_without_a_snapshot():
    ctx = build_2026_replay_season()
    order = resolve_finals_seed_order(ctx["database"], ctx["season"].season_id, ctx["competition"].competition_id)
    ladder = LadderRepository(ctx["database"]).snapshot(ctx["competition"].competition_id, 20)
    assert order == tuple(row.season_entry_id for row in ladder.rows)
    assert order == tuple(e.season_entry_id for e in ctx["entries"])  # Bridesmaids first, mathematically


def test_resolve_finals_seed_order_ignores_a_snapshot_scoped_to_a_different_competition():
    ctx = build_2026_replay_season()
    _repo(ctx).apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="apply")
    # A snapshot exists for this season, but scoped to a different
    # competition_id must never be handed back silently -- the fallback
    # mathematical pathway is attempted instead, which fails for an unknown
    # competition, exactly as `LadderRepository.snapshot` documents.
    with pytest.raises(KeyError):
        resolve_finals_seed_order(ctx["database"], ctx["season"].season_id, "some-other-competition-id")


def test_resolve_finals_seed_order_rejects_a_competition_id_from_a_different_season():
    """Codex review (PR #188): `LadderRepository.snapshot` derives its own
    season solely from `competition_id` and never checks the caller's
    `season_id` -- an accidental cross-season `competition_id` must fail
    closed rather than silently seeding one season from another season's
    ladder."""
    ctx = build_2026_replay_season()
    other = build_season(database=ctx["database"], year=2201, trigger_round=1, regular_season_round_count=1)
    with pytest.raises(FinalsSeedingContextError):
        resolve_finals_seed_order(ctx["database"], ctx["season"].season_id, other["competition"].competition_id)


def test_resolve_finals_seed_order_refuses_an_unresolved_ladder_tie_without_a_snapshot():
    """Codex review (PR #188): `LadderSnapshot.rows`' `season_entry_id`
    ordering exists solely for repeatable serialization of an unresolved
    tie, never as a real tiebreak -- the mathematical-ladder fallback must
    refuse rather than silently let a UUID decide finals seeding."""
    ctx = build_2026_replay_season(score_fn=all_draws)
    ladder = LadderRepository(ctx["database"]).snapshot(ctx["competition"].competition_id, 20)
    assert all(row.tied for row in ladder.rows)  # sanity: a genuine full tie
    with pytest.raises(UnresolvedLadderTieError):
        resolve_finals_seed_order(ctx["database"], ctx["season"].season_id, ctx["competition"].competition_id)


def test_resolve_finals_seed_order_from_a_snapshot_ignores_a_tied_live_ladder():
    """The historical snapshot path never touches `LadderRow.tied` -- it
    returns the already-resolved, audited historical order directly and
    never even reaches the mathematical-ladder tie check."""
    ctx = build_2026_replay_season(score_fn=all_draws)
    ladder = LadderRepository(ctx["database"]).snapshot(ctx["competition"].competition_id, 20)
    assert all(row.tied for row in ladder.rows)  # the live ladder is genuinely, fully tied
    _repo(ctx).apply(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR, reason="apply")

    order = resolve_finals_seed_order(ctx["database"], ctx["season"].season_id, ctx["competition"].competition_id)
    assert len(order) == 10


# -- 11. Live/2027 isolation, generic-ladder-editor guard ------------------


def test_a_2027_season_can_never_satisfy_the_replay_context():
    ctx = build_2026_replay_season(year=2027)
    report = _repo(ctx).preview(ctx["season"].season_id, ctx["competition"].competition_id)
    assert report["replay_context_ready"] is False
    assert "2026" in report["diagnostic"]


def test_apply_accepts_no_caller_supplied_seed_order_parameter():
    """Structural guard against #187 becoming a generic ladder editor: the
    only order `apply` can ever write is the fixed historical constant."""
    params = set(inspect.signature(FinalsSeedingRepository.apply).parameters)
    assert params == {"self", "season_id", "competition_id", "actor", "reason"}
    assert len(HISTORICAL_FINALS_SEED_TEAM_NAMES) == 10
