"""Issue #67 checkpoint: deterministic replay validation for the difficult
first-half-2026 cases (staged/early lockout, ordinary bye/availability,
missing/partial submission with carry-forward/proxy, and Opening Round
deferred/compensating-bye mixed-source scoring) that #66's one-round replay
harness deliberately deferred.

Every scenario below drives the same production repositories/services #66
and #69 already implement -- `app.lockouts`, `app.lineup_validation`,
`app.carry_forward`, `app.lineup_proxy`, `app.opening_round`,
`app.calculations`, `app.round_review`, `app.ladder` -- through
`app.replay.ReplayAflDataSource`/`ReplayClock` for controlled evidence and
time. `app.replay_checkpoint` contributes no sporting rule of its own; it
only shapes each scenario's already-computed results into one deterministic,
comparable report (see that module's docstring).

SYNTHETIC EVIDENCE NOTICE: every player, match and lineup in
tests/fixtures/replay_checkpoint_2026/ is an explicitly synthetic scenario
built to exercise these mechanisms deterministically -- not a claimed
historical 2026 BBBFFL/AFL fact, except where a fixture's own provenance
records `known_fact` for a specific field (the 2026 AFL round identity/
mapping). See each fixture's `manifest.description` and per-record
`provenance`.
"""

import json
from pathlib import Path

import pytest

from app.afl_client import Team
from app.audit import ActorContext
from app.calculations import MatchupCalculationService
from app.carry_forward import CarryForwardService, NoCarryForwardSourceError
from app.identity import IdentityRepository
from app.ladder import LadderRepository
from app.lineup_proxy import LineupProxyService
from app.lineup_validation import LineupValidationService
from app.lineups import POSITIONS, WeeklyLineupRepository
from app.lockouts import (
    LockedSelectionError,
    LockoutRepository,
    LockoutTriggerRepository,
    RoundMatchFactsProvider,
)
from app.opening_round import (
    DeferredSlotLockedError,
    OpeningRoundNominationRepository,
    OpeningRoundRuleRepository,
    OpeningRoundSelectionGuard,
)
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.replay import ReplayAflDataSource, ReplayClock
from app.replay_checkpoint import (
    HistoricalStatus,
    build_checkpoint_scenario,
    build_checkpoint_suite,
    write_checkpoint_suite_report,
)
from app.round_mapping import AflApiReferenceValidator, RoundMappingRepository
from app.round_review import RoundReviewRepository, attempt_signoff, build_round_review
from tests.db_helpers import migrated_connection
from tests.lineup_helpers import complete_lineup
from tests.round_review_helpers import progress_to_review
from tests.test_carry_forward import SCOPE_SQL, acquire_players, submit_round
from tests.test_carry_forward import context as carry_forward_context
from tests.test_competition_lifecycle import operational

FIXTURES = Path(__file__).parent / "fixtures" / "replay_checkpoint_2026"
OPERATOR = ActorContext.anonymous_operator(role="scorer")
SCORER = ActorContext.anonymous_operator(role="scorer")


def _canonical_positions(db, positions):
    """Deterministic report form of a `{position: season_player_id}`
    mapping: the internal random UUID is never comparable across reruns, so
    reports always carry the stable `canonical_player_id` instead."""
    out = {}
    for position, player_id in positions.items():
        row = db.execute(
            "SELECT canonical_player_id FROM season_player_pool WHERE season_player_id=?", (player_id,)
        ).fetchone()
        out[position] = row["canonical_player_id"] if row else None
    return out


def _acquire_evidence_player(pool, ownership, scope, entry, source, canonical_player_id):
    evidence_player = source.get_player(canonical_player_id)
    season_player = pool.refresh_player(
        scope["season_id"],
        canonical_player_id,
        evidence_player.name,
        afl_team_id=evidence_player.current_team.team_id,
        afl_team_name=evidence_player.current_team.name,
        source_provider="replay:checkpoint-2026@1.0.0",
    )
    ownership.acquire(
        season_player.season_player_id, entry.season_entry_id, actor=OPERATOR, reason="checkpoint squad setup"
    )
    return season_player


def _early_lockout_scenario():
    """Issue #67 requirement 2: staged/early lockout, proven through
    prohibited-mutation attempts rather than merely inspecting a computed
    `locked=true` flag."""
    fixture = FIXTURES / "early_lockout_evidence.json"
    before = ReplayClock.from_iso("2026-04-01T00:00:00Z")
    between = ReplayClock.from_iso("2026-04-02T10:00:00Z")
    after = ReplayClock.from_iso("2026-04-04T10:00:00Z")
    before_source = ReplayAflDataSource(fixture, clock=before)
    between_source = ReplayAflDataSource(fixture, clock=between)
    after_source = ReplayAflDataSource(fixture, clock=after)

    db = migrated_connection()
    lifecycle, round_, entries = operational(db, year=2026, afl_round=1346)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(
        "SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c "
        "ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    entry = entries[0]
    pool, ownership = PlayerPoolRepository(db), OwnershipRepository(db)
    ownership.configure_squad_limit(scope["season_id"], 20)

    manifest_lineup = before_source.lineup_inputs[0]
    positions = {}
    canonical_by_season_player_id = {}
    for position, canonical in manifest_lineup["positions"].items():
        season_player = _acquire_evidence_player(pool, ownership, scope, entry, before_source, canonical)
        positions[position] = season_player.season_player_id
        canonical_by_season_player_id[season_player.season_player_id] = canonical
    bypass_player = _acquire_evidence_player(pool, ownership, scope, entry, before_source, 5010)
    edit_player = _acquire_evidence_player(pool, ownership, scope, entry, before_source, 5011)
    canonical_by_season_player_id[bypass_player.season_player_id] = 5010
    canonical_by_season_player_id[edit_player.season_player_id] = 5011

    # `operational()` already accepts the round's AFL mapping (year=2026, afl_round=1346).
    mappings = RoundMappingRepository(db)
    triggers = LockoutTriggerRepository(db)
    triggers.create(
        round_.bbbffl_round_id,
        "early",
        "selective",
        1,
        [2701],
        actor=OPERATOR,
        reason="checkpoint Thursday selective lockout stage",
    )
    triggers.create(
        round_.bbbffl_round_id,
        "main",
        "main",
        2,
        [2702],
        actor=OPERATOR,
        reason="checkpoint Saturday main lockout stage",
    )

    lineups = WeeklyLineupRepository(db)
    lockouts = LockoutRepository(db)

    def guard_at(source, clock):
        return lockouts.guard(match_facts=RoundMatchFactsProvider(mappings, source), evaluation_at=clock.now())

    lineup_id, _ = lineups.get_or_create_header(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    submitted_v1 = lineups.submit_positions(
        lineup_id,
        positions,
        expected_submission_version=0,
        actor=OPERATOR,
        source_type="scorer_proxy",
        reason="checkpoint: initial submission before either lockout stage",
        lock_guard=guard_at(before_source, before),
    )
    assert submitted_v1.version == 1

    lock_before = lockouts.lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        positions,
        match_facts=RoundMatchFactsProvider(mappings, before_source),
        evaluation_at=before.now(),
    )
    lock_between = lockouts.lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        positions,
        match_facts=RoundMatchFactsProvider(mappings, between_source),
        evaluation_at=between.now(),
    )
    assert {p.state.value for p in lock_before.positions.values()} == {"editable"}
    assert lock_between.positions["F1"].state.value == "locked"
    assert lock_between.positions["F1"].reason == "selective_trigger_activated"
    assert lock_between.positions["F1"].afl_match_id == 2701
    still_editable = {"F2", "F3", "M1", "M2", "M3", "Ruck", "Tackler", "Interchange"}
    assert {lock_between.positions[position].state.value for position in still_editable} == {"editable"}

    # Prohibited: mutate the now-locked F1 position.
    attempted_locked_mutation = dict(positions)
    attempted_locked_mutation["F1"] = bypass_player.season_player_id
    with pytest.raises(LockedSelectionError):
        lineups.submit_positions(
            lineup_id,
            attempted_locked_mutation,
            expected_submission_version=1,
            actor=OPERATOR,
            source_type="scorer_proxy",
            reason="checkpoint: attempted mutation of a locked position",
            lock_guard=guard_at(between_source, between),
        )

    # Prohibited: Interchange cannot bring in a player from the now-locked match.
    attempted_bypass = dict(positions)
    attempted_bypass["Interchange"] = bypass_player.season_player_id
    with pytest.raises(LockedSelectionError):
        lineups.submit_positions(
            lineup_id,
            attempted_bypass,
            expected_submission_version=1,
            actor=OPERATOR,
            source_type="scorer_proxy",
            reason="checkpoint: attempted Interchange bypass of an activated selective trigger",
            lock_guard=guard_at(between_source, between),
        )

    # Permitted: edit a still-editable position.
    permitted_edit = dict(positions)
    permitted_edit["M1"] = edit_player.season_player_id
    submitted_v2 = lineups.submit_positions(
        lineup_id,
        permitted_edit,
        expected_submission_version=1,
        actor=OPERATOR,
        source_type="scorer_proxy",
        reason="checkpoint: permitted edit of a still-editable position",
        lock_guard=guard_at(between_source, between),
    )
    assert submitted_v2.version == 2
    assert submitted_v2.positions["M1"] == edit_player.season_player_id
    assert submitted_v2.positions["F1"] == positions["F1"]

    # Immutable submission history: version 1 is untouched by the version-2 resubmission.
    v1_reread = lineups.get_submission(lineup_id, 1)
    assert v1_reread.positions == positions

    lock_after = lockouts.lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted_v2.positions,
        match_facts=RoundMatchFactsProvider(mappings, after_source),
        evaluation_at=after.now(),
    )
    assert {p.state.value for p in lock_after.positions.values()} == {"locked"}
    assert lock_after.positions["M1"].reason == "main_lockout_triggered"

    # Main lockout now covers the position that was still editable a moment ago.
    attempted_after_main = dict(submitted_v2.positions)
    attempted_after_main["M2"] = bypass_player.season_player_id
    with pytest.raises(LockedSelectionError):
        lineups.submit_positions(
            lineup_id,
            attempted_after_main,
            expected_submission_version=2,
            actor=OPERATOR,
            source_type="scorer_proxy",
            reason="checkpoint: attempted mutation after main lockout activated",
            lock_guard=guard_at(after_source, after),
        )

    return build_checkpoint_scenario(
        "early_lockout",
        "A staged BBBFFL round with a Thursday selective lockout stage (one AFL match) followed by a "
        "Saturday main lockout stage (a second AFL match); proves per-match staged locking, a rejected "
        "mutation of a locked position, Interchange unable to bypass an activated trigger, a permitted "
        "resubmission of a still-editable position, and that the immutable version-1 submission history "
        "survives that resubmission.",
        historical_or_synthetic=HistoricalStatus.SYNTHETIC,
        evidence_sources=[str(fixture)],
        evidence_classification=before_source.evidence_records(),
        clocks={
            "before": before.now().isoformat(),
            "between": between.now().isoformat(),
            "after": after.now().isoformat(),
        },
        starting_lineup=[
            {"position": position, "canonical_player_id": canonical}
            for position, canonical in manifest_lineup["positions"].items()
        ],
        lineup_provenance=[manifest_lineup["provenance"]],
        afl_match_state=[
            {"afl_match_id": match.match_id, "status": match.status, "start_time_utc": match.start_time_utc}
            for match in after_source.get_matches(1346)
        ],
        lockout=[
            {
                "checkpoint": "before",
                "positions": {
                    p: {"state": s.state.value, "reason": s.reason, "afl_match_id": s.afl_match_id}
                    for p, s in lock_before.positions.items()
                },
            },
            {
                "checkpoint": "between",
                "positions": {
                    p: {"state": s.state.value, "reason": s.reason, "afl_match_id": s.afl_match_id}
                    for p, s in lock_between.positions.items()
                },
            },
            {
                "checkpoint": "after",
                "positions": {
                    p: {"state": s.state.value, "reason": s.reason, "afl_match_id": s.afl_match_id}
                    for p, s in lock_after.positions.items()
                },
            },
        ],
        official_result={
            "submission_version_history": [1, 2],
            "final_positions": {
                position: canonical_by_season_player_id[player_id]
                for position, player_id in submitted_v2.positions.items()
            },
        },
    )


def test_checkpoint_staged_early_lockout_scenario():
    scenario = _early_lockout_scenario()
    assert scenario["outcome"] == "PASS"
    assert scenario["lockout"][0]["positions"]["F1"]["state"] == "editable"
    assert scenario["lockout"][1]["positions"]["F1"]["state"] == "locked"
    assert scenario["lockout"][2]["positions"]["M1"]["state"] == "locked"


def test_checkpoint_staged_early_lockout_scenario_is_deterministic_on_rerun():
    first = _early_lockout_scenario()
    second = _early_lockout_scenario()
    assert first == second


def _bye_availability_scenario():
    """Issue #67 requirement 3: an ordinary AFL club bye is a visible,
    attributable warning that never becomes an automatic BBBFFL DNP ruling
    or a silent replacement -- kept explicitly separate from a genuinely
    ambiguous slot (a player whose club played, but afl-api never returned
    a stat row), which remains explicit scorer input via #58's round
    review, exactly as any other exceptional evidence gap would."""
    fixture = FIXTURES / "bye_availability_evidence.json"
    clock = ReplayClock.from_iso("2026-04-11T13:00:00Z")
    source = ReplayAflDataSource(fixture, clock=clock)

    db = migrated_connection()
    lifecycle, round_, entries = operational(db, year=2026, afl_round=1400)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(SCOPE_SQL, (round_.bbbffl_round_id,)).fetchone()
    pool, ownership = PlayerPoolRepository(db), OwnershipRepository(db)
    ownership.configure_squad_limit(scope["season_id"], 20)

    entry = entries[0]
    manifest_lineup = source.lineup_inputs[0]
    positions = {}
    canonical_by_season_player_id = {}
    for position, canonical in manifest_lineup["positions"].items():
        season_player = _acquire_evidence_player(pool, ownership, scope, entry, source, canonical)
        positions[position] = season_player.season_player_id
        canonical_by_season_player_id[season_player.season_player_id] = canonical

    lineups = WeeklyLineupRepository(db)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        positions,
        expected_revision=0,
    )
    checked = LineupValidationService(db, source).validate_submission(draft.lineup_id, positions)
    assert checked.valid, checked.to_dict()
    bye_warnings = [m for m in checked.warnings if m.category == "availability" and m.code == "afl_club_bye"]
    assert len(bye_warnings) == 1
    bye_warning = bye_warnings[0]
    assert bye_warning.position == "F1"
    assert bye_warning.season_player_id == positions["F1"]
    assert bye_warning.details["afl_team_id"] == 603
    assert bye_warning.details["dnp"] is False
    # No other warning/error touched the lineup -- an ordinary bye does not
    # optimise or otherwise alter the submitted team.
    assert {m.code for m in checked.messages} == {"afl_club_bye"}

    submitted = lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)
    assert submitted.positions == positions

    # Every other entry needs a complete, ordinarily-resolvable lineup for
    # the round to calculate/review at all; they are filled with the
    # bye-club's own players (deliberately, so their absent afl-api stat
    # rows read as an ordinary club bye too, never as a second ambiguous
    # case unrelated to this scenario's focus).
    filler_team = Team(603, "Bye Club")
    for other in entries[1:]:
        submit_round(lineups, scope, round_.bbbffl_round_id, other, {}, neutral_team=filler_team)

    progress_to_review(lifecycle, round_.bbbffl_round_id)
    MatchupCalculationService(db, source).calculate_round(
        round_.bbbffl_round_id, upstream_revision=source.manifest["version"], observed_at=clock.now().isoformat()
    )
    review_repo = RoundReviewRepository(db)
    identities = IdentityRepository(db)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    focus_matchup = next(
        m
        for m in lifecycle.list_matchups(round_.bbbffl_round_id)
        if entry.season_entry_id in (m.home_season_entry_id, m.away_season_entry_id)
    )
    blocked = next(m for m in review.matchups if m.matchup_id == focus_matchup.matchup_id)
    assert review.ready_for_signoff is False
    assert any("Ruck" in reason for reason in blocked.blockers)
    # The bye slot (F1) never appears as a blocker: an ordinary club bye is
    # not a DNP ruling and does not require scorer input.
    assert not any("F1" in reason for reason in blocked.blockers)

    unresolved_before_ruling = [
        {"matchup_id": focus_matchup.matchup_id, "slot": "Ruck", "reason": reason}
        for reason in blocked.blockers
        if "Ruck" in reason
    ]

    ruling_version = review_repo.record_dnp_ruling(
        focus_matchup.matchup_id,
        entry.season_entry_id,
        "Ruck",
        True,
        expected_review_version=blocked.review_version,
        actor=SCORER,
        reason="checkpoint: confirmed unavailable, no afl-api stat row despite playing (genuinely exceptional evidence gap)",
    )
    review_repo.record_interchange_ruling(
        focus_matchup.matchup_id,
        entry.season_entry_id,
        "Ruck",
        expected_review_version=ruling_version,
        actor=SCORER,
        reason="checkpoint: cover Ruck with the interchange",
    )
    ready_review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    assert ready_review.ready_for_signoff is True

    published = attempt_signoff(
        lifecycle,
        review_repo,
        identities,
        round_.bbbffl_round_id,
        actor=SCORER,
        reason="checkpoint: bye/availability round sign-off",
    )
    assert published.state == "final"
    result = lifecycle.effective_result(focus_matchup.matchup_id)
    frozen_side = (
        result.input_snapshot["home"]
        if result.input_snapshot["home"]["season_entry_id"] == entry.season_entry_id
        else result.input_snapshot["away"]
    )
    ruck_frozen = next(s for s in frozen_side["slots"] if s["slot"] == "Ruck")

    calculation_row = db.execute(
        "SELECT snapshot FROM bbbffl_matchup_calculation WHERE matchup_id=?", (focus_matchup.matchup_id,)
    ).fetchone()
    calculated = json.loads(calculation_row["snapshot"])
    calculated_side = (
        calculated["home"] if calculated["home"]["season_entry_id"] == entry.season_entry_id else calculated["away"]
    )
    f1_calculated = next(s for s in calculated_side["slots"] if s["position"] == "F1")
    ruck_calculated = next(s for s in calculated_side["slots"] if s["position"] == "Ruck")

    # Every entry was fully lineup'd and the whole round was calculated
    # (not just the focus matchup), so the ladder snapshot immediately
    # after finalisation is genuinely meaningful here -- not left empty.
    round_context = db.execute(
        "SELECT l.fixture_round_number FROM bbbffl_round_lifecycle l WHERE l.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    team_by_entry = {e.season_entry_id: identities.get_public_team(e.season_entry_id).team_name for e in entries}
    ladder = LadderRepository(db).snapshot(scope["competition_id"], round_context["fixture_round_number"])
    ladder_effect = sorted(
        (
            {
                "rank": row.rank,
                "team": team_by_entry[row.season_entry_id],
                "played": row.played,
                "wins": row.wins,
                "draws": row.draws,
                "losses": row.losses,
                "points": row.competition_points,
                "percentage": str(row.percentage),
                "points_for": str(row.points_for),
                "tie_group": sorted(team_by_entry[entry_id] for entry_id in row.tie_group),
            }
            for row in ladder.rows
        ),
        key=lambda item: (item["rank"], item["team"]),
    )

    return build_checkpoint_scenario(
        "bye_availability",
        "An ordinary BBBFFL round where one selected player's AFL club has a normal scheduled bye (F1) and a "
        "second selected player's club played but afl-api returned no stat row for them (Ruck, a genuinely "
        "ambiguous evidence gap). Proves the bye produces a visible, attributable, non-blocking warning with "
        "no automatic DNP and no silent replacement, while the truly ambiguous slot blocks sign-off until an "
        "explicit scorer ruling is recorded.",
        historical_or_synthetic=HistoricalStatus.SYNTHETIC,
        evidence_sources=[str(fixture)],
        evidence_classification=source.evidence_records(),
        clocks={"observed_at": clock.now().isoformat()},
        starting_lineup=[
            {"position": position, "canonical_player_id": canonical}
            for position, canonical in manifest_lineup["positions"].items()
        ],
        lineup_provenance=[manifest_lineup["provenance"]],
        afl_match_state=[
            {"afl_match_id": match.match_id, "status": match.status} for match in source.get_matches(1400)
        ],
        validation_warnings=[m.to_dict() for m in checked.messages if m.category != "availability"],
        availability_warnings=[
            {
                **{k: v for k, v in warning.to_dict().items() if k != "season_player_id"},
                "canonical_player_id": canonical_by_season_player_id[warning.season_player_id],
            }
            for warning in bye_warnings
        ],
        unresolved_questions=[],
        calculated_result={
            "f1_bye": {"participation_state": f1_calculated["participation"]["state"], "score": f1_calculated["score"]},
            "ruck_ambiguous": {
                "participation_state": ruck_calculated["participation"]["state"],
                "dnp_ruling": ruck_frozen["dnp_ruling"],
            },
        },
        official_result={
            "home_score": str(result.home_score),
            "away_score": str(result.away_score),
            "version": result.version,
        },
        expected_vs_actual={
            "expected": "F1 scores zero as an ordinary club bye (not DNP); Ruck requires and receives an explicit scorer DNP ruling before sign-off",
            "actual": {
                "f1_participation_state": f1_calculated["participation"]["state"],
                "ruck_dnp_ruling": ruck_frozen["dnp_ruling"],
                "round_state": published.state,
            },
        },
        ladder_effect=ladder_effect,
        discrepancies=[],
    ), unresolved_before_ruling


def test_checkpoint_bye_availability_scenario():
    scenario, unresolved_before_ruling = _bye_availability_scenario()
    assert scenario["outcome"] == "PASS"
    assert scenario["availability_warnings"][0]["code"] == "afl_club_bye"
    assert scenario["availability_warnings"][0]["details"]["dnp"] is False
    assert scenario["calculated_result"]["f1_bye"]["participation_state"] == "club_bye"
    assert scenario["calculated_result"]["f1_bye"]["score"] == 0
    assert scenario["calculated_result"]["ruck_ambiguous"]["dnp_ruling"] is True
    # The exceptional case genuinely blocked sign-off before the explicit
    # scorer ruling was recorded -- never silently resolved.
    assert len(unresolved_before_ruling) == 1
    # The ladder snapshot immediately after finalisation is populated (all
    # ten entries played) and internally consistent, not left empty.
    assert len(scenario["ladder_effect"]) == 10
    assert {row["played"] for row in scenario["ladder_effect"]} == {1}
    assert [row["rank"] for row in scenario["ladder_effect"]] == sorted(
        row["rank"] for row in scenario["ladder_effect"]
    )


def test_checkpoint_bye_availability_scenario_is_deterministic_on_rerun():
    first, _ = _bye_availability_scenario()
    second, _ = _bye_availability_scenario()
    assert first == second


def _round1_no_prior_lineup_scenario():
    """Issue #67 requirement 4, "no previous lineup": Round 1 (or any
    round with no predecessor in this competition stream) must never
    manufacture a previous team. `CarryForwardService` refuses explicitly;
    this scenario's own outcome is deliberately `UNRESOLVED`, exactly as
    the issue requires ("a missing answer is a valid replay finding") --
    never silently treated as a pass."""
    db, lifecycle, rounds, entries, scope, pool, ownership = carry_forward_context(year=2026, rounds=1)
    entry = entries[0]
    service = CarryForwardService(db)

    resolved = service.resolve_source(scope["season_id"], scope["competition_id"], rounds[0], entry.season_entry_id)
    assert resolved is None
    with pytest.raises(NoCarryForwardSourceError) as excinfo:
        service.carry_forward(
            scope["season_id"],
            scope["competition_id"],
            rounds[0],
            entry.season_entry_id,
            expected_submission_version=0,
            actor=OPERATOR,
            reason="checkpoint: round 1 has no predecessor in this competition stream",
        )

    team_name = IdentityRepository(db).get_public_team(entry.season_entry_id).team_name
    return build_checkpoint_scenario(
        "round1_no_prior_lineup",
        "BBBFFL Round 1 of a 2026 competition stream (no predecessor round exists at all). Proves "
        "carry-forward refuses to invent a default/optimised team and instead surfaces an explicit, "
        "visibly unresolved state requiring scorer/admin confirmation or proxy entry.",
        historical_or_synthetic=HistoricalStatus.SYNTHETIC,
        evidence_sources=["synthetic 2026 season/competition/round construction (tests.test_carry_forward.context)"],
        evidence_classification=[
            {
                "kind": "season_round_identity",
                "identity": "round-1",
                "evidence_class": "synthetic_scenario",
                "source": "checkpoint test fixture season/round construction",
            },
            {
                "kind": "carry_forward_source",
                "identity": f"{team_name}:round-1",
                "evidence_class": "unresolved_scorer_input",
                "source": "no previous submitted lineup exists in this competition stream for this entry",
            },
        ],
        clocks={},
        starting_lineup=[],
        lineup_provenance=[],
        carry_forward=None,
        proxy_entry=None,
        unresolved_questions=[
            {
                "team": team_name,
                "bbbffl_round": "round-1",
                "question": "No previous submitted lineup exists for this entry in this competition stream, and "
                "this is Round 1 (no predecessor round at all); an explicit scorer/admin confirmation or proxy "
                "entry is required before this round can be scored for this entry.",
                "error_raised": type(excinfo.value).__name__,
            }
        ],
        discrepancies=[],
    )


def test_checkpoint_round1_no_prior_lineup_scenario():
    scenario = _round1_no_prior_lineup_scenario()
    assert scenario["outcome"] == "UNRESOLVED"
    assert len(scenario["unresolved_questions"]) == 1
    assert scenario["unresolved_questions"][0]["error_raised"] == "NoCarryForwardSourceError"
    assert scenario["carry_forward"] is None
    assert scenario["proxy_entry"] is None


def test_checkpoint_round1_no_prior_lineup_scenario_is_deterministic_on_rerun():
    first = _round1_no_prior_lineup_scenario()
    second = _round1_no_prior_lineup_scenario()
    assert first == second


def _carry_forward_and_proxy_scenario():
    """Issue #67 requirement 4: exact carry-forward with recorded
    provenance for one entry, and proxy entry (with proxy, not coach,
    provenance) for a second entry whose predecessor round is itself
    unrecoverable -- both distinct from a fabricated coach submission."""
    db, lifecycle, rounds, entries, scope, pool, ownership = carry_forward_context(year=2026, rounds=2, squad_limit=30)
    entry_cf, entry_proxy = entries[0], entries[1]
    lineups = WeeklyLineupRepository(db)
    identities = IdentityRepository(db)

    # Every position is explicitly acquired (never `complete_lineup`'s
    # auto-generated filler, whose canonical IDs are derived from the
    # entry's own randomly-generated UUID and would therefore differ
    # between reruns) so this scenario's report is exactly reproducible.
    players_cf = acquire_players(pool, ownership, scope, entry_cf, 6100, 9)
    players_proxy = acquire_players(pool, ownership, scope, entry_proxy, 6200, 9)
    positions_cf = {position: players_cf[index].season_player_id for index, position in enumerate(POSITIONS)}
    positions_proxy = {position: players_proxy[index].season_player_id for index, position in enumerate(POSITIONS)}

    # Round 1: a genuine coach submission for entry_cf -- the baseline this
    # scenario's round-2 carry-forward will copy exactly.
    _, round1_submission = submit_round(lineups, scope, rounds[0], entry_cf, positions_cf)
    assert round1_submission.source_type == "coach"

    # Round 2: entry_cf never resubmits by lockout -- exact carry-forward.
    carry_service = CarryForwardService(db)
    cf_submission, cf_source = carry_service.carry_forward(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry_cf.season_entry_id,
        expected_submission_version=0,
        actor=OPERATOR,
        reason="checkpoint: round 2 not submitted by lockout, exact carry-forward from round 1",
    )
    assert cf_submission.positions == round1_submission.positions
    assert cf_submission.source_type == "carry_forward"
    assert cf_submission.source_type != "coach"
    assert cf_source.source_bbbffl_round_id == rounds[0]
    assert cf_source.source_lineup_id == round1_submission.lineup_id
    assert cf_source.source_version == round1_submission.version

    # entry_proxy: no recoverable round-1 evidence at all (never submitted),
    # so round 2 has no carry-forward source either -- proxy entry is used
    # instead, attributed to the operator, never presented as a coach
    # submission.
    no_source = carry_service.resolve_source(
        scope["season_id"], scope["competition_id"], rounds[1], entry_proxy.season_entry_id
    )
    assert no_source is None
    proxy_service = LineupProxyService(db)
    proxy_draft = proxy_service.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry_proxy.season_entry_id,
        positions_proxy,
        expected_revision=0,
        actor=OPERATOR,
    )
    proxy_submission = proxy_service.submit(
        proxy_draft.lineup_id,
        expected_draft_revision=proxy_draft.revision,
        expected_submission_version=0,
        actor=OPERATOR,
        reason="checkpoint: no recoverable round-1 evidence for this entry; reconstructed via scorer proxy entry",
    )
    assert proxy_submission.source_type == "scorer_proxy"
    assert proxy_submission.source_type != "coach"
    assert proxy_submission.actor_role == "scorer"

    team_cf = identities.get_public_team(entry_cf.season_entry_id).team_name
    team_proxy = identities.get_public_team(entry_proxy.season_entry_id).team_name

    return build_checkpoint_scenario(
        "carry_forward_and_proxy_provenance",
        "A second 2026 round where one entry is carried forward exactly from its round-1 coach submission "
        "(never optimised, never hindsight-adjusted) and a second entry -- with no recoverable round-1 "
        "evidence at all -- is entered via the scorer/proxy mechanism. Proves both remain distinguishable "
        "from an authenticated coach submission, and that the carry-forward is an exact copy.",
        historical_or_synthetic=HistoricalStatus.SYNTHETIC,
        evidence_sources=["synthetic 2026 season/competition/round construction (tests.test_carry_forward.context)"],
        evidence_classification=[
            {
                "kind": "round1_coach_submission",
                "identity": team_cf,
                "evidence_class": "synthetic_scenario",
                "source": "checkpoint test fixture coach submission",
            },
            {
                "kind": "round2_carry_forward_source",
                "identity": team_cf,
                "evidence_class": "reconstructable_behaviour",
                "source": "deterministically derived from the round-1 submission via app.carry_forward",
            },
            {
                "kind": "round2_proxy_entry",
                "identity": team_proxy,
                "evidence_class": "unresolved_scorer_input",
                "source": "no recoverable round-1 coach evidence; entered via scorer proxy, not claimed as a coach submission",
            },
        ],
        clocks={},
        starting_lineup=[
            {"team": team_cf, "positions": _canonical_positions(db, round1_submission.positions)},
        ],
        lineup_provenance=[
            {"team": team_cf, "round": "round1", "source_type": round1_submission.source_type},
        ],
        carry_forward={
            "team": team_cf,
            "source_bbbffl_round": "round-1"
            if cf_source.source_bbbffl_round_id == rounds[0]
            else cf_source.source_bbbffl_round_id,
            "source_version": cf_source.source_version,
            "positions_match_source_exactly": cf_submission.positions == cf_source.positions,
            "target_positions": _canonical_positions(db, cf_submission.positions),
            "source_type": cf_submission.source_type,
        },
        proxy_entry={
            "team": team_proxy,
            "actor_type": proxy_submission.actor_type,
            "actor_role": proxy_submission.actor_role,
            "source_type": proxy_submission.source_type,
            "target_positions": _canonical_positions(db, proxy_submission.positions),
            "reason": proxy_submission.reason,
        },
        unresolved_questions=[],
        discrepancies=[],
    )


def test_checkpoint_carry_forward_and_proxy_scenario():
    scenario = _carry_forward_and_proxy_scenario()
    assert scenario["outcome"] == "PASS"
    assert scenario["carry_forward"]["positions_match_source_exactly"] is True
    assert scenario["carry_forward"]["source_type"] == "carry_forward"
    assert scenario["proxy_entry"]["source_type"] == "scorer_proxy"
    assert scenario["proxy_entry"]["actor_role"] == "scorer"


def test_checkpoint_carry_forward_and_proxy_scenario_is_deterministic_on_rerun():
    first = _carry_forward_and_proxy_scenario()
    second = _carry_forward_and_proxy_scenario()
    assert first == second


def _opening_round_deferred_scenario():
    """Issue #67 requirement 5 (folding in #69): one BBBFFL round where an
    ordinary slot scores from the current mapped AFL round while a second,
    previously-nominated slot scores from the player's own AFL Opening
    Round match -- driven entirely through #69's real domain services
    (`app.opening_round`) and #66's replay evidence source, never a second
    scoring engine. `app.replay.ReplayAflDataSource` supplies both AFL
    rounds' matches/stats from the same one evidence file, keyed by AFL
    round/match ID rather than by BBBFFL round -- exactly what lets one
    calculation mix two AFL source rounds without ambiguity."""
    fixture = FIXTURES / "opening_round_deferred_evidence.json"
    clock = ReplayClock.from_iso("2026-04-04T13:00:00Z")
    source = ReplayAflDataSource(fixture, clock=clock)

    db = migrated_connection()
    lifecycle, round_, entries = operational(db, year=2026, afl_round=1345)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(SCOPE_SQL, (round_.bbbffl_round_id,)).fetchone()
    pool, ownership = PlayerPoolRepository(db), OwnershipRepository(db)
    ownership.configure_squad_limit(scope["season_id"], 20)
    entry = entries[0]

    rules = OpeningRoundRuleRepository(db)
    rule = rules.accept(
        scope["season_id"],
        2,
        2026,
        1343,
        1345,
        round_.bbbffl_round_id,
        AflApiReferenceValidator(source),
        evidence_classification="known_fact",
        actor=OPERATOR,
        reason="checkpoint: 2026 Brisbane Lions Opening Round / R2 compensating bye (known 2026 fact)",
    )
    deferred_player = _acquire_evidence_player(pool, ownership, scope, entry, source, 7001)
    ordinary_player = _acquire_evidence_player(pool, ownership, scope, entry, source, 7002)

    nominations = OpeningRoundNominationRepository(db)
    nomination = nominations.nominate(
        rule.rule_id,
        entry.season_entry_id,
        "M1",
        deferred_player.season_player_id,
        source,
        actor=OPERATOR,
        reason="checkpoint: synthetic nomination exercising #69's mixed-source scoring",
    )

    lineups = WeeklyLineupRepository(db)
    nominations.preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    draft = lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    assert draft.positions["M1"] == deferred_player.season_player_id

    filler_team = Team(999, "Filler Bye Club")
    full = complete_lineup(
        db,
        scope,
        entry,
        overrides={**draft.positions, "M2": ordinary_player.season_player_id},
        neutral_team=filler_team,
    )
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        full,
        expected_revision=draft.revision,
    )
    guard = OpeningRoundSelectionGuard(nominations, inner=None)
    submitted = lineups.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        actor=OPERATOR,
        source_type="scorer_proxy",
        reason="checkpoint: Opening Round deferred nomination preloaded, remaining slots ordinary",
        lock_guard=guard,
    )
    assert submitted.positions["M1"] == deferred_player.season_player_id

    # The nomination remains locked according to #69's rules: an attempted
    # edit to the deferred slot is rejected, never silently overwritten.
    replacement = pool.refresh_player(
        scope["season_id"], 7003, "Attempted Replacement Player", afl_team_id=2, afl_team_name="Brisbane Lions"
    )
    ownership.acquire(
        replacement.season_player_id, entry.season_entry_id, actor=OPERATOR, reason="checkpoint squad setup"
    )
    attempted = dict(submitted.positions)
    attempted["M1"] = replacement.season_player_id
    with pytest.raises(DeferredSlotLockedError):
        lineups.submit_positions(
            draft.lineup_id,
            attempted,
            expected_submission_version=1,
            actor=OPERATOR,
            source_type="scorer_proxy",
            reason="checkpoint: attempted edit of the locked deferred slot",
            lock_guard=guard,
        )

    # The matchup's opponent entry needs a complete, ordinarily-resolvable
    # lineup too -- filled entirely from the (synthetic) filler bye club so
    # this scenario stays focused on the deferred/ordinary mix, not a
    # second unrelated evidence gap.
    focus_matchup = next(
        m
        for m in lifecycle.list_matchups(round_.bbbffl_round_id)
        if entry.season_entry_id in (m.home_season_entry_id, m.away_season_entry_id)
    )
    opponent_id = (
        focus_matchup.away_season_entry_id
        if focus_matchup.home_season_entry_id == entry.season_entry_id
        else focus_matchup.home_season_entry_id
    )
    opponent = next(e for e in entries if e.season_entry_id == opponent_id)
    submit_round(lineups, scope, round_.bbbffl_round_id, opponent, {}, neutral_team=filler_team)

    calculated = MatchupCalculationService(db, source).calculate_matchup(
        focus_matchup.matchup_id, upstream_revision=source.manifest["version"], observed_at=clock.now().isoformat()
    )
    side = (
        calculated.snapshot["home"]
        if calculated.snapshot["home"]["season_entry_id"] == entry.season_entry_id
        else calculated.snapshot["away"]
    )
    slots_by_position = {slot["position"]: slot for slot in side["slots"]}
    m1 = slots_by_position["M1"]
    m2 = slots_by_position["M2"]

    deferred_context = nominations.deferred_context(round_.bbbffl_round_id, entry.season_entry_id, "M1")
    team_name = IdentityRepository(db).get_public_team(entry.season_entry_id).team_name

    return build_checkpoint_scenario(
        "opening_round_deferred_mixed_source",
        "One BBBFFL round mixing two AFL source rounds in a single lineup: M2 scores from the current "
        "mapped AFL round (2026 R2, AFL round 1345) while M1 -- a slot previously nominated from Opening "
        "Round (AFL round 1343) for Brisbane Lions, whose compensating bye lands in this round -- scores "
        "from its own frozen Opening Round match/statistics. Proves the deferred slot never receives "
        "current-round statistics, ordinary slots are unaffected, the nomination stays locked, and "
        "diagnostics expose the mixed provenance unambiguously.",
        historical_or_synthetic=HistoricalStatus.SYNTHETIC,
        evidence_sources=[str(fixture)],
        evidence_classification=source.evidence_records(),
        clocks={"observed_at": clock.now().isoformat()},
        starting_lineup=[
            {"position": "M1", "canonical_player_id": 7001},
            {"position": "M2", "canonical_player_id": 7002},
        ],
        lineup_provenance=[
            {"team": team_name, "M1_source": "opening_round_nomination", "M2_source": "ordinary_submission"}
        ],
        afl_match_state=[
            {"afl_round_id": 1343, "afl_match_id": match.match_id, "status": match.status}
            for match in source.get_matches(1343)
        ]
        + [
            {"afl_round_id": 1345, "afl_match_id": match.match_id, "status": match.status}
            for match in source.get_matches(1345)
        ],
        deferred_source={
            "nominated_player_canonical_id": 7001,
            "bbbffl_slot": "M1",
            "nomination_provenance": {"actor_type": nomination.actor_type, "actor_role": nomination.actor_role},
            "source_afl_round_id": deferred_context["afl_opening_round_id"],
            "source_afl_match_id": deferred_context["source_afl_match_id"],
            "current_bbbffl_scoring_round": "round-2-target",
            "evidence_classification": deferred_context["evidence_classification"],
            "explanation": "Deferred Opening Round evidence supplied this slot's score; the current round's "
            "ordinary mapped AFL round was never consulted for it.",
        },
        calculated_result={
            "m1_deferred": {
                "scoring_source": m1["scoring_source"],
                "source_afl_round_id": m1["source_afl_round_id"],
                "afl_match_id": m1["afl_match_id"],
                "score": m1["score"],
                "participation_state": m1["participation"]["state"],
            },
            "m2_ordinary": {
                "scoring_source": m2["scoring_source"],
                "source_afl_round_id": m2["source_afl_round_id"],
                "afl_match_id": m2["afl_match_id"],
                "score": m2["score"],
                "participation_state": m2["participation"]["state"],
            },
        },
        expected_vs_actual={
            "expected": "M1 scores 25 (1pt/disposal) from AFL round 1343 match 9001; M2 scores 18 from AFL round 1345 match 9002",
            "actual": {
                "m1_score": m1["score"],
                "m1_source_afl_round_id": m1["source_afl_round_id"],
                "m2_score": m2["score"],
                "m2_source_afl_round_id": m2["source_afl_round_id"],
            },
        },
        discrepancies=[],
    )


def test_checkpoint_opening_round_deferred_mixed_source_scenario():
    scenario = _opening_round_deferred_scenario()
    assert scenario["outcome"] == "PASS"
    assert scenario["calculated_result"]["m1_deferred"]["scoring_source"] == "opening_round_deferred"
    assert scenario["calculated_result"]["m1_deferred"]["source_afl_round_id"] == 1343
    assert scenario["calculated_result"]["m1_deferred"]["afl_match_id"] == 9001
    assert scenario["calculated_result"]["m1_deferred"]["score"] == 25
    assert scenario["calculated_result"]["m2_ordinary"]["scoring_source"] == "ordinary"
    assert scenario["calculated_result"]["m2_ordinary"]["source_afl_round_id"] == 1345
    assert scenario["calculated_result"]["m2_ordinary"]["afl_match_id"] == 9002
    assert scenario["calculated_result"]["m2_ordinary"]["score"] == 18
    assert scenario["deferred_source"]["source_afl_round_id"] == 1343
    assert scenario["deferred_source"]["source_afl_match_id"] == 9001


def test_checkpoint_opening_round_deferred_mixed_source_scenario_is_deterministic_on_rerun():
    first = _opening_round_deferred_scenario()
    second = _opening_round_deferred_scenario()
    assert first == second


def _full_suite():
    """Issue #67's whole checkpoint suite: every required scenario built
    from a clean database/evidence/clock, assembled and validated by
    `app.replay_checkpoint.build_checkpoint_suite` -- the DoD's "deterministic
    golden checkpoint covering" all four required cases in one report."""
    early_lockout = _early_lockout_scenario()
    bye_availability, _ = _bye_availability_scenario()
    round1_unresolved = _round1_no_prior_lineup_scenario()
    carry_forward_and_proxy = _carry_forward_and_proxy_scenario()
    opening_round_deferred = _opening_round_deferred_scenario()
    return build_checkpoint_suite(
        "2026-checkpoint-rounds-1-9-suite",
        [early_lockout, bye_availability, round1_unresolved, carry_forward_and_proxy, opening_round_deferred],
    )


def test_checkpoint_suite_aggregates_every_required_scenario(tmp_path):
    suite = _full_suite()
    ids = {scenario["scenario_id"] for scenario in suite["scenarios"]}
    assert ids == {
        "early_lockout",
        "bye_availability",
        "round1_no_prior_lineup",
        "carry_forward_and_proxy_provenance",
        "opening_round_deferred_mixed_source",
    }
    # An unresolved scorer question (Round 1's no-prior-lineup case) must
    # never read as a silent pass at the whole-suite level either.
    assert suite["outcome_counts"]["UNRESOLVED"] == 1
    assert suite["outcome_counts"]["FAIL"] == 0
    assert suite["outcome_counts"]["PASS"] == 4
    assert suite["suite_resolved"] is False

    json_path, summary_path = tmp_path / "checkpoint.json", tmp_path / "checkpoint.txt"
    write_checkpoint_suite_report(suite, json_path, summary_path)
    written = json.loads(json_path.read_text())
    assert written == suite
    summary_text = summary_path.read_text()
    assert "UNRESOLVED" in summary_text
    assert "round1_no_prior_lineup" in summary_text
    assert "Suite resolved (no FAIL/UNRESOLVED): False" in summary_text


def test_checkpoint_suite_is_deterministic_on_full_clean_rerun():
    """Issue #67 requirement 7: a complete rerun of the entire checkpoint
    suite from clean state (fresh database, fresh evidence load, fresh
    clock) produces an equivalent report -- lineups, provenance, lock
    behaviour, warnings, scores, sign-off state, ladder outcome, and
    diagnostics all included, since they are exactly what each scenario's
    report already carries."""
    first = _full_suite()
    second = _full_suite()
    assert first == second
