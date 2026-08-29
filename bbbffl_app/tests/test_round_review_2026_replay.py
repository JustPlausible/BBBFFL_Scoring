"""Deterministic 2026 replay coverage for the scorer round-review/sign-off/
correction workflow (roadmap package 28, issue #58's "2026 replay"
requirement), following the same pattern as
tests/test_carry_forward_2026_replay.py: a deterministic, database-backed
replay exercised through pytest against a `year=2026` season, rather than
a live interactive script.

SYNTHETIC EVIDENCE NOTICE: every player, lineup and round below is
synthetic test fixture data generated for this suite, not genuine
historical 2026 BBBFFL coach selections -- BBBFFL has no live afl-api
access to real 2026 season data from this environment (see
docs/afl-evidence-fixtures.md). Do not read anything here as a historical
record of what any real coach selected in 2026.

This exercises every stage issue #58 requirement 14 names: (1) calculated
scores; (2) scorer review; (3) resolution of a required ruling; (4)
full-round sign-off; (5) effective official results; (6) reopen/correction
of at least one matchup; (7) publication of a replacement official
version -- demonstrating that mock 2026 scoring can safely become official
BBBFFL result history before any ladder/finals system is trusted to read
it (which issue #58 deliberately does not implement -- see app.round_
review's module docstring).
"""

from app.audit import ActorContext, AuditEventRepository
from app.calculations import MatchupCalculationService
from app.identity import IdentityRepository
from app.round_review import (
    RoundReviewRepository,
    attempt_correction,
    attempt_signoff,
    build_round_review,
)
from tests.round_review_helpers import Facts, full_round, progress_to_review

SCORER = ActorContext.anonymous_operator(role="scorer")
ADMIN = ActorContext.anonymous_operator(role="admin")


def test_2026_replay_calculate_review_rule_signoff_then_correct_one_matchup():
    # (1) A persisted 2026 round with five matchups and fully submitted,
    # named lineups -- app.calculations.MatchupCalculationService derives
    # the calculated scores exactly as an ordinary live round would.
    db, lifecycle, round_, entries, stats, canon = full_round(year=2026, afl_round=901)
    progress_to_review(lifecycle, round_.bbbffl_round_id)

    matchups = lifecycle.list_matchups(round_.bbbffl_round_id)
    focus_matchup = matchups[0]
    home_entry = focus_matchup.home_season_entry_id

    # One player's afl-api evidence is genuinely ambiguous this round (no
    # stat row at all, not a club bye) -- exactly the "review_required"
    # case app.participation.assess_participation exists for.
    ambiguous_canonical = canon[(home_entry, "F1")]
    replay_stats = dict(stats)
    del replay_stats[ambiguous_canonical]
    MatchupCalculationService(db, Facts(replay_stats)).calculate_round(round_.bbbffl_round_id)

    # (2) Scorer review: the round-level surface makes the blocker on the
    # affected matchup immediately visible without inspecting raw tables.
    review_repo = RoundReviewRepository(db)
    identities = IdentityRepository(db)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    assert len(review.matchups) == 5
    assert review.ready_for_signoff is False
    blocked = next(m for m in review.matchups if m.matchup_id == focus_matchup.matchup_id)
    assert any("F1" in reason for reason in blocked.blockers)
    assert all(m.eligible_for_signoff for m in review.matchups if m.matchup_id != focus_matchup.matchup_id)

    # (3) Resolution of the required ruling: the scorer confirms the DNP
    # and assigns the Interchange to cover it, both attributed and
    # audited.
    v = review_repo.record_dnp_ruling(
        focus_matchup.matchup_id,
        home_entry,
        "F1",
        True,
        expected_review_version=blocked.review_version,
        actor=SCORER,
        reason="2026 replay: confirmed unavailable, no afl-api stat row",
    )
    review_repo.record_interchange_ruling(
        focus_matchup.matchup_id,
        home_entry,
        "F1",
        expected_review_version=v,
        actor=SCORER,
        reason="2026 replay: cover F1 with the interchange",
    )
    ready_review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    assert ready_review.ready_for_signoff is True

    # (4) Full-round sign-off, atomically, across all five matchups.
    published = attempt_signoff(
        lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="2026 replay round sign-off"
    )
    assert published.state == "final"

    # (5) Effective official results: every matchup has exactly one
    # official version, frozen with the inputs that produced it, and the
    # ruling/interchange decision is visible in that frozen snapshot.
    for matchup in matchups:
        history = lifecycle.result_history(matchup.matchup_id)
        assert [h.version for h in history] == [1]
        assert lifecycle.get_matchup(matchup.matchup_id).effective_official_version == 1
    focus_official_v1 = lifecycle.effective_result(focus_matchup.matchup_id)
    frozen_home = focus_official_v1.input_snapshot["home"]
    frozen_f1 = next(slot for slot in frozen_home["slots"] if slot["slot"] == "F1")
    assert frozen_f1["dnp_ruling"] is True
    assert frozen_home["interchange"]["target_position"] == "F1"

    # (6) Reopen/correction of at least one matchup: a post-publication
    # transcription correction is discovered for the focus matchup only --
    # every other matchup's official result is untouched.
    review_repo.record_override(
        focus_matchup.matchup_id,
        home_entry,
        "F2",
        123.0,
        None,
        "2026 replay: post-publication transcription correction",
        expected_review_version=lifecycle.get_matchup(focus_matchup.matchup_id).review_version,
        actor=ADMIN,
    )
    corrected = attempt_correction(
        lifecycle,
        review_repo,
        identities,
        focus_matchup.matchup_id,
        actor=ADMIN,
        reason="2026 replay: correct F2 transcription error",
    )

    # (7) Publication of a replacement official version: version 1 is
    # preserved unchanged, version 2 is now effective, and every other
    # matchup's history is completely unaffected.
    assert corrected.version == 2
    history = lifecycle.result_history(focus_matchup.matchup_id)
    assert [h.version for h in history] == [1, 2]
    assert history[0].home_score == focus_official_v1.home_score
    assert history[0].input_snapshot == focus_official_v1.input_snapshot
    assert lifecycle.get_matchup(focus_matchup.matchup_id).effective_official_version == 2
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "final"
    for matchup in matchups[1:]:
        assert [h.version for h in lifecycle.result_history(matchup.matchup_id)] == [1]

    # The complete finalisation/correction sequence is independently
    # readable from the audit trail, distinct from the official-result
    # versions themselves (issue #58 requirement 11).
    events = AuditEventRepository(db).list_events(entity_type="competition.matchup", entity_id=focus_matchup.matchup_id)
    assert [e.action for e in events] == ["competition.result.published", "competition.result.corrected"]
    assert events[-1].reason == "2026 replay: correct F2 transcription error"
