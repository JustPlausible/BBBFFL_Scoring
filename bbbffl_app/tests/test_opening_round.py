"""Opening Round deferred-selection and compensating-bye scoring (issue
#69). See docs/opening-round-deferred-selection.md for the design, and
tests/opening_round_evidence.py for the AFL-side facts these tests are
built from (transcribed from docs/evidence/opening-round/*.json, the
attached Bruno/CFS captures).

Every nomination built here is an explicitly **synthetic test scenario**
(app.opening_round.EVIDENCE_CLASSIFICATIONS): no historical BBBFFL
nomination record exists in this repository, only the AFL-side facts the
supplied captures establish (see the evidence classification note in each
test below and docs/opening-round-deferred-selection.md's evidence-boundary
section).
"""

import json
from pathlib import Path

import pytest

from app.afl_client import Match, PlayerStatLine, Team
from app.audit import ActorContext, AuditEventRepository
from app.calculations import MatchupCalculationService
from app.carry_forward import CarryForwardService
from app.lineups import WeeklyLineupRepository
from app.opening_round import (
    DeferredSlotLockedError,
    IneligiblePlayerError,
    OpeningRoundError,
    OpeningRoundNominationRepository,
    OpeningRoundRuleRepository,
    OpeningRoundSelectionGuard,
    UnauthorizedNominationActorError,
    UnknownRuleError,
)
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests import opening_round_evidence as evidence
from tests.db_helpers import migrated_connection
from tests.lineup_helpers import complete_lineup
from tests.test_competition_lifecycle import operational

ADMIN = ActorContext.anonymous_operator("admin")
SCORER = ActorContext.anonymous_operator("scorer")
COACH = ActorContext.anonymous_operator("coach")  # not a valid nomination actor -- see tests below


class KnownRounds:
    """`app.round_mapping.AflReferenceValidator` accepting any of a fixed
    set of (afl_season_id, afl_round_id) pairs -- unlike
    `tests.test_competition_lifecycle.KnownRound`, which only accepts one."""

    def __init__(self, *pairs):
        self.pairs = set(pairs)

    def round_exists(self, season, round_):
        return (season, round_) in self.pairs


class MultiRoundMatchClient:
    """Duck-typed AFL client returning a different match list per AFL round
    ID -- needed once a lineup can draw stats from more than one AFL round
    in the same calculation (an ordinary round and a deferred slot's
    Opening Round)."""

    def __init__(self, matches_by_round, stats_by_match=None):
        self.matches_by_round = matches_by_round
        self.stats_by_match = stats_by_match or {}
        self.requested_rounds = []

    def get_matches(self, round_id):
        self.requested_rounds.append(round_id)
        return self.matches_by_round.get(round_id, [])

    def get_match_player_stats(self, match_id):
        return self.stats_by_match.get(match_id, {})


def setup_scope(db, year, target_afl_round):
    """A season/competition/round pair whose accepted round mapping targets
    `target_afl_round` -- the compensating-bye round a deferred slot's
    ordinary sibling positions score from."""
    lifecycle, round_, entries = operational(db, year, target_afl_round)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(
        "SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    OwnershipRepository(db).configure_squad_limit(scope["season_id"], 30)
    return lifecycle, round_, entries, dict(scope)


def accept_rule(db, season_id, afl_club_id, ev: evidence.SeasonEvidence, afl_bye_round_id, bbbffl_round_id, **kwargs):
    validator = KnownRounds((ev.afl_season_id, ev.afl_opening_round_id), (ev.afl_season_id, afl_bye_round_id))
    return OpeningRoundRuleRepository(db).accept(
        season_id,
        afl_club_id,
        ev.afl_season_id,
        ev.afl_opening_round_id,
        afl_bye_round_id,
        bbbffl_round_id,
        validator,
        actor=ADMIN,
        reason="synthetic test scenario built from tests/opening_round_evidence.py",
        **kwargs,
    )


def own_player(db, season_id, entry, canonical_id, name, afl_team_id):
    player = PlayerPoolRepository(db).refresh_player(season_id, canonical_id, name, afl_team_id=afl_team_id)
    OwnershipRepository(db).acquire(player.season_player_id, entry.season_entry_id)
    return player


def nominate_bl_2024(db, season_id, round_id, entry, position="M1", canonical_id=910001):
    """A representative, explicitly synthetic 2024 nomination: Brisbane
    Lions (afl_club_id=2) played Opening Round (AFL round 954) and had its
    compensating bye in AFL round 956 -- known facts from
    tests/opening_round_evidence.EVIDENCE_2024. The nominating coach/slot
    combination is synthetic: no historical BBBFFL record of this specific
    nomination exists in this repository."""
    ev = evidence.EVIDENCE_2024
    rule = accept_rule(db, season_id, 2, ev, ev.compensating_bye_round["BL"], round_id)
    player = own_player(db, season_id, entry, canonical_id, "Synthetic BL Opening Round Player", afl_team_id=2)
    or_client = MultiRoundMatchClient(
        {
            ev.afl_opening_round_id: [
                Match(match_id=8001, home_team=Team(2, "BL"), away_team=Team(5, "CARL"), status="CONCLUDED")
            ]
        }
    )
    nomination = OpeningRoundNominationRepository(db).nominate(
        rule.rule_id,
        entry.season_entry_id,
        position,
        player.season_player_id,
        or_client,
        actor=SCORER,
        reason="synthetic nomination for issue #69 coverage",
    )
    return rule, player, nomination


# -- Evidence integrity -------------------------------------------------


def test_evidence_constants_match_captured_source_files():
    """Cross-check the transcribed facts in tests/opening_round_evidence.py
    against the raw captures they claim to summarise, so a future edit to
    either cannot silently drift from the other."""
    repo_root = Path(__file__).resolve().parents[2]
    for ev in evidence.ALL_SEASONS:
        with open(repo_root / ev.source_file) as handle:
            rounds = json.load(handle)["rounds"]
        opening = next(r for r in rounds if r["roundNumber"] == 0)
        assert opening["id"] == ev.afl_opening_round_id
        assert opening["name"] == "Opening Round"
        bye_ids = {team["id"] for team in opening["byes"]}
        all_club_ids = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 1}
        participants = all_club_ids - bye_ids
        assert participants == set(ev.participating_clubs.values())
        for club, expected_round in ev.compensating_bye_round.items():
            club_id = ev.participating_clubs[club]
            actual_round = next(r for r in rounds if club_id in {team["id"] for team in r["byes"]})
            assert actual_round["id"] == expected_round, (ev.year, club)


# -- 1/2: explicit season configuration, unconfigured season unchanged ---


def test_unconfigured_season_has_no_active_rule_and_nomination_is_refused():
    db = migrated_connection()
    lifecycle, round_, entries, scope = setup_scope(db, 2027, 500)
    rules = OpeningRoundRuleRepository(db)
    assert rules.resolve(scope["season_id"], 2) is None

    fake_rule_id = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(UnknownRuleError):
        OpeningRoundNominationRepository(db).nominate(
            fake_rule_id, entries[0].season_entry_id, "M1", "irrelevant", MultiRoundMatchClient({}), actor=SCORER
        )


def test_proposal_alone_does_not_activate_the_capability():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2027, 500)
    rules = OpeningRoundRuleRepository(db)
    ev = evidence.EVIDENCE_2024
    proposed = rules.propose(
        scope["season_id"],
        2,
        state="unresolved",
        afl_season_id=ev.afl_season_id,
        afl_opening_round_id=ev.afl_opening_round_id,
        reason="not yet confirmed",
    )
    assert proposed.state == "unresolved"
    assert rules.resolve(scope["season_id"], 2) is None


def test_explicit_acceptance_enables_the_capability():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    resolved = OpeningRoundRuleRepository(db).resolve(scope["season_id"], 2)
    assert resolved is not None and resolved.state == "accepted"
    assert nomination.season_player_id == player.season_player_id


# -- 3/7: 2024 structure, including later R5/R6 compensating byes --------


def test_2024_structure_representable_including_bl_carl_r2():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    assert rule.afl_bye_round_id == 956
    assert nomination.bbbffl_round_id == round_.bbbffl_round_id


def test_2024_later_r5_r6_compensating_byes_are_representable():
    """The supplied 2024 evidence disproves any hard-coded R2-R4 assumption:
    Collingwood/Sydney's compensating bye is AFL round 959 (R5) and
    Melbourne/Richmond's is AFL round 960 (R6)."""
    db = migrated_connection()
    ev = evidence.EVIDENCE_2024
    _, round5, entries, scope = setup_scope(db, 2024, ev.compensating_bye_round["COLL"])
    rule = accept_rule(db, scope["season_id"], 3, ev, ev.compensating_bye_round["COLL"], round5.bbbffl_round_id)
    assert rule.afl_bye_round_id == 959

    db6 = migrated_connection()
    _, round6, entries6, scope6 = setup_scope(db6, 2024, ev.compensating_bye_round["MELB"])
    rule6 = accept_rule(db6, scope6["season_id"], 17, ev, ev.compensating_bye_round["MELB"], round6.bbbffl_round_id)
    assert rule6.afl_bye_round_id == 960


# -- 4: 2025 smaller structure is independently configurable -------------


def test_2025_structure_independently_configurable():
    db = migrated_connection()
    ev = evidence.EVIDENCE_2025
    _, round_, entries, scope = setup_scope(db, 2025, ev.compensating_bye_round["GWS"])
    rule = accept_rule(
        db,
        scope["season_id"],
        ev.participating_clubs["GWS"],
        ev,
        ev.compensating_bye_round["GWS"],
        round_.bbbffl_round_id,
    )
    assert rule.afl_opening_round_id == 1146
    assert rule.afl_bye_round_id == 1148
    # Only 4 clubs participated in 2025 -- a club absent from that list
    # (e.g. Carlton, id 5) has no accepted rule to nominate under.
    assert "CARL" not in ev.participating_clubs
    assert OpeningRoundRuleRepository(db).resolve(scope["season_id"], 5) is None


# -- 5/6: 2026 structure, and mappings are not assumed identical ---------


def test_2026_structure_r2_r3_r4_compensating_byes():
    db = migrated_connection()
    ev = evidence.EVIDENCE_2026
    _, round2, entries, scope = setup_scope(db, 2026, ev.compensating_bye_round["BL"])
    rule_bl = accept_rule(
        db,
        scope["season_id"],
        ev.participating_clubs["BL"],
        ev,
        ev.compensating_bye_round["BL"],
        round2.bbbffl_round_id,
    )
    assert rule_bl.afl_bye_round_id == 1345

    db2 = migrated_connection()
    _, round3, entries2, scope2 = setup_scope(db2, 2026, ev.compensating_bye_round["GCFC"])
    rule_gcfc = accept_rule(
        db2,
        scope2["season_id"],
        ev.participating_clubs["GCFC"],
        ev,
        ev.compensating_bye_round["GCFC"],
        round3.bbbffl_round_id,
    )
    assert rule_gcfc.afl_bye_round_id == 1346

    db3 = migrated_connection()
    _, round4, entries3, scope3 = setup_scope(db3, 2026, ev.compensating_bye_round["GWS"])
    rule_gws = accept_rule(
        db3,
        scope3["season_id"],
        ev.participating_clubs["GWS"],
        ev,
        ev.compensating_bye_round["GWS"],
        round4.bbbffl_round_id,
    )
    assert rule_gws.afl_bye_round_id == 1347


def test_mappings_are_not_assumed_identical_across_seasons():
    """Brisbane Lions' compensating bye round differs by season (2024: AFL
    round 956; 2026: AFL round 1345) -- the rule is season-scoped, never a
    global `club -> round` constant."""
    db24 = migrated_connection()
    ev24 = evidence.EVIDENCE_2024
    _, round24, entries24, scope24 = setup_scope(db24, 2024, ev24.compensating_bye_round["BL"])
    rule24 = accept_rule(
        db24, scope24["season_id"], 2, ev24, ev24.compensating_bye_round["BL"], round24.bbbffl_round_id
    )

    db26 = migrated_connection()
    ev26 = evidence.EVIDENCE_2026
    _, round26, entries26, scope26 = setup_scope(db26, 2026, ev26.compensating_bye_round["BL"])
    rule26 = accept_rule(
        db26, scope26["season_id"], 2, ev26, ev26.compensating_bye_round["BL"], round26.bbbffl_round_id
    )

    assert rule24.afl_bye_round_id != rule26.afl_bye_round_id
    assert (rule24.afl_opening_round_id, rule24.afl_bye_round_id) == (954, 956)
    assert (rule26.afl_opening_round_id, rule26.afl_bye_round_id) == (1343, 1345)


# -- 8/9/10: nomination workflow ------------------------------------------


def test_eligible_owned_player_can_be_nominated():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    assert nomination.season_player_id == player.season_player_id
    assert nomination.source_afl_match_id == 8001


def test_nomination_persists_player_target_round_slot_and_source_context():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, entries[0], position="Ruck"
    )
    fetched = OpeningRoundNominationRepository(db).get(nomination.nomination_id)
    assert fetched.position == "Ruck"
    assert fetched.season_player_id == player.season_player_id
    assert fetched.bbbffl_round_id == round_.bbbffl_round_id
    assert fetched.source_afl_match_id == 8001
    assert fetched.actor_type == "anonymous_operator" and fetched.actor_role == "scorer"


def test_unowned_player_is_rejected():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    ev = evidence.EVIDENCE_2024
    rule = accept_rule(db, scope["season_id"], 2, ev, ev.compensating_bye_round["BL"], round_.bbbffl_round_id)
    # Owned by no one.
    unowned = PlayerPoolRepository(db).refresh_player(scope["season_id"], 910099, "Unowned BL Player", afl_team_id=2)
    or_client = MultiRoundMatchClient({954: [Match(8001, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED")]})
    with pytest.raises(IneligiblePlayerError):
        OpeningRoundNominationRepository(db).nominate(
            rule.rule_id, entries[0].season_entry_id, "M1", unowned.season_player_id, or_client, actor=SCORER
        )


def test_player_from_a_different_club_is_rejected():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    ev = evidence.EVIDENCE_2024
    rule = accept_rule(
        db, scope["season_id"], 2, ev, ev.compensating_bye_round["BL"], round_.bbbffl_round_id
    )  # BL rule
    wrong_club_player = own_player(db, scope["season_id"], entries[0], 910098, "Carlton Player", afl_team_id=5)
    or_client = MultiRoundMatchClient({954: [Match(8001, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED")]})
    with pytest.raises(IneligiblePlayerError):
        OpeningRoundNominationRepository(db).nominate(
            rule.rule_id, entries[0].season_entry_id, "M1", wrong_club_player.season_player_id, or_client, actor=SCORER
        )


def test_nomination_requires_an_operator_actor_never_the_coach():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    ev = evidence.EVIDENCE_2024
    rule = accept_rule(db, scope["season_id"], 2, ev, ev.compensating_bye_round["BL"], round_.bbbffl_round_id)
    player = own_player(db, scope["season_id"], entries[0], 910097, "BL Player", afl_team_id=2)
    or_client = MultiRoundMatchClient({954: [Match(8001, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED")]})
    with pytest.raises(UnauthorizedNominationActorError):
        OpeningRoundNominationRepository(db).nominate(
            rule.rule_id, entries[0].season_entry_id, "M1", player.season_player_id, or_client, actor=COACH
        )


# -- 11/12: locked future position -----------------------------------------


def test_future_slot_is_preloaded_into_the_target_round_draft():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    lineups = WeeklyLineupRepository(db)
    OpeningRoundNominationRepository(db).preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )
    draft = lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )
    assert draft.positions["M1"] == player.season_player_id
    # Idempotent: calling again does not raise or change anything.
    OpeningRoundNominationRepository(db).preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )


def test_ordinary_edit_cannot_replace_or_reposition_the_deferred_player():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    lineups = WeeklyLineupRepository(db)
    nominations = OpeningRoundNominationRepository(db)
    nominations.preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )
    draft = lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )
    full = complete_lineup(db, scope, entries[0], overrides=draft.positions)

    guard = OpeningRoundSelectionGuard(nominations)
    with pytest.raises(DeferredSlotLockedError):
        lineups.submit_positions(
            draft.lineup_id,
            {**full, "M1": full["F1"]},
            expected_submission_version=0,
            actor=SCORER,
            source_type="scorer_proxy",
            reason="attempt to move deferred player",
            lock_guard=guard,
        )
    # Repositioning attempted -- the deferred slot is unaffected because the
    # submission was rejected outright rather than partially applied.
    assert lineups.get_effective_submission(draft.lineup_id) is None

    submitted = lineups.submit_positions(
        draft.lineup_id,
        full,
        expected_submission_version=0,
        actor=SCORER,
        source_type="scorer_proxy",
        reason="legitimate submission keeping deferred slot intact",
        lock_guard=guard,
    )
    assert submitted.positions["M1"] == player.season_player_id


# -- 13: carry-forward cannot overwrite a deferred slot --------------------


def test_carry_forward_cannot_overwrite_a_deferred_slot():
    db = migrated_connection()
    _, round1, entries, scope = setup_scope(db, 2024, 900)
    from app.round_mapping import RoundMappingRepository
    from app.season import SeasonRepository

    seasons = SeasonRepository(db)
    round2 = seasons.create_round(scope["competition_id"], "round-2", "Round 2 (BL/CARL bye)", 2)
    RoundMappingRepository(db).accept(round2.bbbffl_round_id, 2024, 956, KnownRounds((2024, 956)))
    from app.competition_lifecycle import CompetitionLifecycleRepository

    lifecycle = CompetitionLifecycleRepository(db)
    lifecycle.create_ordinary_round(round2.bbbffl_round_id)
    lifecycle.transition(round2.bbbffl_round_id, "open")

    entry = entries[0]
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round2.bbbffl_round_id, entry)

    lineups = WeeklyLineupRepository(db)
    nominations = OpeningRoundNominationRepository(db)
    # Round 1's own submission has a *different* player in M1 -- an ordinary
    # verbatim carry-forward would try to copy that into round 2's M1,
    # which the nomination has already locked to `player`.
    round1_positions = complete_lineup(db, scope, entry)
    draft1 = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round1.bbbffl_round_id,
        entry.season_entry_id,
        round1_positions,
        expected_revision=0,
    )
    lineups.submit(draft1.lineup_id, expected_draft_revision=draft1.revision, expected_submission_version=0)

    nominations.preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round2.bbbffl_round_id, entry.season_entry_id
    )
    guard = OpeningRoundSelectionGuard(nominations)
    carry_forward = CarryForwardService(db)
    with pytest.raises(DeferredSlotLockedError):
        carry_forward.carry_forward(
            scope["season_id"],
            scope["competition_id"],
            round2.bbbffl_round_id,
            entry.season_entry_id,
            expected_submission_version=0,
            actor=ActorContext.system(),
            reason="ordinary carry-forward",
            lock_guard=guard,
        )


# -- 14/15/18: coexistence with ordinary staged lockout & mixed sources ---


def test_deferred_lock_and_ordinary_staged_lockout_coexist_in_one_lineup():
    from datetime import datetime, timezone

    from app.lockouts import LockoutRepository, LockoutTriggerRepository

    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    entry = entries[0]
    rule, deferred_player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, entry, position="M1"
    )

    ordinary_match = Match(
        match_id=7001,
        home_team=Team(100, "Home"),
        away_team=Team(101, "Away"),
        status="UPCOMING",
        start_time_utc="2024-03-21T09:00:00+00:00",
    )
    ordinary_player = own_player(db, scope["season_id"], entry, 910200, "Ordinary F1 Player", afl_team_id=100)

    lineups = WeeklyLineupRepository(db)
    nominations = OpeningRoundNominationRepository(db)
    nominations.preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    draft = lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    full = complete_lineup(db, scope, entry, overrides={**draft.positions, "F1": ordinary_player.season_player_id})

    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "main", "main", 1, [7001], actor=ADMIN, reason="round main lockout")

    class Facts:
        def matches_for(self, bbbffl_round_id):
            return [ordinary_match]

    lockout_repo = LockoutRepository(db)
    inner_guard = lockout_repo.guard(match_facts=Facts(), evaluation_at=datetime(2024, 3, 20, tzinfo=timezone.utc))
    combined_guard = OpeningRoundSelectionGuard(nominations, inner=inner_guard)

    submitted = lineups.submit_positions(
        draft.lineup_id,
        full,
        expected_submission_version=0,
        actor=SCORER,
        source_type="scorer_proxy",
        reason="initial submission before lockout",
        lock_guard=combined_guard,
    )
    assert submitted.positions["M1"] == deferred_player.season_player_id
    assert submitted.positions["F1"] == ordinary_player.season_player_id

    # Main trigger activates once the match reaches its scheduled start.
    late_guard = OpeningRoundSelectionGuard(
        nominations,
        inner=lockout_repo.guard(match_facts=Facts(), evaluation_at=datetime(2024, 3, 21, 10, tzinfo=timezone.utc)),
    )
    other_player = own_player(db, scope["season_id"], entry, 910201, "Replacement F1", afl_team_id=100)
    attempted = dict(full)
    attempted["F1"] = other_player.season_player_id
    with pytest.raises(Exception):
        lineups.submit_positions(
            draft.lineup_id,
            attempted,
            expected_submission_version=1,
            actor=SCORER,
            source_type="scorer_proxy",
            reason="attempt to change F1 after ordinary lockout",
            lock_guard=late_guard,
        )
    # The deferred slot is unaffected by, and does not need, the ordinary
    # lockout's own match resolution (its club has no match in this round).
    attempted_deferred_change = dict(full)
    attempted_deferred_change["M1"] = other_player.season_player_id
    with pytest.raises(DeferredSlotLockedError):
        lineups.submit_positions(
            draft.lineup_id,
            attempted_deferred_change,
            expected_submission_version=1,
            actor=SCORER,
            source_type="scorer_proxy",
            reason="attempt to change deferred slot",
            lock_guard=late_guard,
        )


def test_mixed_source_lineup_uses_canonical_scoring_for_both_sources():
    db = migrated_connection()
    lifecycle, round_, entries, scope = setup_scope(db, 2024, 956)
    home, away = entries[0], entries[1]
    rule, deferred_player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, home, position="M1"
    )

    ordinary_match = Match(match_id=7001, home_team=Team(100, "Home"), away_team=Team(101, "Away"), status="CONCLUDED")
    opening_match = Match(match_id=8001, home_team=Team(2, "BL"), away_team=Team(5, "CARL"), status="CONCLUDED")
    ordinary_player = own_player(db, scope["season_id"], home, 910300, "Ordinary M-slot player", afl_team_id=100)

    lineups = WeeklyLineupRepository(db)
    nominations = OpeningRoundNominationRepository(db)
    nominations.preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, home.season_entry_id
    )
    draft = lineups.get_draft(scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, home.season_entry_id)
    home_positions = complete_lineup(
        db, scope, home, overrides={**draft.positions, "M2": ordinary_player.season_player_id}
    )
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        home.season_entry_id,
        home_positions,
        expected_revision=draft.revision,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)

    away_positions = complete_lineup(db, scope, away)
    away_draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        away.season_entry_id,
        away_positions,
        expected_revision=0,
    )
    lineups.submit(away_draft.lineup_id, expected_draft_revision=away_draft.revision, expected_submission_version=0)

    matchup = next(
        m
        for m in lifecycle.list_matchups(round_.bbbffl_round_id)
        if home.season_entry_id in (m.home_season_entry_id, m.away_season_entry_id)
    )
    client = MultiRoundMatchClient(
        {956: [ordinary_match], 954: [opening_match]},
        stats_by_match={
            7001: {910300: PlayerStatLine(910300, disposals=20)},
            8001: {910001: PlayerStatLine(910001, disposals=30)},
        },
    )
    service = MatchupCalculationService(db, client)
    calculated = service.calculate_matchup(matchup.matchup_id)
    home_side = (
        calculated.snapshot["home"]
        if calculated.snapshot["home"]["season_entry_id"] == home.season_entry_id
        else calculated.snapshot["away"]
    )
    slots_by_position = {slot["position"]: slot for slot in home_side["slots"]}

    m1 = slots_by_position["M1"]
    m2 = slots_by_position["M2"]
    assert m1["scoring_source"] == "opening_round_deferred"
    assert m1["source_afl_round_id"] == 954
    assert m1["afl_match_id"] == 8001
    assert m1["score"] == 30  # 1 point per disposal, same canonical formula
    assert m1["participation"]["state"] == "played_with_stats"
    assert m1["participation"]["dnp_recommendation"] == "not_dnp"

    assert m2["scoring_source"] == "ordinary"
    assert m2["source_afl_round_id"] == 956
    assert m2["afl_match_id"] == 7001
    assert m2["score"] == 20


def test_calculation_flags_a_slot_whose_submitted_player_no_longer_matches_its_nomination():
    """A nomination names a position and a player; if the *actually
    submitted* player in that position ever diverges from the nominated
    one (e.g. the guard was bypassed, or the nomination was later
    corrected), `_entry()` must never silently score the wrong player from
    Opening Round facts, nor silently fall back to treating them as an
    ordinary selection -- it must flag the slot for scorer review."""
    db = migrated_connection()
    lifecycle, round_, entries, scope = setup_scope(db, 2024, 956)
    home, away = entries[0], entries[1]
    rule, deferred_player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, home, position="M1"
    )
    other_player = own_player(db, scope["season_id"], home, 910310, "Not The Nominated Player", afl_team_id=2)

    lineups = WeeklyLineupRepository(db)
    # Bypass OpeningRoundSelectionGuard entirely (system_derived, no
    # lock_guard) to construct the divergence directly.
    home_positions = complete_lineup(db, scope, home, overrides={"M1": other_player.season_player_id})
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        home.season_entry_id,
        home_positions,
        expected_revision=0,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)
    away_draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        away.season_entry_id,
        complete_lineup(db, scope, away),
        expected_revision=0,
    )
    lineups.submit(away_draft.lineup_id, expected_draft_revision=away_draft.revision, expected_submission_version=0)

    matchup = next(
        m
        for m in lifecycle.list_matchups(round_.bbbffl_round_id)
        if home.season_entry_id in (m.home_season_entry_id, m.away_season_entry_id)
    )
    client = MultiRoundMatchClient(
        {
            956: [Match(7001, Team(2, "BL"), Team(101, "Away"), "CONCLUDED")],
            954: [Match(8001, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED")],
        },
        stats_by_match={7001: {910310: PlayerStatLine(910310, disposals=99)}},
    )
    service = MatchupCalculationService(db, client)
    calculated = service.calculate_matchup(matchup.matchup_id)
    home_side = (
        calculated.snapshot["home"]
        if calculated.snapshot["home"]["season_entry_id"] == home.season_entry_id
        else calculated.snapshot["away"]
    )
    m1 = next(slot for slot in home_side["slots"] if slot["position"] == "M1")
    assert m1["scoring_source"] == "opening_round_nomination_mismatch"
    assert m1["score"] == 0
    assert m1["participation"]["state"] == "unknown"
    assert m1["participation"]["dnp_recommendation"] == "review_required"
    assert "different player" in m1["participation"]["reason"]


# -- 20: ordinary bye/DNP unaffected for non-deferred players --------------


def test_ordinary_bye_behaviour_is_unchanged_for_a_non_deferred_player():
    from app.lineup_validation import LineupValidationService

    class ByeClient:
        def get_rounds(self, afl_season_id):
            class R:
                round_id = 956
                byes = (Team(7, "Port Adelaide"),)

            return [R()]

    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    entry = entries[0]
    rule, deferred_player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, entry, position="M1"
    )
    bye_player = own_player(db, scope["season_id"], entry, 910400, "Ordinary Bye Player", afl_team_id=7)

    lineups = WeeklyLineupRepository(db)
    nominations = OpeningRoundNominationRepository(db)
    nominations.preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    draft = lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    positions = complete_lineup(db, scope, entry, overrides={**draft.positions, "F2": bye_player.season_player_id})

    service = LineupValidationService(db, ByeClient())
    result = service.validate_submission(draft.lineup_id, positions)
    codes_by_position = {(m.position, m.code) for m in result.messages}
    assert ("F2", "afl_club_bye") in codes_by_position
    assert ("M1", "afl_club_bye") not in codes_by_position
    assert ("M1", "deferred_selection_active") in codes_by_position


# -- 21/22: missing/invalid evidence fails explicitly -----------------------


def test_missing_source_evidence_at_scoring_time_fails_explicitly_not_silently():
    db = migrated_connection()
    lifecycle, round_, entries, scope = setup_scope(db, 2024, 956)
    home, away = entries[0], entries[1]
    rule, deferred_player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, home, position="M1"
    )

    lineups = WeeklyLineupRepository(db)
    nominations = OpeningRoundNominationRepository(db)
    nominations.preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, home.season_entry_id
    )
    draft = lineups.get_draft(scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, home.season_entry_id)
    home_positions = complete_lineup(db, scope, home, overrides=draft.positions)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        home.season_entry_id,
        home_positions,
        expected_revision=draft.revision,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)
    away_draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        away.season_entry_id,
        complete_lineup(db, scope, away),
        expected_revision=0,
    )
    lineups.submit(away_draft.lineup_id, expected_draft_revision=away_draft.revision, expected_submission_version=0)

    matchup = next(
        m
        for m in lifecycle.list_matchups(round_.bbbffl_round_id)
        if home.season_entry_id in (m.home_season_entry_id, m.away_season_entry_id)
    )
    # The Opening Round match list this time does not include the deferred
    # club at all -- evidence has gone missing/unresolved since nomination.
    client = MultiRoundMatchClient({956: [], 954: []})
    service = MatchupCalculationService(db, client)
    calculated = service.calculate_matchup(matchup.matchup_id)
    home_side = (
        calculated.snapshot["home"]
        if calculated.snapshot["home"]["season_entry_id"] == home.season_entry_id
        else calculated.snapshot["away"]
    )
    m1 = next(slot for slot in home_side["slots"] if slot["position"] == "M1")
    assert m1["participation"]["state"] == "unknown"
    assert m1["participation"]["dnp_recommendation"] == "review_required"
    assert "Opening Round deferred nomination" in m1["participation"]["reason"]
    assert m1["score"] == 0  # never guessed non-zero, never silently dropped


def test_nomination_under_an_unaccepted_rule_fails_explicitly():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    ev = evidence.EVIDENCE_2024
    proposed = OpeningRoundRuleRepository(db).propose(
        scope["season_id"],
        2,
        state="ambiguous",
        afl_season_id=ev.afl_season_id,
        afl_opening_round_id=ev.afl_opening_round_id,
        reason="not yet confirmed",
    )
    player = own_player(db, scope["season_id"], entries[0], 910500, "BL Player", afl_team_id=2)
    with pytest.raises(UnknownRuleError):
        OpeningRoundNominationRepository(db).nominate(
            proposed.rule_id,
            entries[0].season_entry_id,
            "M1",
            player.season_player_id,
            MultiRoundMatchClient({}),
            actor=SCORER,
        )


# -- 23/24: correction/audit and evidence classification -------------------


def test_correction_preserves_original_state_actor_reason_in_audit_history():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, entries[0], position="M1"
    )
    other_player = own_player(db, scope["season_id"], entries[0], 910600, "Corrected BL Player", afl_team_id=2)

    nominations = OpeningRoundNominationRepository(db)
    corrected = nominations.correct(
        nomination.nomination_id,
        season_player_id=other_player.season_player_id,
        actor=ADMIN,
        reason="original nomination was a data-entry error",
    )
    assert corrected.season_player_id == other_player.season_player_id

    events = AuditEventRepository(db).list_events(entity_id=nomination.nomination_id)
    correction_events = [e for e in events if e.action == "opening_round.nomination.corrected"]
    assert len(correction_events) == 1
    event = correction_events[0]
    assert event.before_state == {"position": "M1", "season_player_id": player.season_player_id}
    assert event.after_state == {"position": "M1", "season_player_id": other_player.season_player_id}
    assert event.reason == "original nomination was a data-entry error"
    assert event.actor_role == "admin"


def test_correction_requires_a_reason_and_an_operator_actor():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    nominations = OpeningRoundNominationRepository(db)
    with pytest.raises(OpeningRoundError):
        nominations.correct(nomination.nomination_id, position="Ruck", actor=ADMIN, reason="")
    with pytest.raises(UnauthorizedNominationActorError):
        nominations.correct(nomination.nomination_id, position="Ruck", actor=COACH, reason="not an operator")


def test_correction_revalidates_an_unowned_wrong_club_or_wrong_season_replacement_player():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    nominations = OpeningRoundNominationRepository(db)

    unowned = PlayerPoolRepository(db).refresh_player(scope["season_id"], 910800, "Unowned BL Player", afl_team_id=2)
    with pytest.raises(IneligiblePlayerError):
        nominations.correct(
            nomination.nomination_id,
            season_player_id=unowned.season_player_id,
            actor=ADMIN,
            reason="swap in an unowned player",
        )

    wrong_club = own_player(db, scope["season_id"], entries[0], 910801, "Carlton Player", afl_team_id=5)
    with pytest.raises(IneligiblePlayerError):
        nominations.correct(
            nomination.nomination_id,
            season_player_id=wrong_club.season_player_id,
            actor=ADMIN,
            reason="swap in a wrong-club player",
        )

    # A legitimate replacement (same club, owned by the same entry) still succeeds.
    other_bl_player = own_player(db, scope["season_id"], entries[0], 910802, "Second BL Player", afl_team_id=2)
    corrected = nominations.correct(
        nomination.nomination_id,
        season_player_id=other_bl_player.season_player_id,
        actor=ADMIN,
        reason="legitimate swap",
    )
    assert corrected.season_player_id == other_bl_player.season_player_id


def test_rule_correction_is_refused_while_nominations_exist():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    ev = evidence.EVIDENCE_2024
    validator = KnownRounds(
        (ev.afl_season_id, ev.afl_opening_round_id), (ev.afl_season_id, ev.compensating_bye_round["CARL"])
    )
    with pytest.raises(OpeningRoundError):
        OpeningRoundRuleRepository(db).correct(
            scope["season_id"],
            2,
            ev.afl_season_id,
            ev.afl_opening_round_id,
            ev.compensating_bye_round["CARL"],
            round_.bbbffl_round_id,
            validator,
            actor=ADMIN,
            reason="attempt to move the target round",
        )


def test_evidence_classification_is_inspectable_and_validated():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, entries[0], position="M1"
    )
    assert rule.evidence_classification == "known_fact"

    context = OpeningRoundNominationRepository(db).deferred_context(
        round_.bbbffl_round_id, entries[0].season_entry_id, "M1"
    )
    assert context["evidence_classification"] == "known_fact"
    assert context["afl_opening_round_id"] == 954
    assert context["afl_bye_round_id"] == 956
    assert context["nomination_id"] == nomination.nomination_id

    ev = evidence.EVIDENCE_2024
    with pytest.raises(ValueError):
        accept_rule(
            db,
            scope["season_id"],
            3,
            ev,
            ev.compensating_bye_round["COLL"],
            round_.bbbffl_round_id,
            evidence_classification="not_a_real_classification",
        )


# -- Duplicate/uniqueness invariants ---------------------------------------


def test_duplicate_slot_and_duplicate_player_nominations_are_rejected():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    ev = evidence.EVIDENCE_2024
    rule = accept_rule(db, scope["season_id"], 2, ev, ev.compensating_bye_round["BL"], round_.bbbffl_round_id)
    rule_carl = accept_rule(db, scope["season_id"], 5, ev, ev.compensating_bye_round["CARL"], round_.bbbffl_round_id)
    entry = entries[0]
    player = own_player(db, scope["season_id"], entry, 910700, "BL Player", afl_team_id=2)
    other_bl_player = own_player(db, scope["season_id"], entry, 910701, "Second BL Player", afl_team_id=2)
    carl_player = own_player(db, scope["season_id"], entry, 910702, "CARL Player", afl_team_id=5)
    or_client = MultiRoundMatchClient(
        {
            954: [
                Match(8001, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED"),
            ]
        }
    )
    nominations = OpeningRoundNominationRepository(db)
    nominations.nominate(rule.rule_id, entry.season_entry_id, "M1", player.season_player_id, or_client, actor=SCORER)

    # Same target slot, different rule -- rejected.
    with pytest.raises(OpeningRoundError):
        nominations.nominate(
            rule_carl.rule_id, entry.season_entry_id, "M1", carl_player.season_player_id, or_client, actor=SCORER
        )

    # Same player nominated again into a different slot -- rejected.
    with pytest.raises(OpeningRoundError):
        nominations.nominate(
            rule.rule_id, entry.season_entry_id, "M2", player.season_player_id, or_client, actor=SCORER
        )

    # Same rule/entry pair nominated again (a second BL player) -- rejected;
    # use correct() to change an existing nomination instead.
    with pytest.raises(OpeningRoundError):
        nominations.nominate(
            rule.rule_id, entry.season_entry_id, "M2", other_bl_player.season_player_id, or_client, actor=SCORER
        )
