"""Issue #190: Steve's confirmed correction/rewind policy.

1. The original official-result version, pairing and elimination record are
   preserved -- never destructively overwritten.
2. A corrected effective result is produced through the normal audited
   correction boundary (`CompetitionLifecycleRepository.correct_matchup_result`,
   already finals-compatible under #197's chosen path).
3. If the immediately downstream finals week has no downstream play state,
   `rewind_bracket` may supersede the affected pairing/elimination and
   regenerate them from the corrected winner/loser.
4. Preview-before-apply, actor/reason audit provenance.
5. No automatic recursion through already-played downstream finals.

If any downstream play state exists (an authoritative lineup submission, a
genuinely locked position, a ruling/adjudication/override, a persisted
calculation, or a published official result), `rewind_bracket` fails closed
and reports the affected artifacts -- it never invalidates/replays them."""

import pytest

from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import (
    DownstreamPlayStateError,
    FinalsBracketAdvanceStateError,
    FinalsBracketRepository,
    StaleFinalsResultError,
)
from app.finals_preflight import open_finals_week
from tests.finals_helpers import (
    accept_week_mapping,
    build_finals_ready_season,
    correct_official_result,
    seed_official_result,
)

ACTOR = ActorContext.anonymous_operator("test")


def _bracket_with_mappings(year):
    built = build_finals_ready_season(year=year)
    repo = FinalsBracketRepository(built["database"])
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="bracket for rewind tests",
    )["bracket"]
    for week in (1, 2, 3, 4):
        round_id = repo.get_week_round_id(bracket.bracket_id, week)
        accept_week_mapping(built["database"], round_id, year=year, afl_round_id=9000 + week)
    return built, bracket, repo


def _week1_to_week2(built, repo, bracket, *, qf_result, ef_result):
    open_finals_week(built["database"], bracket.bracket_id, 1, actor=ACTOR)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(built["database"], pairings["qf"].matchup_id, *qf_result)
    seed_official_result(built["database"], pairings["ef"].matchup_id, *ef_result)
    repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="advance from week 1")
    return pairings


def test_rewind_preview_never_mutates_and_reports_no_change_when_unchanged():
    built, bracket, repo = _bracket_with_mappings(2400)
    _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    before = repo.list_pairings(bracket.bracket_id, week_number=2, include_superseded=True)

    report = repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="preview only", apply=False)
    assert report["no_change_needed"]
    assert not report["blocked"]

    after = repo.list_pairings(bracket.bracket_id, week_number=2, include_superseded=True)
    assert before == after  # a preview never mutates anything


def test_rewind_supersedes_pairing_and_elimination_when_no_downstream_play_state():
    built, bracket, repo = _bracket_with_mappings(2401)
    entries = built["entries"]
    week1_pairings = _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    old_ss2 = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "second_semi")
    old_fs = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "first_semi")
    old_elim = next(e for e in repo.list_eliminations(bracket.bracket_id) if e.stage == "week1_elimination_final")

    # Flip the QF result: away (seed3) now wins instead of home (seed2).
    correct_official_result(
        built["database"], week1_pairings["qf"].matchup_id, 70, 100, reason="scorer correction: QF result flipped"
    )

    preview = repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="preview after correction", apply=False)
    assert not preview["no_change_needed"]
    assert not preview["blocked"]

    applied = repo.rewind_bracket(
        bracket.bracket_id, 1, actor=ACTOR, reason="apply rewind after QF correction", apply=True
    )
    assert not applied["blocked"]
    assert applied["audit_event_id"]

    new_ss2 = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "second_semi")
    new_fs = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "first_semi")
    assert new_ss2.away_season_entry_id == entries[2].season_entry_id  # new QF winner = seed3
    assert new_fs.home_season_entry_id == entries[1].season_entry_id  # new QF loser = seed2
    # EF's own winner (seed5) is unaffected by a QF-only correction.
    assert new_fs.away_season_entry_id == old_fs.away_season_entry_id

    # Old history is preserved, not deleted, and explicitly marked superseded.
    all_pairings = repo.list_pairings(bracket.bracket_id, week_number=2, include_superseded=True)
    old_ss2_row = next(p for p in all_pairings if p.pairing_id == old_ss2.pairing_id)
    old_fs_row = next(p for p in all_pairings if p.pairing_id == old_fs.pairing_id)
    assert old_ss2_row.status == "superseded"
    assert old_ss2_row.superseded_by_pairing_id == new_ss2.pairing_id
    assert old_fs_row.status == "superseded"
    assert old_fs_row.superseded_by_pairing_id == new_fs.pairing_id

    # The elimination this correction did NOT touch (EF's own) is untouched.
    unaffected_elim = next(
        e for e in repo.list_eliminations(bracket.bracket_id) if e.stage == "week1_elimination_final"
    )
    assert unaffected_elim.elimination_id == old_elim.elimination_id
    assert unaffected_elim.status == "active"


def test_rewind_supersedes_the_elimination_tied_to_the_corrected_match():
    """A correction that flips the Elimination Final's own winner/loser must
    also produce a consistent superseded elimination record -- never left
    naming the original, now-incorrect loser (issue #190 acceptance
    criterion)."""
    built, bracket, repo = _bracket_with_mappings(2402)
    entries = built["entries"]
    week1_pairings = _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    old_elim = next(e for e in repo.list_eliminations(bracket.bracket_id) if e.stage == "week1_elimination_final")
    assert old_elim.season_entry_id == entries[3].season_entry_id  # seed4 (EF home) originally lost

    correct_official_result(
        built["database"], week1_pairings["ef"].matchup_id, 95, 60, reason="scorer correction: EF result flipped"
    )
    applied = repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="apply after EF correction", apply=True)
    assert not applied["blocked"]

    new_elim = next(e for e in repo.list_eliminations(bracket.bracket_id) if e.stage == "week1_elimination_final")
    assert new_elim.season_entry_id == entries[4].season_entry_id  # seed5 (EF away) now loses instead
    assert new_elim.elimination_id != old_elim.elimination_id

    superseded = next(
        e
        for e in repo.list_eliminations(bracket.bracket_id, include_superseded=True)
        if e.elimination_id == old_elim.elimination_id
    )
    assert superseded.status == "superseded"
    assert superseded.superseded_by_elimination_id == new_elim.elimination_id

    # The downstream pairing and elimination can never describe two different
    # effective winners of the corrected match: First Semi's away side (the
    # EF winner) must exactly match who is now recorded as eliminated by it
    # being the *other* side.
    first_semi = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "first_semi")
    assert first_semi.away_season_entry_id == entries[3].season_entry_id  # new EF winner = seed4
    assert new_elim.season_entry_id != first_semi.away_season_entry_id


def test_rewind_fails_closed_once_downstream_official_result_is_published():
    built, bracket, repo = _bracket_with_mappings(2403)
    week1_pairings = _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    open_finals_week(built["database"], bracket.bracket_id, 2, actor=ACTOR)
    week2 = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=2)}
    seed_official_result(built["database"], week2["first_semi"].matchup_id, 120, 40)  # downstream result now exists

    correct_official_result(
        built["database"],
        week1_pairings["ef"].matchup_id,
        95,
        60,
        reason="scorer correction after downstream published",
    )

    preview = repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="preview - should be blocked", apply=False)
    assert preview["blocked"]
    blocked_slot = next(c for c in preview["pairing_changes"] if c["blocked"])
    assert blocked_slot["slot"] == "first_semi"
    assert any(a["type"] == "official_result" for a in blocked_slot["artifacts"])

    with pytest.raises(DownstreamPlayStateError) as excinfo:
        repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="apply - must raise", apply=True)
    assert excinfo.value.report["blocked"]

    # Nothing was mutated: pairing and elimination still consistently
    # describe the pre-correction (not yet re-derived) state.
    still_active = repo.list_pairings(bracket.bracket_id, week_number=2)
    assert {p.pairing_id for p in still_active} == {p.pairing_id for p in week2.values()}
    all_pairings = repo.list_pairings(bracket.bracket_id, week_number=2, include_superseded=True)
    assert all(p.status == "active" for p in all_pairings)


def test_rewind_fails_closed_once_a_lineup_is_submitted_downstream():
    built, bracket, repo = _bracket_with_mappings(2404)
    week1_pairings = _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    open_finals_week(built["database"], bracket.bracket_id, 2, actor=ACTOR)
    week2 = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=2)}
    round_id = repo.get_week_round_id(bracket.bracket_id, 2)

    from app.lineups import WeeklyLineupRepository
    from app.player_pool import OwnershipRepository, PlayerPoolRepository

    scope = (
        built["database"]
        .execute(
            "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
            "JOIN competition_stream c ON c.competition_id = r.competition_id WHERE r.bbbffl_round_id=?",
            (round_id,),
        )
        .fetchone()
    )
    ownership = OwnershipRepository(built["database"])
    ownership.configure_squad_limit(scope["season_id"], 5)
    player = PlayerPoolRepository(built["database"]).refresh_player(scope["season_id"], 990001, "Downstream Player")
    entry_id = week2["first_semi"].home_season_entry_id
    ownership.acquire(player.season_player_id, entry_id)
    lineups = WeeklyLineupRepository(built["database"])
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_id,
        entry_id,
        {"F1": player.season_player_id},
        expected_revision=0,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=1, expected_submission_version=0)

    correct_official_result(
        built["database"], week1_pairings["qf"].matchup_id, 70, 100, reason="scorer correction after lineup submitted"
    )
    preview = repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="preview - should be blocked", apply=False)
    assert preview["blocked"]
    blocked = next(c for c in preview["pairing_changes"] if c["blocked"])
    assert any(a["type"] == "lineup_submission" for a in blocked["artifacts"])


def test_rewind_never_recurses_past_the_immediately_downstream_week():
    """A correction to a Week 1 match, once Week 2's own downstream (Week 3)
    already exists, is blocked by Week 2's own play state (it must already
    be published for Week 3 to exist at all) -- `rewind_bracket(from_week=1)`
    never reaches into Week 3/4 itself; it only ever evaluates Week 2."""
    built, bracket, repo = _bracket_with_mappings(2405)
    week1_pairings = _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    open_finals_week(built["database"], bracket.bracket_id, 2, actor=ACTOR)
    week2 = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=2)}
    seed_official_result(built["database"], week2["second_semi"].matchup_id, 100, 40)
    seed_official_result(built["database"], week2["first_semi"].matchup_id, 100, 40)
    repo.advance_bracket(bracket.bracket_id, 2, actor=ACTOR, reason="advance from week 2")
    week3_before = repo.list_pairings(bracket.bracket_id, week_number=3, include_superseded=True)

    correct_official_result(
        built["database"],
        week1_pairings["qf"].matchup_id,
        70,
        100,
        reason="scorer correction reaching an already-advanced week",
    )
    with pytest.raises(DownstreamPlayStateError):
        repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="must be blocked", apply=True)

    # Week 3's own pairing (two hops downstream of the Week 1 correction) is
    # completely untouched -- rewind(from_week=1) never even looks at it.
    week3_after = repo.list_pairings(bracket.bracket_id, week_number=3, include_superseded=True)
    assert week3_before == week3_after


def test_rewind_requires_a_reason_when_applying():
    built, bracket, repo = _bracket_with_mappings(2406)
    _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    with pytest.raises(Exception, match="reason"):
        repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="", apply=True)


def test_rewind_apply_is_idempotent_against_an_already_current_derivation():
    built, bracket, repo = _bracket_with_mappings(2407)
    _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    first = repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="no-op apply", apply=True)
    assert first["no_change_needed"]
    second = repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="no-op apply again", apply=True)
    assert second["no_change_needed"]
    all_pairings = repo.list_pairings(bracket.bracket_id, week_number=2, include_superseded=True)
    assert len(all_pairings) == 2  # no superseded rows were created by the no-op applies


def test_rewind_reconciles_an_already_materialised_matchup_when_unblocked():
    """Codex review, PR #201: superseding a pairing that has already been
    opened into a real `bbbffl_matchup` (but has no downstream play state
    yet) must reconcile that same matchup's participants in place, not
    leave the new pairing's `matchup_id` NULL -- otherwise the round keeps
    exposing the old, now-incorrect participants, and the slot can never be
    re-materialised (`uq_round_matchup_order`/`uq_finals_bracket_pairing_
    matchup` would both block it)."""
    built, bracket, repo = _bracket_with_mappings(2408)
    week1_pairings = _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    open_finals_week(built["database"], bracket.bracket_id, 2, actor=ACTOR)
    old_ss2 = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "second_semi")
    old_fs = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "first_semi")
    assert old_ss2.matchup_id is not None
    assert old_fs.matchup_id is not None

    correct_official_result(
        built["database"], week1_pairings["qf"].matchup_id, 70, 100, reason="scorer correction: QF result flipped"
    )
    applied = repo.rewind_bracket(
        bracket.bracket_id, 1, actor=ACTOR, reason="reconcile materialised matchups", apply=True
    )
    assert not applied["blocked"]

    new_ss2 = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "second_semi")
    new_fs = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "first_semi")
    # The same underlying matchup rows are reused, not left NULL/orphaned.
    assert new_ss2.matchup_id == old_ss2.matchup_id
    assert new_fs.matchup_id == old_fs.matchup_id

    ss2_matchup = (
        built["database"]
        .execute(
            "SELECT home_season_entry_id, away_season_entry_id FROM bbbffl_matchup WHERE matchup_id=?",
            (new_ss2.matchup_id,),
        )
        .fetchone()
    )
    assert ss2_matchup["home_season_entry_id"] == new_ss2.home_season_entry_id
    assert ss2_matchup["away_season_entry_id"] == new_ss2.away_season_entry_id
    fs_matchup = (
        built["database"]
        .execute(
            "SELECT home_season_entry_id, away_season_entry_id FROM bbbffl_matchup WHERE matchup_id=?",
            (new_fs.matchup_id,),
        )
        .fetchone()
    )
    assert fs_matchup["home_season_entry_id"] == new_fs.home_season_entry_id
    assert fs_matchup["away_season_entry_id"] == new_fs.away_season_entry_id

    # The old (superseded) pairing rows no longer reference the matchup --
    # a plain UNIQUE constraint on matchup_id would otherwise be violated by
    # the new pairing pointing at the same row.
    old_rows = repo.list_pairings(bracket.bracket_id, week_number=2, include_superseded=True)
    superseded_ss2 = next(p for p in old_rows if p.pairing_id == old_ss2.pairing_id)
    superseded_fs = next(p for p in old_rows if p.pairing_id == old_fs.pairing_id)
    assert superseded_ss2.status == "superseded"
    assert superseded_ss2.matchup_id is None
    assert superseded_fs.status == "superseded"
    assert superseded_fs.matchup_id is None

    # The round remains genuinely playable afterwards: re-running the
    # preflight/open-round adapter is a safe idempotent no-op (nothing left
    # to materialise), never a constraint violation.
    reopened = repo.open_finals_week(bracket.bracket_id, 2, actor=ACTOR)
    assert reopened["already_open"]
    matchup_count = (
        built["database"]
        .execute(
            "SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?",
            (repo.get_week_round_id(bracket.bracket_id, 2),),
        )
        .fetchone()
    )
    assert matchup_count["n"] == 2  # no orphaned extra matchup rows were created


def test_open_finals_week_never_materialises_a_pairing_superseded_mid_open():
    """Codex review, PR #201: `open_finals_week` first reads a week's
    *active* pairings, then materialises each one under its own lock. If a
    concurrent rewind supersedes one of those exact pairings in between --
    unrealistic in a single-threaded test, but reproduced directly here by
    calling the internal `_materialise_pairing` step against a pairing
    object captured *before* a rewind superseded it -- the stale object's
    (now-incorrect) participants must never be attached to a fresh
    `bbbffl_matchup`, and the caller must be told to reload and retry
    against the current active pairing instead."""
    built, bracket, repo = _bracket_with_mappings(2409)
    week1_pairings = _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    stale_second_semi = next(
        p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "second_semi"
    )

    # Supersede 'second_semi' (still unmaterialised) via a QF correction,
    # *after* capturing the pairing object above but before it is ever
    # passed to `_materialise_pairing` -- exactly the race window Codex
    # identified between `open_finals_week`'s read and its per-pairing lock.
    correct_official_result(
        built["database"], week1_pairings["qf"].matchup_id, 70, 100, reason="races open_finals_week's own read"
    )
    repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="supersede before materialisation", apply=True)

    round_id = repo.get_week_round_id(bracket.bracket_id, 2)
    # `_materialise_pairing` is only ever really invoked from inside
    # `open_finals_week`, which always creates the round's lifecycle row
    # first (Codex review, PR #201's lifecycle-then-pairing lock order) --
    # match that precondition here so this direct call exercises the same
    # state a real race would find it in, rather than the unrelated "no
    # finals lifecycle to attach to" error a truly nonexistent round would
    # raise.
    CompetitionLifecycleRepository(built["database"]).create_non_ordinary_round(
        round_id, actor=ACTOR, reason="lifecycle row created before materialisation, matching open_finals_week"
    )
    materialised = repo._materialise_pairing(round_id, stale_second_semi, actor=ACTOR, reason="stale attempt")
    assert materialised is False

    # Nothing was written for the stale (superseded) pairing or its old
    # participants -- no matchup exists for this round at all yet.
    matchup_count = (
        built["database"]
        .execute("SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?", (round_id,))
        .fetchone()
    )
    assert matchup_count["n"] == 0
    still_null = repo.list_pairings(bracket.bracket_id, week_number=2, include_superseded=True)
    assert all(p.matchup_id is None for p in still_null)

    # A fresh open_finals_week call, unaffected by the artificial staleness
    # above (its own read is always current), correctly materialises the
    # *replacement* active pairing.
    reopened = open_finals_week(built["database"], bracket.bracket_id, 2, actor=ACTOR)
    assert reopened["round"].state == "open"
    new_second_semi = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "second_semi")
    assert new_second_semi.matchup_id is not None
    assert new_second_semi.away_season_entry_id != stale_second_semi.away_season_entry_id


def test_open_finals_week_fails_closed_when_its_own_read_races_a_rewind(monkeypatch):
    """The end-to-end shape of the same race: `open_finals_week` reads a
    week's active pairings, then a concurrent rewind supersedes one before
    materialisation reaches it. Monkeypatching `list_pairings` to return
    that now-stale snapshot (exactly what a real interleaving would hand
    `open_finals_week`) proves it fails closed instead of silently
    materialising stale participants."""
    built, bracket, repo = _bracket_with_mappings(2410)
    week1_pairings = _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))
    stale_pairings = repo.list_pairings(bracket.bracket_id, week_number=2)

    correct_official_result(
        built["database"], week1_pairings["qf"].matchup_id, 70, 100, reason="races open_finals_week's own read"
    )
    repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="supersede before materialisation", apply=True)

    real_list_pairings = FinalsBracketRepository.list_pairings
    call_count = {"n": 0}

    def stale_once(self, bracket_id, *, week_number=None, include_superseded=False):
        # The first call is `build_finals_week_preflight`'s own readiness
        # check; the second is the repository's `open_finals_week` reading
        # what it will actually materialise -- exactly the read a real
        # concurrent rewind would race.
        if week_number == 2 and not include_superseded:
            call_count["n"] += 1
            if call_count["n"] == 2:
                return stale_pairings
        return real_list_pairings(self, bracket_id, week_number=week_number, include_superseded=include_superseded)

    monkeypatch.setattr(FinalsBracketRepository, "list_pairings", stale_once)

    with pytest.raises(FinalsBracketAdvanceStateError, match="superseded by a concurrent rewind"):
        open_finals_week(built["database"], bracket.bracket_id, 2, actor=ACTOR)

    round_id = repo.get_week_round_id(bracket.bracket_id, 2)
    assert (
        built["database"]
        .execute("SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?", (round_id,))
        .fetchone()["n"]
        == 0
    )

    reopened = open_finals_week(built["database"], bracket.bracket_id, 2, actor=ACTOR)
    assert reopened["round"].state == "open"


def test_rewind_preview_reports_expected_versions_and_apply_detects_a_stale_one():
    """Codex review, PR #201: unlike `advance_bracket`, `rewind_bracket`
    accepted no `expected_versions` at all -- an operator's `apply` always
    derived from whatever the prerequisite results happened to be *right
    now*, silently superseding pairings from a correction that landed after
    their preview rather than rejecting the now-stale preview. A preview's
    own `expected_versions` (mirroring `preview_advance_bracket`) must be
    accepted and re-checked under the same prerequisite-matchup lock."""
    built, bracket, repo = _bracket_with_mappings(2411)
    week1_pairings = _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))

    # First correction: makes a rewind non-trivial (something to preview).
    correct_official_result(built["database"], week1_pairings["qf"].matchup_id, 70, 100, reason="first QF correction")
    preview = repo.rewind_bracket(
        bracket.bracket_id, 1, actor=ACTOR, reason="preview before second correction", apply=False
    )
    assert not preview["no_change_needed"]
    expected_versions = preview["expected_versions"]
    assert week1_pairings["ef"].matchup_id in expected_versions

    # Second correction lands on the *other* prerequisite (EF) after the
    # preview above captured its version -- an apply using that stale
    # preview must never proceed silently.
    correct_official_result(
        built["database"], week1_pairings["ef"].matchup_id, 60, 90, reason="second correction races the preview"
    )

    with pytest.raises(StaleFinalsResultError):
        repo.rewind_bracket(
            bracket.bracket_id,
            1,
            actor=ACTOR,
            reason="apply against a now-stale preview",
            apply=True,
            expected_versions=expected_versions,
        )

    # Nothing was superseded by the aborted apply.
    all_pairings = repo.list_pairings(bracket.bracket_id, week_number=2, include_superseded=True)
    assert all(p.status == "active" for p in all_pairings)

    # A fresh preview/apply (current versions) still works normally.
    fresh_versions = repo.rewind_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="fresh preview", apply=False)[
        "expected_versions"
    ]
    applied = repo.rewind_bracket(
        bracket.bracket_id,
        1,
        actor=ACTOR,
        reason="apply against a fresh preview",
        apply=True,
        expected_versions=fresh_versions,
    )
    assert not applied["blocked"]


def test_materialise_pairing_locks_the_round_lifecycle_row_before_the_pairing_row(monkeypatch):
    """Codex review, PR #201: `_downstream_play_state` (called from
    `rewind_bracket`, in the same transaction that later supersedes a
    pairing row) locks the round's `bbbffl_round_lifecycle` row before it
    ever touches `finals_bracket_pairing`. `_materialise_pairing` used to
    acquire the opposite order (pairing, then lifecycle) -- two concurrent
    transactions each already holding the lock the other needs next can
    only be resolved by PostgreSQL aborting one with a deadlock error,
    instead of the intended retryable stale-pairing refusal. Pins the fix:
    `_materialise_pairing` must acquire the identical lifecycle-then-
    pairing order `_downstream_play_state` uses, removing the cycle."""
    from app.db import _TransactionConnection

    built, bracket, repo = _bracket_with_mappings(2412)
    _week1_to_week2(built, repo, bracket, qf_result=(100, 50), ef_result=(50, 100))

    locked_order: list[str] = []
    recording = {"on": False}
    real_execute = _TransactionConnection.execute

    def recording_execute(self, statement, parameters=()):
        if recording["on"]:
            if "FROM bbbffl_round_lifecycle" in statement:
                locked_order.append("lifecycle")
            elif "FROM finals_bracket_pairing WHERE pairing_id" in statement:
                locked_order.append("pairing")
        return real_execute(self, statement, parameters)

    monkeypatch.setattr(_TransactionConnection, "execute", recording_execute)

    real_materialise = FinalsBracketRepository._materialise_pairing

    def recording_materialise(self, round_id, pairing, *, actor, reason):
        recording["on"] = True
        try:
            return real_materialise(self, round_id, pairing, actor=actor, reason=reason)
        finally:
            recording["on"] = False

    monkeypatch.setattr(FinalsBracketRepository, "_materialise_pairing", recording_materialise)

    open_finals_week(built["database"], bracket.bracket_id, 2, actor=ACTOR)

    # Week 2 has two non-bye slots (second_semi, first_semi), each
    # materialised via its own `_materialise_pairing` transaction -- every
    # one of them must lock lifecycle strictly before pairing.
    assert locked_order == ["lifecycle", "pairing", "lifecycle", "pairing"]
