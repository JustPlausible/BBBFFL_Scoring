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
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app import opening_round as opening_round_module
from app.afl_client import Match, PlayerStatLine, Team
from app.audit import ActorContext, AuditEventRepository
from app.calculations import MatchupCalculationService
from app.carry_forward import CarryForwardService
from app.db import transaction as database_transaction
from app.lineups import WeeklyLineupRepository
from app.opening_round import (
    ENTITY_TYPE_NOMINATION,
    ENTITY_TYPE_SUBMISSION,
    DeferredSlotLockedError,
    IneligiblePlayerError,
    OpeningRoundError,
    OpeningRoundNominationRepository,
    OpeningRoundRuleRepository,
    OpeningRoundSelectionGuard,
    OpeningRoundSubmissionRepository,
    SubmissionConfirmedError,
    UnauthorizedNominationActorError,
    UnknownRuleError,
    build_opening_round_readiness,
    describe_accepted_rule,
    describe_accepted_rules,
    resolve_afl_club_name,
)
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.season import SeasonRepository
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


def test_submitting_a_vacancy_elsewhere_never_clears_the_deferred_lock():
    """Issue #98: a coach formally submitting a partial lineup with
    deliberate vacancies elsewhere must never disturb an existing Opening
    Round deferred/preloaded lock -- the deferred slot stays exactly as
    nominated, and the vacancies are persisted as `None`, not fabricated."""
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
    assert draft.positions["M1"] == player.season_player_id
    assert all(v is None for k, v in draft.positions.items() if k != "M1")

    guard = OpeningRoundSelectionGuard(nominations)
    submitted = lineups.submit_positions(
        draft.lineup_id,
        draft.positions,
        expected_submission_version=0,
        actor=SCORER,
        source_type="scorer_proxy",
        reason="partial submission: only the deferred slot is named",
        lock_guard=guard,
    )
    assert submitted.positions["M1"] == player.season_player_id
    assert all(v is None for k, v in submitted.positions.items() if k != "M1")


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


def test_calculation_flags_drift_between_persisted_source_match_and_current_opening_round_evidence():
    """`nominate()` freezes `source_afl_match_id` at nomination time. If the
    live/replay Opening Round evidence later resolves this player's club to
    a *different* match (or none at all) -- e.g. a corrected upstream
    fixture -- the scoring path must never silently substitute that
    different match's statistics for the ones actually recorded with the
    nomination; it must fail explicit, scorer-review evidence instead."""
    db = migrated_connection()
    lifecycle, round_, entries, scope = setup_scope(db, 2024, 956)
    home, away = entries[0], entries[1]
    rule, deferred_player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, home, position="M1"
    )
    assert nomination.source_afl_match_id == 8001  # frozen at nomination time, per nominate_bl_2024

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
    # A "corrected upstream fixture": at scoring time, BL's Opening Round
    # match now resolves to a *different* match ID (9999) than the 8001
    # frozen with the nomination -- e.g. afl-api republished the round with
    # renumbered match IDs. A score attached to 9999 must never be trusted
    # as the nomination's recorded source.
    client = MultiRoundMatchClient(
        {954: [Match(9999, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED")]},
        stats_by_match={9999: {910001: PlayerStatLine(910001, disposals=50)}},
    )
    service = MatchupCalculationService(db, client)
    calculated = service.calculate_matchup(matchup.matchup_id)
    home_side = (
        calculated.snapshot["home"]
        if calculated.snapshot["home"]["season_entry_id"] == home.season_entry_id
        else calculated.snapshot["away"]
    )
    m1 = next(slot for slot in home_side["slots"] if slot["position"] == "M1")
    assert m1["scoring_source"] == "opening_round_source_drift"
    assert m1["afl_match_id"] is None
    assert m1["score"] == 0
    assert m1["participation"]["state"] == "unknown"
    assert m1["participation"]["dnp_recommendation"] == "review_required"
    assert "8001" in m1["participation"]["reason"]


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


def test_correction_reconciles_preloaded_slot_without_leaving_stale_assignment():
    from app.lineup_validation import LineupValidationService

    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    _, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0], position="F1")
    nominations = OpeningRoundNominationRepository(db)
    lineups = WeeklyLineupRepository(db)
    nominations.preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )

    corrected = nominations.correct(
        nomination.nomination_id, position="M1", actor=ADMIN, reason="correct deferred target position"
    )
    draft = lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )
    assert corrected.position == "M1"
    assert draft.positions["F1"] is None
    assert draft.positions["M1"] == player.season_player_id
    assert list(draft.positions.values()).count(player.season_player_id) == 1
    assert LineupValidationService(db).validate_submission(draft.lineup_id, draft.positions).valid


def test_correction_reconciles_both_player_and_position_in_preloaded_draft():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    _, original, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, entries[0], position="F1"
    )
    replacement = own_player(db, scope["season_id"], entries[0], 910601, "Replacement BL", afl_team_id=2)
    nominations = OpeningRoundNominationRepository(db)
    lineups = WeeklyLineupRepository(db)
    nominations.preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )

    nominations.correct(
        nomination.nomination_id,
        position="M1",
        season_player_id=replacement.season_player_id,
        actor=ADMIN,
        reason="correct player and target",
    )
    draft = lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id
    )
    assert draft.positions["F1"] is None
    assert draft.positions["M1"] == replacement.season_player_id
    assert original.season_player_id not in draft.positions.values()


def test_correction_conflicts_are_domain_errors_and_leave_nominations_unchanged():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    ev = evidence.EVIDENCE_2024
    bl_rule = accept_rule(db, scope["season_id"], 2, ev, ev.compensating_bye_round["BL"], round_.bbbffl_round_id)
    carl_rule = accept_rule(db, scope["season_id"], 5, ev, ev.compensating_bye_round["CARL"], round_.bbbffl_round_id)
    bl = own_player(db, scope["season_id"], entries[0], 910610, "BL conflict player", afl_team_id=2)
    carl = own_player(db, scope["season_id"], entries[0], 910611, "CARL conflict player", afl_team_id=5)
    client = MultiRoundMatchClient(
        {ev.afl_opening_round_id: [Match(8001, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED")]}
    )
    nominations = OpeningRoundNominationRepository(db)
    first = nominations.nominate(
        bl_rule.rule_id, entries[0].season_entry_id, "F1", bl.season_player_id, client, actor=SCORER
    )
    second = nominations.nominate(
        carl_rule.rule_id, entries[0].season_entry_id, "M1", carl.season_player_id, client, actor=SCORER
    )

    with pytest.raises(OpeningRoundError, match="target slot M1"):
        nominations.correct(first.nomination_id, position="M1", actor=ADMIN, reason="conflicting slot")
    assert nominations.get(first.nomination_id).position == "F1"

    # Under valid domain state, a player already nominated under a different
    # club-specific rule is ineligible for this rule first. Preserve that
    # eligibility ordering rather than corrupting cached club facts merely to
    # force the defensive duplicate-player branch.
    with pytest.raises(IneligiblePlayerError, match="does not match rule club"):
        nominations.correct(
            first.nomination_id,
            season_player_id=carl.season_player_id,
            actor=ADMIN,
            reason="conflicting player",
        )
    assert nominations.get(first.nomination_id).season_player_id == bl.season_player_id
    assert nominations.get(second.nomination_id).season_player_id == carl.season_player_id


def test_correction_translates_a_database_uniqueness_race_without_partial_mutation(monkeypatch):
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    _, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0], position="F1")

    @contextmanager
    def uniqueness_race(database):
        with database_transaction(database) as connection:

            class RacingConnection:
                def execute(self, statement, parameters=()):
                    if statement.startswith("UPDATE opening_round_nomination SET"):
                        raise IntegrityError(statement, parameters, RuntimeError("simulated uniqueness race"))
                    return connection.execute(statement, parameters)

            yield RacingConnection()

    monkeypatch.setattr(opening_round_module, "transaction", uniqueness_race)
    nominations = OpeningRoundNominationRepository(db)
    with pytest.raises(OpeningRoundError, match="conflicts with an existing target slot or nominated player"):
        nominations.correct(nomination.nomination_id, position="M1", actor=ADMIN, reason="race with another correction")

    unchanged = nominations.get(nomination.nomination_id)
    assert unchanged.position == "F1"
    assert unchanged.season_player_id == player.season_player_id


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


def test_second_player_under_the_same_rule_is_permitted_in_a_distinct_position():
    """Issue #135: the removed `uq_opening_round_nomination_rule_entry`
    constraint used to reject a second player nominated under the same
    accepted rule/entry pair. An accepted rule scopes one AFL club's Opening
    Round mapping, not how many of that club's owned players may be
    nominated into distinct target-round slots -- a second, distinct BL
    player in a distinct slot must now succeed, and both nominations must
    remain independently readable and independently player-unique."""
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    ev = evidence.EVIDENCE_2024
    rule = accept_rule(db, scope["season_id"], 2, ev, ev.compensating_bye_round["BL"], round_.bbbffl_round_id)
    entry = entries[0]
    player = own_player(db, scope["season_id"], entry, 910700, "BL Player", afl_team_id=2)
    other_bl_player = own_player(db, scope["season_id"], entry, 910701, "Second BL Player", afl_team_id=2)
    or_client = MultiRoundMatchClient({954: [Match(8001, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED")]})
    nominations = OpeningRoundNominationRepository(db)
    first = nominations.nominate(
        rule.rule_id, entry.season_entry_id, "M1", player.season_player_id, or_client, actor=SCORER
    )
    second = nominations.nominate(
        rule.rule_id, entry.season_entry_id, "M2", other_bl_player.season_player_id, or_client, actor=SCORER
    )
    assert first.rule_id == second.rule_id == rule.rule_id
    assert {first.season_player_id, second.season_player_id} == {
        player.season_player_id,
        other_bl_player.season_player_id,
    }
    results = nominations.list_for_season_entry(scope["season_id"], entry.season_entry_id)
    assert {n.nomination_id for n in results} == {first.nomination_id, second.nomination_id}

    # This is also a schema-level guarantee, not merely an application-layer
    # allowance -- a raw duplicate (rule_id, season_entry_id) row is no
    # longer rejected by the database itself (see migrations/versions/
    # 0024_opening_round_multi_player.py, which removed
    # `uq_opening_round_nomination_rule_entry`); a third BL player at a
    # third distinct slot, inserted directly, is accepted too.
    third_bl_player = own_player(db, scope["season_id"], entry, 910703, "Third BL Player", afl_team_id=2)
    with database_transaction(db) as conn:
        conn.execute(
            "INSERT INTO opening_round_nomination VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                scope["season_id"],
                rule.rule_id,
                round_.bbbffl_round_id,
                entry.season_entry_id,
                "M3",
                third_bl_player.season_player_id,
                None,
                "anonymous_operator",
                "scorer",
                "scorer",
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
            ),
        )
    results = nominations.list_for_season_entry(scope["season_id"], entry.season_entry_id)
    assert len(results) == 3


# -- Issue #135: multiple owned players from one AFL club -------------------
#
# The historical regression case from issue #135: one BBBFFL entry submits
# nine Opening Round nominations across three target rounds, built entirely
# from the real 2026 evidence (tests/opening_round_evidence.py's
# EVIDENCE_2026 -- COLL/CARL/GEEL/BL share compensating bye AFL round 1345
# (target Round 2), GCFC/WB/HAW/SYD share 1346 (target Round 3), STK/GWS
# share 1347 (target Round 4)). Two target rounds each carry two nominated
# players from the *same* AFL club under the *same* accepted rule -- exactly
# what the removed `uq_opening_round_nomination_rule_entry` constraint used
# to reject.


def _issue_135_scope(db):
    """Season/competition/entries with three target BBBFFL rounds mapped to
    the real 2026 compensating-bye AFL rounds (R2=1345, R3=1346, R4=1347),
    all open for lineup submission -- the scaffold the issue #135 nine-
    player historical regression is built on."""
    from app.round_mapping import RoundMappingRepository
    from app.season import SeasonRepository

    lifecycle, round2, entries, scope = setup_scope(db, 2026, 1345)
    seasons = SeasonRepository(db)
    round3 = seasons.create_round(scope["competition_id"], "round-3", "Round 3", 2)
    RoundMappingRepository(db).accept(round3.bbbffl_round_id, 2026, 1346, KnownRounds((2026, 1346)))
    lifecycle.create_ordinary_round(round3.bbbffl_round_id)
    lifecycle.transition(round3.bbbffl_round_id, "open")
    round4 = seasons.create_round(scope["competition_id"], "round-4", "Round 4", 3)
    RoundMappingRepository(db).accept(round4.bbbffl_round_id, 2026, 1347, KnownRounds((2026, 1347)))
    lifecycle.create_ordinary_round(round4.bbbffl_round_id)
    lifecycle.transition(round4.bbbffl_round_id, "open")
    return lifecycle, {"R2": round2, "R3": round3, "R4": round4}, entries, scope


# (club code, afl_club_id, target round key, position, canonical_player_id, display name)
_ISSUE_135_NOMINATIONS = (
    ("COLL", 3, "R2", "F1", 930201, "Jamie Elliott"),
    ("CARL", 5, "R2", "M1", 930202, "George Hewett"),
    ("COLL", 3, "R2", "M2", 930203, "Josh Daicos"),
    ("HAW", 9, "R3", "F1", 930204, "Jack Gunston"),
    ("GCFC", 4, "R3", "M1", 930205, "Noah Anderson"),
    ("WB", 8, "R3", "M2", 930206, "Ed Richards"),
    ("GCFC", 4, "R3", "Interchange", 930207, "Touk Miller"),
    ("GWS", 15, "R4", "F1", 930208, "Toby Greene"),
    ("STK", 11, "R4", "Interchange", 930209, "Rowan Marshall"),
)

# Opening Round (AFL round 1343) matches pairing the seven clubs involved --
# synthetic pairings, not a claimed real 2026 Opening Round draw (see this
# module's docstring and docs/opening-round-deferred-selection.md's
# evidence-boundary section).
_ISSUE_135_OR_CLIENT = MultiRoundMatchClient(
    {
        1343: [
            Match(9301, Team(3, "COLL"), Team(5, "CARL"), "CONCLUDED"),
            Match(9302, Team(9, "HAW"), Team(4, "GCFC"), "CONCLUDED"),
            Match(9303, Team(8, "WB"), Team(15, "GWS"), "CONCLUDED"),
            Match(9304, Team(11, "STK"), Team(999, "Filler"), "CONCLUDED"),
        ]
    },
    # `app.scoring.score_position` scores a Forward slot from goals/behinds
    # and a Midfield slot from disposals (see `app.scoring.ScoringRules`'s
    # default coefficients: forward_goal=6, forward_behind=1,
    # midfield_disposal=1) -- each nominated player's stat line uses
    # whichever fields their own target position actually scores from.
    stats_by_match={
        9301: {
            930201: PlayerStatLine(930201, goals=4, behinds=1),  # Jamie Elliott (F1): 6*4+1 = 25
            930203: PlayerStatLine(930203, disposals=22),  # Josh Daicos (M2): 22
            930202: PlayerStatLine(930202, disposals=18),  # George Hewett (M1): 18
        },
        9302: {
            930204: PlayerStatLine(930204, goals=3, behinds=2),  # Jack Gunston (F1): 6*3+2 = 20
            930205: PlayerStatLine(930205, disposals=27),  # Noah Anderson (M1): 27
            930207: PlayerStatLine(930207, disposals=24),  # Touk Miller (Interchange)
        },
        9303: {
            930206: PlayerStatLine(930206, disposals=19),  # Ed Richards (M2): 19
            930208: PlayerStatLine(930208, goals=2, behinds=4),  # Toby Greene (F1): 6*2+4 = 16
        },
        9304: {930209: PlayerStatLine(930209, disposals=15)},  # Rowan Marshall (Interchange)
    },
)


def _build_issue_135_regression(db):
    """Accept every rule, own every player and submit all nine historical
    nominations for one entry. Returns
    `(lifecycle, rounds, scope, entry, players, nominations)` where
    `players`/`nominations` are keyed by display name."""
    ev = evidence.EVIDENCE_2026
    lifecycle, rounds, entries, scope = _issue_135_scope(db)
    entry = entries[0]
    accepted_clubs: dict[int, object] = {}
    players: dict[str, object] = {}
    nominations: dict[str, object] = {}
    nomination_repo = OpeningRoundNominationRepository(db)
    for code, club_id, round_key, position, canonical_id, name in _ISSUE_135_NOMINATIONS:
        if club_id not in accepted_clubs:
            accepted_clubs[club_id] = accept_rule(
                db, scope["season_id"], club_id, ev, ev.compensating_bye_round[code], rounds[round_key].bbbffl_round_id
            )
        rule = accepted_clubs[club_id]
        player = own_named_player(
            db, scope["season_id"], entry, canonical_id, name, afl_team_id=club_id, afl_team_name=code
        )
        players[name] = player
        nominations[name] = nomination_repo.nominate(
            rule.rule_id, entry.season_entry_id, position, player.season_player_id, _ISSUE_135_OR_CLIENT, actor=SCORER
        )
    return lifecycle, rounds, scope, entry, players, nominations


def test_issue_135_historical_nine_player_submission_is_fully_accepted():
    """Regression coverage items 1-4: two Collingwood players in distinct
    Round 2 positions, two Gold Coast players in distinct Round 3
    positions, the full nine-player historical submission, and F1/
    Interchange each reused across different target rounds."""
    db = migrated_connection()
    _, rounds, scope, entry, players, nominations = _build_issue_135_regression(db)
    assert len(nominations) == 9

    coll_rule_id = nominations["Jamie Elliott"].rule_id
    assert nominations["Josh Daicos"].rule_id == coll_rule_id  # same club, same rule, same entry
    assert nominations["Jamie Elliott"].position != nominations["Josh Daicos"].position
    assert (
        nominations["Jamie Elliott"].bbbffl_round_id
        == nominations["Josh Daicos"].bbbffl_round_id
        == rounds["R2"].bbbffl_round_id
    )

    gcfc_rule_id = nominations["Noah Anderson"].rule_id
    assert nominations["Touk Miller"].rule_id == gcfc_rule_id  # same club, same rule, same entry
    assert nominations["Noah Anderson"].position != nominations["Touk Miller"].position
    assert (
        nominations["Noah Anderson"].bbbffl_round_id
        == nominations["Touk Miller"].bbbffl_round_id
        == rounds["R3"].bbbffl_round_id
    )

    # F1 is nominated once per target round -- reusing the same position
    # label across three distinct target BBBFFL rounds is valid.
    f1_rounds = {
        nominations[name].bbbffl_round_id
        for name in ("Jamie Elliott", "Jack Gunston", "Toby Greene")
        if nominations[name].position == "F1"
    }
    assert f1_rounds == {rounds["R2"].bbbffl_round_id, rounds["R3"].bbbffl_round_id, rounds["R4"].bbbffl_round_id}
    interchange_rounds = {
        nominations[name].bbbffl_round_id
        for name in ("Touk Miller", "Rowan Marshall")
        if nominations[name].position == "Interchange"
    }
    assert interchange_rounds == {rounds["R3"].bbbffl_round_id, rounds["R4"].bbbffl_round_id}

    results = OpeningRoundNominationRepository(db).list_for_season_entry(scope["season_id"], entry.season_entry_id)
    assert {n.nomination_id for n in results} == {n.nomination_id for n in nominations.values()}
    assert len(results) == 9  # every nomination returned exactly once


def test_issue_135_historical_submission_readiness_has_no_conflicts_and_confirms():
    """Regression coverage item 10: readiness must not label the nine valid
    same-rule/multi-player nominations as duplicates, mismatches or
    conflicts, and the entry must be able to confirm its submission."""
    db = migrated_connection()
    _, rounds, scope, entry, players, nominations = _build_issue_135_regression(db)

    readiness = build_opening_round_readiness(db, scope["season_id"])
    assert readiness.duplicate_nominations == ()
    assert readiness.mismatched_nominations == ()
    assert readiness.conflicting_nominations == ()
    entry_readiness = readiness.for_entry(entry.season_entry_id)
    assert entry_readiness.nomination_count == 9

    confirmed = OpeningRoundSubmissionRepository(db).confirm(scope["season_id"], entry.season_entry_id, actor=ADMIN)
    assert confirmed.state == "confirmed"
    after_confirm = build_opening_round_readiness(db, scope["season_id"])
    assert after_confirm.duplicate_nominations == ()
    assert after_confirm.for_entry(entry.season_entry_id).is_confirmed is True


def test_issue_135_historical_submission_preloads_and_locks_every_position_independently():
    """Regression coverage item 11: every valid same-rule nomination
    preloads into its own correct target-round position, is independently
    locked, cannot be displaced by ordinary weekly lineup editing, does not
    overwrite a sibling nomination sharing the same rule, does not affect
    unused positions, and does not leak into other target rounds."""
    db = migrated_connection()
    _, rounds, scope, entry, players, nominations = _build_issue_135_regression(db)
    lineups = WeeklyLineupRepository(db)
    nomination_repo = OpeningRoundNominationRepository(db)

    expected_by_round = {
        "R2": {"F1": "Jamie Elliott", "M1": "George Hewett", "M2": "Josh Daicos"},
        "R3": {"F1": "Jack Gunston", "M1": "Noah Anderson", "M2": "Ed Richards", "Interchange": "Touk Miller"},
        "R4": {"F1": "Toby Greene", "Interchange": "Rowan Marshall"},
    }
    drafts = {}
    for round_key, round_ in rounds.items():
        nomination_repo.preload_target_lineup(
            lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
        )
        draft = lineups.get_draft(
            scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
        )
        drafts[round_key] = draft
        for position, name in expected_by_round[round_key].items():
            assert draft.positions[position] == players[name].season_player_id
        # Positions with no nomination in this round remain untouched/empty.
        untouched = set(draft.positions) - set(expected_by_round[round_key])
        assert all(draft.positions[position] is None for position in untouched)
        # No cross-round leakage: none of this round's players appear under
        # another round's nominated names.
        other_rounds_players = {
            players[name].season_player_id
            for key, mapping in expected_by_round.items()
            if key != round_key
            for name in mapping.values()
        }
        assert not (set(draft.positions.values()) & other_rounds_players)

    # Every nominated position is independently locked: an attempt to move
    # a different player into any one of them is rejected, while the
    # sibling nominations sharing the same rule/round remain unaffected.
    guard = OpeningRoundSelectionGuard(nomination_repo)
    r2_draft = drafts["R2"]
    full = complete_lineup(db, scope, entry, overrides=r2_draft.positions)
    impostor = own_named_player(
        db, scope["season_id"], entry, 930299, "F1 Impostor", afl_team_id=None, afl_team_name=None
    )
    with pytest.raises(DeferredSlotLockedError):
        lineups.submit_positions(
            r2_draft.lineup_id,
            {**full, "F1": impostor.season_player_id},
            expected_submission_version=0,
            actor=SCORER,
            source_type="scorer_proxy",
            reason="attempt to displace Jamie Elliott with an unrelated player",
            lock_guard=guard,
        )
    assert lineups.get_effective_submission(r2_draft.lineup_id) is None
    submitted = lineups.submit_positions(
        r2_draft.lineup_id,
        full,
        expected_submission_version=0,
        actor=SCORER,
        source_type="scorer_proxy",
        reason="legitimate submission keeping both Collingwood nominations intact",
        lock_guard=guard,
    )
    assert submitted.positions["F1"] == players["Jamie Elliott"].season_player_id
    assert submitted.positions["M2"] == players["Josh Daicos"].season_player_id
    assert submitted.positions["M1"] == players["George Hewett"].season_player_id


def test_issue_135_two_players_sharing_one_rule_are_each_scored_from_their_own_correct_source():
    """Regression coverage item 12: deferred scoring includes every valid
    nomination, and two players sharing the same accepted rule (the two
    Collingwood players in Round 2, and the two Gold Coast players in
    Round 3) each independently score from the shared Opening Round source
    match with their own statistics -- never each other's, never
    collapsed."""
    db = migrated_connection()
    lifecycle, rounds, scope, entry, players, nominations = _build_issue_135_regression(db)
    lineups = WeeklyLineupRepository(db)
    nomination_repo = OpeningRoundNominationRepository(db)

    def _submit_round(round_key):
        round_ = rounds[round_key]
        nomination_repo.preload_target_lineup(
            lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
        )
        draft = lineups.get_draft(
            scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
        )
        # Submitted exactly as preloaded (issue #98's valid-partial-lineup
        # precedent -- unused positions are deliberate vacancies, not
        # neutral fillers): only the nominated slots matter for this
        # assertion.
        lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)
        matchup = next(
            m
            for m in lifecycle.list_matchups(round_.bbbffl_round_id)
            if entry.season_entry_id in (m.home_season_entry_id, m.away_season_entry_id)
        )
        opponent_id = (
            matchup.away_season_entry_id
            if matchup.home_season_entry_id == entry.season_entry_id
            else matchup.home_season_entry_id
        )
        away_positions = complete_lineup(db, scope, _entry_ref(opponent_id))
        away_draft = lineups.save_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            opponent_id,
            away_positions,
            expected_revision=0,
        )
        lineups.submit(away_draft.lineup_id, expected_draft_revision=away_draft.revision, expected_submission_version=0)
        service = MatchupCalculationService(db, _ISSUE_135_OR_CLIENT)
        calculated = service.calculate_matchup(matchup.matchup_id)
        home_side = (
            calculated.snapshot["home"]
            if calculated.snapshot["home"]["season_entry_id"] == entry.season_entry_id
            else calculated.snapshot["away"]
        )
        return {slot["position"]: slot for slot in home_side["slots"]}

    r2_slots = _submit_round("R2")
    assert r2_slots["F1"]["scoring_source"] == "opening_round_deferred"
    assert r2_slots["F1"]["afl_match_id"] == 9301
    assert r2_slots["F1"]["score"] == 25  # Jamie Elliott
    assert r2_slots["M2"]["scoring_source"] == "opening_round_deferred"
    assert r2_slots["M2"]["afl_match_id"] == 9301
    assert r2_slots["M2"]["score"] == 22  # Josh Daicos -- distinct from Jamie Elliott's score above
    assert r2_slots["M1"]["afl_match_id"] == 9301
    assert r2_slots["M1"]["score"] == 18  # George Hewett, Carlton, same source match

    r3_slots = _submit_round("R3")
    assert r3_slots["M1"]["scoring_source"] == "opening_round_deferred"
    assert r3_slots["M1"]["afl_match_id"] == 9302
    assert r3_slots["M1"]["score"] == 27  # Noah Anderson
    assert r3_slots["Interchange"]["scoring_source"] == "opening_round_deferred"
    assert r3_slots["Interchange"]["afl_match_id"] == 9302
    # Interchange never carries its own `score` (see
    # `MatchupCalculationService._entry`'s Interchange handling) -- its raw
    # stat line is what proves it independently resolved Touk Miller's own
    # disposals, distinct from Noah Anderson's, from the same shared match.
    assert r3_slots["Interchange"]["stats"]["disposals"] == 24  # Touk Miller


def _entry_ref(season_entry_id):
    """Adapts a bare `season_entry_id` to the `.season_entry_id`-only shape
    `tests.lineup_helpers.complete_lineup` expects, for an opponent entry
    this module otherwise only has an ID for (resolved from the round's own
    frozen fixture pairing, not chosen arbitrarily)."""
    return type("EntryRef", (), {"season_entry_id": season_entry_id})()


# -- Presentation and readiness (issue #131) ---------------------------------


class RoundNumberClient:
    """Duck-typed AFL client exposing only `get_rounds` -- for presentation
    tests that resolve AFL round numbers into human-readable labels, never
    used for scoring or match resolution."""

    def __init__(self, rounds_by_season):
        self.rounds_by_season = rounds_by_season
        self.calls = 0

    def get_rounds(self, season_id):
        self.calls += 1
        return self.rounds_by_season.get(season_id, [])


def own_named_player(db, season_id, entry, canonical_id, name, afl_team_id, afl_team_name):
    """Like `own_player`, but also caches `afl_team_name` -- needed for the
    presentation tests below, which resolve a club's display name from
    `season_player_pool` (see `app.opening_round.resolve_afl_club_name`)."""
    player = PlayerPoolRepository(db).refresh_player(
        season_id, canonical_id, name, afl_team_id=afl_team_id, afl_team_name=afl_team_name
    )
    OwnershipRepository(db).acquire(player.season_player_id, entry.season_entry_id)
    return player


def test_resolve_afl_club_name_is_none_until_a_player_pool_row_caches_it():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1345)
    assert resolve_afl_club_name(db, scope["season_id"], 15) is None
    own_named_player(
        db, scope["season_id"], entries[0], 920100, "GWS Player", afl_team_id=15, afl_team_name="GWS Giants"
    )
    assert resolve_afl_club_name(db, scope["season_id"], 15) == "GWS Giants"
    # An unrelated club with no cached player is still unresolved.
    assert resolve_afl_club_name(db, scope["season_id"], 3) is None


def test_describe_accepted_rule_produces_human_readable_primary_label_with_ids_retained():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    ev = evidence.EVIDENCE_2026
    rule = accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)
    own_named_player(
        db, scope["season_id"], entries[0], 920101, "GWS Player", afl_team_id=15, afl_team_name="GWS Giants"
    )
    client = RoundNumberClient(
        {
            ev.afl_season_id: [
                type("R", (), {"round_id": 1343, "round_number": 0})(),
                type("R", (), {"round_id": 1347, "round_number": 4})(),
            ]
        }
    )

    view = describe_accepted_rule(rule, db, client)

    assert view["afl_club_name"] == "GWS Giants"
    assert view["afl_opening_round_label"] == "Opening Round"
    assert view["afl_bye_round_label"] == "AFL Round 4"
    assert view["bbbffl_round_label"] == round_.label
    assert view["display_label"] == f"GWS Giants — Opening Round → compensating AFL Round 4 → {round_.label}"
    # Internal identifiers remain present as secondary diagnostics -- never
    # replaced by the resolved presentation fields (issue #131 #3).
    assert view["rule_id"] == rule.rule_id
    assert view["afl_club_id"] == 15
    assert view["afl_opening_round_id"] == ev.afl_opening_round_id
    assert view["afl_bye_round_id"] == ev.compensating_bye_round["GWS"]
    assert view["bbbffl_round_id"] == round_.bbbffl_round_id


def test_describe_accepted_rule_falls_back_to_internal_ids_when_evidence_is_unavailable():
    """A round-number resolution failure (evidence source down, or a club
    with no cached player yet) must never hide the underlying accepted-rule
    data -- only fall back to a diagnostic label (issue #131's "never mutate
    accepted-rule history merely to make presentation readable")."""
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    ev = evidence.EVIDENCE_2026
    rule = accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)

    class FailingClient:
        def get_rounds(self, season_id):
            raise RuntimeError("evidence source unavailable")

    view = describe_accepted_rule(rule, db, FailingClient())
    assert view["afl_club_name"] is None
    assert view["afl_opening_round_label"] is None
    assert view["rule_id"] == rule.rule_id  # domain data is still returned in full
    assert view["afl_club_id"] == 15


def test_describe_accepted_rules_shares_one_get_rounds_call_per_afl_season():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1345)
    ev = evidence.EVIDENCE_2026
    accept_rule(db, scope["season_id"], 2, ev, ev.compensating_bye_round["BL"], round_.bbbffl_round_id)
    accept_rule(db, scope["season_id"], 5, ev, ev.compensating_bye_round["CARL"], round_.bbbffl_round_id)
    client = RoundNumberClient({ev.afl_season_id: [type("R", (), {"round_id": 1345, "round_number": 2})()]})

    views = describe_accepted_rules(db, client, scope["season_id"])

    assert len(views) == 2
    assert client.calls == 1  # one accepted rule's resolution is reused for the other


# -- Nomination readiness (issue #131 #8, replaced by explicit confirmation
# semantics in issue #133) ----------------------------------------------


def test_readiness_owning_an_eligible_player_never_creates_a_required_nomination():
    """Issue #133's central fix: owning an eligible Opening Round player, or
    an accepted rule existing for a club represented in an entry's squad,
    must never itself imply a required nomination. Before any confirmation,
    every entry (whether or not it owns an eligible player) reads as simply
    unconfirmed -- never as "missing" nominations against eligible rules."""
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    ev = evidence.EVIDENCE_2026
    accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)
    own_named_player(
        db, scope["season_id"], entries[0], 920102, "GWS Player", afl_team_id=15, afl_team_name="GWS Giants"
    )

    readiness = build_opening_round_readiness(db, scope["season_id"])
    assert readiness.total_entries == len(entries)
    assert readiness.total_confirmed == 0
    assert readiness.is_ready is False
    entry0 = readiness.for_entry(entries[0].season_entry_id)
    assert entry0.is_confirmed is False
    assert entry0.nomination_count == 0
    # entries[1] owns nothing eligible at all -- it must still appear,
    # simply as another unconfirmed entry, never omitted or distinguished.
    entry1 = readiness.for_entry(entries[1].season_entry_id)
    assert entry1 is not None
    assert entry1.is_confirmed is False


def test_readiness_counts_confirmed_entries_not_nominations_or_eligible_rules():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    ev = evidence.EVIDENCE_2026
    rule = accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)
    player = own_named_player(
        db, scope["season_id"], entries[0], 920105, "GWS Player", afl_team_id=15, afl_team_name="GWS Giants"
    )
    or_client = MultiRoundMatchClient(
        {ev.afl_opening_round_id: [Match(9103, Team(15, "GWS"), Team(1, "ADEL"), "CONCLUDED")]}
    )
    OpeningRoundNominationRepository(db).nominate(
        rule.rule_id, entries[0].season_entry_id, "M1", player.season_player_id, or_client, actor=SCORER
    )
    # A nomination alone -- with no explicit confirmation -- must not count.
    assert build_opening_round_readiness(db, scope["season_id"]).total_confirmed == 0

    OpeningRoundSubmissionRepository(db).confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)
    after_one = build_opening_round_readiness(db, scope["season_id"])
    assert after_one.total_confirmed == 1
    assert after_one.for_entry(entries[0].season_entry_id).is_confirmed is True
    assert after_one.for_entry(entries[0].season_entry_id).nomination_count == 1
    assert after_one.is_ready is False  # nine other entries have not confirmed

    # An entry with zero nominations confirms just as validly.
    OpeningRoundSubmissionRepository(db).confirm(scope["season_id"], entries[1].season_entry_id, actor=SCORER)
    after_two = build_opening_round_readiness(db, scope["season_id"])
    assert after_two.total_confirmed == 2
    zero_nomination_entry = after_two.for_entry(entries[1].season_entry_id)
    assert zero_nomination_entry.is_confirmed is True
    assert zero_nomination_entry.nomination_count == 0

    for entry in entries[2:]:
        OpeningRoundSubmissionRepository(db).confirm(scope["season_id"], entry.season_entry_id, actor=SCORER)
    full = build_opening_round_readiness(db, scope["season_id"])
    assert full.total_confirmed == full.total_entries == len(entries)
    assert full.is_ready is True


def test_readiness_detects_mismatched_and_conflicting_nominations_independently_of_confirmation():
    """`mismatched`/`conflicting` are defensive integrity checks over
    already-persisted state, unrelated to and reported separately from
    confirmation completeness (issue #133): a confirmed entry whose
    underlying nomination data is later corrupted still shows as confirmed,
    but the corruption remains independently visible and `is_ready` stays
    False."""
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    ev = evidence.EVIDENCE_2026
    rule = accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)
    player = own_named_player(
        db, scope["season_id"], entries[0], 920105, "GWS Player", afl_team_id=15, afl_team_name="GWS Giants"
    )
    or_client = MultiRoundMatchClient(
        {ev.afl_opening_round_id: [Match(9103, Team(15, "GWS"), Team(1, "ADEL"), "CONCLUDED")]}
    )
    nomination = OpeningRoundNominationRepository(db).nominate(
        rule.rule_id, entries[0].season_entry_id, "M1", player.season_player_id, or_client, actor=SCORER
    )
    OpeningRoundSubmissionRepository(db).confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)

    clean = build_opening_round_readiness(db, scope["season_id"])
    assert clean.mismatched_nominations == ()
    assert clean.conflicting_nominations == ()
    assert clean.for_entry(entries[0].season_entry_id).is_confirmed is True

    # Simulate the player's cached AFL club changing after the nomination
    # (never done through app.opening_round itself). `database.execute` is
    # a short-lived, no-explicit-commit connection meant for reads (see
    # `app.db.DatabaseConnection.execute`'s docstring); a write needs
    # `transaction()` to actually persist across the next call.
    with database_transaction(db) as conn:
        conn.execute(
            "UPDATE season_player_pool SET afl_team_id=? WHERE season_player_id=?", (99, player.season_player_id)
        )
    mismatched = build_opening_round_readiness(db, scope["season_id"])
    assert [m["nomination_id"] for m in mismatched.mismatched_nominations] == [nomination.nomination_id]
    assert mismatched.is_ready is False
    # The entry's *confirmation* is entirely unaffected by the corruption --
    # confirmation completeness and integrity are independent signals.
    assert mismatched.for_entry(entries[0].season_entry_id).is_confirmed is True
    with database_transaction(db) as conn:
        conn.execute(
            "UPDATE season_player_pool SET afl_team_id=? WHERE season_player_id=?", (15, player.season_player_id)
        )

    # Simulate the nomination's captured target round drifting from the
    # rule's own target round (never possible through app.opening_round's
    # own write paths -- OpeningRoundRuleHasNominationsError blocks a rule
    # correction while nominations exist). A real, distinct round is used
    # (not a fabricated UUID) so the FK constraint on
    # `opening_round_nomination.bbbffl_round_id` is still satisfied.
    other_round = SeasonRepository(db).create_round(scope["competition_id"], "round-2", "Round 2", 2)
    with database_transaction(db) as conn:
        conn.execute(
            "UPDATE opening_round_nomination SET bbbffl_round_id=? WHERE nomination_id=?",
            (other_round.bbbffl_round_id, nomination.nomination_id),
        )
    conflicting = build_opening_round_readiness(db, scope["season_id"])
    assert [c["nomination_id"] for c in conflicting.conflicting_nominations] == [nomination.nomination_id]
    assert conflicting.is_ready is False
    assert conflicting.for_entry(entries[0].season_entry_id).is_confirmed is True


# -- Explicit Opening Round submission confirmation (issue #133) ------------


def test_confirm_with_zero_nominations_is_valid_and_complete():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    ev = evidence.EVIDENCE_2026
    accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)
    own_named_player(
        db, scope["season_id"], entries[0], 920110, "GWS Player", afl_team_id=15, afl_team_name="GWS Giants"
    )
    submissions = OpeningRoundSubmissionRepository(db)
    assert submissions.get(scope["season_id"], entries[0].season_entry_id) is None

    confirmed = submissions.confirm(
        scope["season_id"], entries[0].season_entry_id, actor=SCORER, reason="deliberately no nominations this week"
    )
    assert confirmed.state == "confirmed"
    assert confirmed.confirmed_at is not None
    assert confirmed.actor_type == "anonymous_operator" and confirmed.actor_role == "scorer"
    assert submissions.is_confirmed(scope["season_id"], entries[0].season_entry_id) is True
    assert (
        OpeningRoundNominationRepository(db).list_for_season_entry(scope["season_id"], entries[0].season_entry_id) == []
    )


def test_confirm_with_a_partial_legal_nomination_set_is_valid():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1345)
    ev = evidence.EVIDENCE_2026
    rule_bl = accept_rule(db, scope["season_id"], 2, ev, ev.compensating_bye_round["BL"], round_.bbbffl_round_id)
    accept_rule(db, scope["season_id"], 5, ev, ev.compensating_bye_round["CARL"], round_.bbbffl_round_id)
    bl_player = own_named_player(
        db, scope["season_id"], entries[0], 920111, "BL Player", afl_team_id=2, afl_team_name="Brisbane Lions"
    )
    # Also owns a Carlton-eligible player, deliberately left unnominated.
    own_named_player(db, scope["season_id"], entries[0], 920112, "CARL Player", afl_team_id=5, afl_team_name="Carlton")
    or_client = MultiRoundMatchClient(
        {ev.afl_opening_round_id: [Match(9110, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED")]}
    )
    OpeningRoundNominationRepository(db).nominate(
        rule_bl.rule_id, entries[0].season_entry_id, "M1", bl_player.season_player_id, or_client, actor=SCORER
    )
    submissions = OpeningRoundSubmissionRepository(db)
    confirmed = submissions.confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)
    assert confirmed.state == "confirmed"
    assert (
        len(OpeningRoundNominationRepository(db).list_for_season_entry(scope["season_id"], entries[0].season_entry_id))
        == 1
    )


def test_confirm_is_idempotent_and_concurrency_safe():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    ev = evidence.EVIDENCE_2026
    accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)
    submissions = OpeningRoundSubmissionRepository(db)
    first = submissions.confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER, reason="first")
    second = submissions.confirm(scope["season_id"], entries[0].season_entry_id, actor=ADMIN, reason="second")
    assert second == first  # no new revision, no actor/reason overwrite
    assert len(submissions.history(scope["season_id"], entries[0].season_entry_id)) == 1


def test_confirmed_submission_cannot_be_silently_mutated_by_a_new_nomination():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    ev = evidence.EVIDENCE_2026
    rule = accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)
    player = own_named_player(
        db, scope["season_id"], entries[0], 920113, "GWS Player", afl_team_id=15, afl_team_name="GWS Giants"
    )
    OpeningRoundSubmissionRepository(db).confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)

    or_client = MultiRoundMatchClient(
        {ev.afl_opening_round_id: [Match(9111, Team(15, "GWS"), Team(1, "ADEL"), "CONCLUDED")]}
    )
    with pytest.raises(SubmissionConfirmedError):
        OpeningRoundNominationRepository(db).nominate(
            rule.rule_id, entries[0].season_entry_id, "M1", player.season_player_id, or_client, actor=SCORER
        )
    assert (
        OpeningRoundNominationRepository(db).list_for_season_entry(scope["season_id"], entries[0].season_entry_id) == []
    )


def test_confirmed_submission_cannot_be_silently_mutated_by_a_correction():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    OpeningRoundSubmissionRepository(db).confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)

    with pytest.raises(SubmissionConfirmedError):
        OpeningRoundNominationRepository(db).correct(
            nomination.nomination_id, position="M2", actor=ADMIN, reason="attempted post-confirmation change"
        )
    assert OpeningRoundNominationRepository(db).get(nomination.nomination_id).position == nomination.position


def test_reopen_requires_a_reason_and_a_currently_confirmed_submission():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    ev = evidence.EVIDENCE_2026
    accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)
    submissions = OpeningRoundSubmissionRepository(db)
    with pytest.raises(OpeningRoundError):
        submissions.reopen(scope["season_id"], entries[0].season_entry_id, actor=ADMIN, reason="nothing confirmed yet")

    submissions.confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)
    with pytest.raises(OpeningRoundError):
        submissions.reopen(scope["season_id"], entries[0].season_entry_id, actor=ADMIN, reason="")


def test_confirm_is_refused_before_opening_round_is_configured_for_the_season():
    """PR #134 review (P2): confirming before the season has any accepted
    Opening Round rule would let a later-accepted rule's readiness silently
    count a confirmation the operator never reviewed against it."""
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    submissions = OpeningRoundSubmissionRepository(db)
    with pytest.raises(OpeningRoundError, match="no accepted Opening Round rule"):
        submissions.confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)

    ev = evidence.EVIDENCE_2026
    accept_rule(db, scope["season_id"], 15, ev, ev.compensating_bye_round["GWS"], round_.bbbffl_round_id)
    confirmed = submissions.confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)
    assert confirmed.state == "confirmed"


def test_reopen_is_explicit_audited_and_permits_a_subsequent_correction():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2024, 956)
    rule, player, nomination = nominate_bl_2024(db, scope["season_id"], round_.bbbffl_round_id, entries[0])
    submissions = OpeningRoundSubmissionRepository(db)
    submissions.confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)

    reopened = submissions.reopen(
        scope["season_id"], entries[0].season_entry_id, actor=ADMIN, reason="historical reconstruction correction"
    )
    assert reopened.state == "draft"
    assert submissions.is_confirmed(scope["season_id"], entries[0].season_entry_id) is False

    other_player = own_player(db, scope["season_id"], entries[0], 920114, "Corrected BL Player", afl_team_id=2)
    corrected = OpeningRoundNominationRepository(db).correct(
        nomination.nomination_id,
        season_player_id=other_player.season_player_id,
        actor=ADMIN,
        reason="reopened correction",
    )
    assert corrected.season_player_id == other_player.season_player_id

    history = submissions.history(scope["season_id"], entries[0].season_entry_id)
    assert [h.state for h in history] == ["confirmed", "draft"]
    assert history[1].reason == "historical reconstruction correction"

    events = AuditEventRepository(db).list_events(entity_type=ENTITY_TYPE_SUBMISSION)
    assert [e.action for e in events] == ["opening_round.submission.confirmed", "opening_round.submission.reopened"]

    # Re-confirming after the correction is a fresh, independently audited
    # confirmation -- never inferred, never merged with the prior one.
    reconfirmed = submissions.confirm(scope["season_id"], entries[0].season_entry_id, actor=SCORER)
    assert reconfirmed.state == "confirmed"
    assert len(submissions.history(scope["season_id"], entries[0].season_entry_id)) == 3


def test_confirmation_requires_an_operator_actor_never_the_coach():
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1347)
    with pytest.raises(UnauthorizedNominationActorError):
        OpeningRoundSubmissionRepository(db).confirm(scope["season_id"], entries[0].season_entry_id, actor=COACH)


# -- Represented-entry nomination read model duplication (issue #133) -------


def test_list_for_season_entry_returns_one_nomination_once_when_four_rules_share_its_target_round():
    """The exact discovered defect: four accepted rules targeting one
    BBBFFL round, one persisted nomination in that round -- the
    season/entry-scoped read must return it exactly once, never once per
    accepted rule that happens to share the round."""
    db = migrated_connection()
    _, round_, entries, scope = setup_scope(db, 2026, 1345)
    ev = evidence.EVIDENCE_2026
    entry = entries[0]
    # The real 2026 evidence: BL, COLL, CARL and GEEL all share compensating
    # bye AFL round 1345 -- four accepted rules genuinely targeting one
    # BBBFFL round, exactly the discovered reproduction (issue #133).
    clubs = [("BL", 2), ("COLL", 3), ("CARL", 5), ("GEEL", 10)]
    for code, club_id in clubs:
        accept_rule(db, scope["season_id"], club_id, ev, ev.compensating_bye_round[code], round_.bbbffl_round_id)
    bl_player = own_named_player(
        db, scope["season_id"], entry, 920120, "BL Player", afl_team_id=2, afl_team_name="Brisbane Lions"
    )
    or_client = MultiRoundMatchClient(
        {ev.afl_opening_round_id: [Match(9120, Team(2, "BL"), Team(5, "CARL"), "CONCLUDED")]}
    )
    rule_bl = OpeningRoundRuleRepository(db).resolve(scope["season_id"], 2)
    nomination = OpeningRoundNominationRepository(db).nominate(
        rule_bl.rule_id, entry.season_entry_id, "M1", bl_player.season_player_id, or_client, actor=SCORER
    )
    corrected = OpeningRoundNominationRepository(db).correct(
        nomination.nomination_id, position="F1", actor=ADMIN, reason="audited correction"
    )

    results = OpeningRoundNominationRepository(db).list_for_season_entry(scope["season_id"], entry.season_entry_id)
    assert [n.nomination_id for n in results] == [corrected.nomination_id]
    events = AuditEventRepository(db).list_events(
        entity_type=ENTITY_TYPE_NOMINATION, entity_id=nomination.nomination_id
    )
    correction_events = [e for e in events if e.action == "opening_round.nomination.corrected"]
    assert len(correction_events) == 1  # never multiplied by the four sharing rules


def test_list_for_season_entry_returns_two_nominations_across_two_shared_target_rounds():
    db = migrated_connection()
    _, round2, entries, scope = setup_scope(db, 2026, 1345)
    ev = evidence.EVIDENCE_2026
    entry = entries[0]
    round3 = SeasonRepository(db).create_round(scope["competition_id"], "round-3", "Round 3", 3)
    from app.competition_lifecycle import CompetitionLifecycleRepository
    from app.round_mapping import RoundMappingRepository

    RoundMappingRepository(db).accept(round3.bbbffl_round_id, 2026, 1346, KnownRounds((2026, 1346)))
    CompetitionLifecycleRepository(db).create_ordinary_round(round3.bbbffl_round_id)

    # Two rules target round2 (BL, CARL, both compensating bye 1345); two
    # rules target round3 (GCFC, WB, both compensating bye 1346).
    accept_rule(db, scope["season_id"], 2, ev, ev.compensating_bye_round["BL"], round2.bbbffl_round_id)
    accept_rule(db, scope["season_id"], 5, ev, ev.compensating_bye_round["CARL"], round2.bbbffl_round_id)
    accept_rule(db, scope["season_id"], 4, ev, ev.compensating_bye_round["GCFC"], round3.bbbffl_round_id)
    accept_rule(db, scope["season_id"], 8, ev, ev.compensating_bye_round["WB"], round3.bbbffl_round_id)

    bl_player = own_named_player(db, scope["season_id"], entry, 920121, "BL Player", afl_team_id=2, afl_team_name="BL")
    gcfc_player = own_named_player(
        db, scope["season_id"], entry, 920122, "GCFC Player", afl_team_id=4, afl_team_name="GCFC"
    )
    or_client = MultiRoundMatchClient(
        {ev.afl_opening_round_id: [Match(9121, Team(2, "BL"), Team(4, "GCFC"), "CONCLUDED")]}
    )
    rule_bl = OpeningRoundRuleRepository(db).resolve(scope["season_id"], 2)
    rule_gcfc = OpeningRoundRuleRepository(db).resolve(scope["season_id"], 4)
    nom1 = OpeningRoundNominationRepository(db).nominate(
        rule_bl.rule_id, entry.season_entry_id, "M1", bl_player.season_player_id, or_client, actor=SCORER
    )
    nom2 = OpeningRoundNominationRepository(db).nominate(
        rule_gcfc.rule_id, entry.season_entry_id, "M2", gcfc_player.season_player_id, or_client, actor=SCORER
    )

    results = OpeningRoundNominationRepository(db).list_for_season_entry(scope["season_id"], entry.season_entry_id)
    assert {n.nomination_id for n in results} == {nom1.nomination_id, nom2.nomination_id}
    assert len(results) == 2
