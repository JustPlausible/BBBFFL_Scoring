"""Issue #242: provisional player creation, Coach nomination and canonical
afl-api reconciliation. Domain-level coverage -- `tests/
test_provisional_players_api.py` covers the HTTP/role boundary."""

import pytest

from app.audit import ActorContext, AuditEventRepository
from app.db import transaction
from app.draft import DraftRepository
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.provisional_players import (
    InvalidReconciliationTargetError,
    NominationStateError,
    NotProvisionalError,
    PlayerNominationRepository,
    ProvisionalPlayerError,
    ProvisionalPlayerRepository,
    ReconciliationConflictError,
    detect_candidates,
)
from app.season import SeasonCompletedError, SeasonRepository
from tests.db_helpers import migrated_connection

SCORER = ActorContext.anonymous_operator("scorer")
COACH = ActorContext.coach("coach-1")


def setup_domain(limit=5):
    db = migrated_connection()
    seasons = SeasonRepository(db)
    identities = IdentityRepository(db)
    season = seasons.create_season(2027, "2027")
    coaches = [identities.create_coach(name) for name in ("One", "Two")]
    entries = [
        identities.create_entry(season.season_id, f"licence-{i}", coach.coach_id, f"Team {i}")
        for i, coach in enumerate(coaches)
    ]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(season.season_id, limit)
    return db, season, entries


def _create(db, season_id, *, given="Jordan", family="Newrecruit", note="Verified via club website squad list"):
    return ProvisionalPlayerRepository(db).create(
        season_id,
        display_name=f"{given} {family}",
        given_name=given,
        family_name=family,
        note=note,
        actor=SCORER,
        reason="verified missing player",
    )


# -- Nomination ---------------------------------------------------------------


def test_coach_can_submit_a_nomination_and_it_is_visible_to_scorer():
    db, season, entries = setup_domain()
    nominations = PlayerNominationRepository(db)
    nomination = nominations.submit(
        season.season_id,
        entries[0].season_entry_id,
        "Jordan Newrecruit",
        afl_club_note="Plays for a club not yet in afl-api",
        note="Saw him listed on the club website",
        actor=COACH,
    )
    assert nomination.status == "pending"
    pending = nominations.list_for_season(season.season_id, status="pending")
    assert [n.nomination_id for n in pending] == [nomination.nomination_id]


def test_nomination_requires_a_player_name():
    db, season, entries = setup_domain()
    with pytest.raises(ValueError):
        PlayerNominationRepository(db).submit(season.season_id, entries[0].season_entry_id, "   ", actor=COACH)


def test_nomination_rejects_an_entry_from_a_different_season():
    db, season, entries = setup_domain()
    other = SeasonRepository(db).create_season(2028, "2028")
    other_identities = IdentityRepository(db)
    other_coach = other_identities.create_coach("Foreign")
    other_entry = other_identities.create_entry(
        other.season_id, "foreign-licence", other_coach.coach_id, "Foreign Team"
    )
    with pytest.raises(KeyError):
        PlayerNominationRepository(db).submit(season.season_id, other_entry.season_entry_id, "Someone", actor=COACH)


def test_dismissing_a_nomination_requires_a_reason_and_is_terminal():
    db, season, entries = setup_domain()
    nominations = PlayerNominationRepository(db)
    nomination = nominations.submit(season.season_id, entries[0].season_entry_id, "Jordan Newrecruit", actor=COACH)
    with pytest.raises(ValueError):
        nominations.dismiss(season.season_id, nomination.nomination_id, actor=SCORER, reason="")
    dismissed = nominations.dismiss(
        season.season_id,
        nomination.nomination_id,
        actor=SCORER,
        reason="Already in afl-api under a different spelling",
    )
    assert dismissed.status == "dismissed"
    with pytest.raises(NominationStateError):
        nominations.dismiss(season.season_id, nomination.nomination_id, actor=SCORER, reason="again")


# -- Provisional creation -------------------------------------------------------


def test_creating_a_provisional_player_requires_no_canonical_id_and_no_fake_one_is_accepted():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id)
    assert player.canonical_player_id is None
    assert player.was_provisional is True
    assert player.eligible is True
    assert player.provisional_note == "Verified via club website squad list"


@pytest.mark.parametrize(
    "field",
    ["display_name", "given_name", "family_name", "note"],
)
def test_creation_requires_every_identifying_field(field):
    db, season, _entries = setup_domain()
    kwargs = dict(display_name="Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit", note="Verified")
    kwargs[field] = "   "
    with pytest.raises(ValueError):
        ProvisionalPlayerRepository(db).create(season.season_id, actor=SCORER, **kwargs)


def test_creation_from_a_nomination_resolves_it():
    db, season, entries = setup_domain()
    nominations = PlayerNominationRepository(db)
    nomination = nominations.submit(season.season_id, entries[0].season_entry_id, "Jordan Newrecruit", actor=COACH)
    player = ProvisionalPlayerRepository(db).create(
        season.season_id,
        display_name="Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        note="Confirmed with the coach and the club",
        actor=SCORER,
        nomination_id=nomination.nomination_id,
    )
    resolved = nominations.get(nomination.nomination_id)
    assert resolved.status == "created"
    assert resolved.resulting_season_player_id == player.season_player_id


def test_creation_refused_once_season_is_completed():
    db, season, _entries = setup_domain()
    with transaction(db) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season.season_id,))
    with pytest.raises(SeasonCompletedError):
        _create(db, season.season_id)


# -- Pool integration / draft --------------------------------------------------


def test_provisional_player_enters_the_selectable_and_available_pool():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id)
    pool = PlayerPoolRepository(db)
    assert player.season_player_id in {p.season_player_id for p in pool.list_selectable(season.season_id)}
    assert player.season_player_id in {p.season_player_id for p in pool.list_available(season.season_id)}
    browsed = {item.season_player_id: item for item in pool.browse(season.season_id)}
    assert browsed[player.season_player_id].is_provisional is True


def test_coach_can_draft_a_provisional_player_through_the_normal_draft_workflow():
    db, season, entries = setup_domain()
    player = _create(db, season.season_id)
    draft = DraftRepository(db)
    draft.accept_order(season.season_id, [e.season_entry_id for e in entries], actor=SCORER)
    pick = draft.execute_pick(season.season_id, entries[0].season_entry_id, player.season_player_id, actor=COACH)
    assert pick.selected_season_player_id == player.season_player_id
    squad = OwnershipRepository(db).current_squad(entries[0].season_entry_id)
    assert player.season_player_id in {item.season_player_id for item in squad}


# -- Candidate detection --------------------------------------------------------


def test_creation_immediately_detects_an_already_existing_canonical_duplicate():
    """Codex review on PR #258 (P2, seventh round): if a canonical pool row
    already matches the new provisional player's structured name *before*
    creation, waiting for the next `refresh_player_pool`/`detect_candidates`
    call to flag it would leave both identities eligible/draftable in the
    meantime. `create` must run the same detection immediately, in the same
    transaction as the insert."""
    db, season, _entries = setup_domain()
    pool = PlayerPoolRepository(db)
    canonical = pool.refresh_player(
        season.season_id, 9801, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")

    assert pool.get_by_id(canonical.season_player_id).eligible is False
    candidate = db.execute(
        "SELECT status FROM provisional_match_candidate WHERE season_player_id=? AND canonical_player_id=?",
        (player.season_player_id, 9801),
    ).fetchone()
    assert candidate is not None
    assert candidate["status"] == "pending"

    # The newly-created provisional player itself is unaffected -- only the
    # pre-existing canonical duplicate is quarantined.
    assert pool.get_by_id(player.season_player_id).eligible is True


def test_creation_quarantines_the_provisional_row_when_the_matching_canonical_is_already_owned():
    """Codex review on PR #258 (P1, ninth round): quarantining an
    already-owned canonical match protects nothing -- `list_available`
    already excludes any row with an open ownership period regardless of
    `eligible`. The actual duplicate-ownership risk is the *provisional*
    row itself staying eligible and independently draftable as the same
    real person; detection (run here via `create`) must quarantine that
    row instead of the pointless canonical one."""
    db, season, entries = setup_domain()
    pool = PlayerPoolRepository(db)
    canonical = pool.refresh_player(
        season.season_id, 9910, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    OwnershipRepository(db).acquire(canonical.season_player_id, entries[0].season_entry_id)

    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")

    assert pool.get_by_id(player.season_player_id).eligible is False
    candidate = db.execute(
        "SELECT status FROM provisional_match_candidate WHERE season_player_id=? AND canonical_player_id=?",
        (player.season_player_id, 9910),
    ).fetchone()
    assert candidate is not None
    assert candidate["status"] == "pending"

    # Once a Scorer rules out the pairing, the provisional row is freed --
    # no other pending owned-match candidate remains for it.
    ProvisionalPlayerRepository(db).reject_candidate(
        season.season_id, player.season_player_id, 9910, actor=SCORER, reason="Different person"
    )
    assert pool.get_by_id(player.season_player_id).eligible is True


def test_no_match_leaves_player_plainly_provisional():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id)
    detected = detect_candidates(db, season.season_id, actor=ActorContext.system())
    assert detected == 0
    outstanding = ProvisionalPlayerRepository(db).list_outstanding(season.season_id)
    [row] = [o for o in outstanding if o.player.season_player_id == player.season_player_id]
    assert row.candidates == ()
    assert row.has_candidate is False


def test_one_plausible_candidate_is_surfaced_and_quarantines_the_duplicate():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    canonical = pool.refresh_player(
        season.season_id, 9001, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    detected = detect_candidates(db, season.season_id, actor=ActorContext.system())
    assert detected == 1

    outstanding = ProvisionalPlayerRepository(db).list_outstanding(season.season_id)
    [row] = [o for o in outstanding if o.player.season_player_id == player.season_player_id]
    assert [c.canonical_player_id for c in row.candidates] == [9001]
    assert row.has_candidate is True
    assert row.is_ambiguous is False

    # The freshly-arrived canonical duplicate must not be draftable while
    # the match is unresolved.
    refreshed = pool.get_by_id(canonical.season_player_id)
    assert refreshed.eligible is False
    assert canonical.season_player_id not in {p.season_player_id for p in pool.list_available(season.season_id)}


def test_detection_never_automatically_reconciles():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    pool.refresh_player(season.season_id, 9002, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit")
    detect_candidates(db, season.season_id, actor=ActorContext.system())
    assert pool.get_by_id(player.season_player_id).canonical_player_id is None


def test_multiple_plausible_candidates_are_ambiguous():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    pool.refresh_player(season.season_id, 9101, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit")
    pool.refresh_player(season.season_id, 9102, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit")
    detected = detect_candidates(db, season.season_id, actor=ActorContext.system())
    assert detected == 2
    outstanding = ProvisionalPlayerRepository(db).list_outstanding(season.season_id)
    [row] = [o for o in outstanding if o.player.season_player_id == player.season_player_id]
    assert row.is_ambiguous is True
    assert sorted(c.canonical_player_id for c in row.candidates) == [9101, 9102]


def test_rejected_candidate_restores_eligibility_and_is_never_resuggested():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    pool.refresh_player(season.season_id, 9003, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit")
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    provisional = ProvisionalPlayerRepository(db)
    provisional.reject_candidate(
        season.season_id, player.season_player_id, 9003, actor=SCORER, reason="Different person, confirmed by club"
    )

    assert pool.get_by_id(pool.get(season.season_id, 9003).season_player_id).eligible is True
    outstanding = provisional.list_outstanding(season.season_id)
    [row] = [o for o in outstanding if o.player.season_player_id == player.season_player_id]
    assert row.has_candidate is False  # rejected candidates are not "pending"

    # Re-running detection must not resurrect the rejected pair.
    detect_candidates(db, season.season_id, actor=ActorContext.system())
    outstanding = provisional.list_outstanding(season.season_id)
    [row] = [o for o in outstanding if o.player.season_player_id == player.season_player_id]
    assert row.has_candidate is False


def test_rejecting_a_candidate_never_makes_an_already_ineligible_row_draftable():
    """Codex review on PR #258 (P1, second round): a canonical row that was
    already `eligible=False` for a reason unrelated to detection must stay
    `False` after its candidate suggestion is rejected -- releasing
    quarantine restores the row's *recorded prior* eligibility, never an
    unconditional `True`."""
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    canonical = pool.refresh_player(
        season.season_id, 9011, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit", eligible=False
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())
    # Quarantine is a no-op here -- it was already ineligible.
    assert pool.get_by_id(canonical.season_player_id).eligible is False

    provisional = ProvisionalPlayerRepository(db)
    provisional.reject_candidate(
        season.season_id, player.season_player_id, 9011, actor=SCORER, reason="Different person"
    )

    assert pool.get_by_id(canonical.season_player_id).eligible is False


def test_prior_eligibility_is_preserved_across_separate_detection_runs():
    """Codex review on PR #258 (P2, third round): a canonical row quarantined
    in one `detect_candidates` run must still record its *true* original
    eligibility for a second provisional player matched to it in a *later*
    run -- not the already-quarantined value that run would otherwise read
    back live from the pool row."""
    db, season, _entries = setup_domain()
    player_a = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    canonical = pool.refresh_player(
        season.season_id, 9012, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())  # run 1: quarantines 9012
    assert pool.get_by_id(canonical.season_player_id).eligible is False

    player_b = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    detect_candidates(db, season.season_id, actor=ActorContext.system())  # run 2: matches player_b too

    provisional = ProvisionalPlayerRepository(db)
    provisional.reject_candidate(season.season_id, player_a.season_player_id, 9012, actor=SCORER, reason="Not A")
    # Still quarantined -- player_b's candidate on 9012 is still pending.
    assert pool.get_by_id(canonical.season_player_id).eligible is False

    provisional.reject_candidate(season.season_id, player_b.season_player_id, 9012, actor=SCORER, reason="Not B")
    # Both rejected -- restored to its true original (eligible) state.
    assert pool.get_by_id(canonical.season_player_id).eligible is True


def test_detect_candidates_derives_eligibility_from_the_live_row_once_no_pending_candidate_remains():
    """Codex review on PR #258 (P2, fourth round): once every earlier
    candidate naming a canonical id has been rejected (so none is pending
    any more), a newly detected candidate for that same id must derive
    `restore_eligible_on_release` from the pool row's *current* eligibility
    -- not a rejected candidate's recorded value, which can be stale by the
    time of the new detection run."""
    db, season, _entries = setup_domain()
    player_a = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    canonical = pool.refresh_player(
        season.season_id, 9601, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())  # candidate A: records True (row eligible)

    provisional = ProvisionalPlayerRepository(db)
    provisional.reject_candidate(season.season_id, player_a.season_player_id, 9601, actor=SCORER, reason="Not A")
    # Released -- no pending candidate remains for 9601.
    assert pool.get_by_id(canonical.season_player_id).eligible is True

    # Made ineligible for an unrelated reason (a fresh afl-api refresh),
    # independent of the now-rejected candidate suggestion.
    pool.refresh_player(
        season.season_id,
        9601,
        "Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        eligible=False,
    )

    player_b = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    detect_candidates(
        db, season.season_id, actor=ActorContext.system()
    )  # candidate B: must derive False, not A's stale True

    provisional.reject_candidate(season.season_id, player_b.season_player_id, 9601, actor=SCORER, reason="Not B")
    # Must remain ineligible -- A's stale recorded True must not be reapplied.
    assert pool.get_by_id(canonical.season_player_id).eligible is False


def test_detect_candidates_is_refused_once_season_is_completed():
    db, season, _entries = setup_domain()
    _create(db, season.season_id, given="Jordan", family="Newrecruit")
    PlayerPoolRepository(db).refresh_player(
        season.season_id, 9013, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    with transaction(db) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season.season_id,))
    with pytest.raises(SeasonCompletedError):
        detect_candidates(db, season.season_id, actor=ActorContext.system())


def test_rejecting_an_unknown_candidate_pair_fails():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id)
    with pytest.raises(KeyError):
        ProvisionalPlayerRepository(db).reject_candidate(
            season.season_id, player.season_player_id, 999999, actor=SCORER, reason="no"
        )


def test_rejecting_one_candidate_does_not_release_a_canonical_row_still_pending_for_another_provisional_player():
    """Codex review on PR #258 (P1): two provisional players can plausibly
    share a name and both match the same canonical row. Rejecting the pair
    for one must not make the canonical row draftable while it is still an
    unresolved suggestion for the other."""
    db, season, _entries = setup_domain()
    player_a = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    player_b = _create(db, season.season_id, given="Jordan2", family="Newrecruit2")
    with transaction(db) as conn:
        conn.execute(
            "UPDATE season_player_pool SET given_name='Jordan', family_name='Newrecruit' WHERE season_player_id=?",
            (player_b.season_player_id,),
        )
    pool = PlayerPoolRepository(db)
    canonical = pool.refresh_player(
        season.season_id, 9005, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())
    assert pool.get_by_id(canonical.season_player_id).eligible is False

    provisional = ProvisionalPlayerRepository(db)
    provisional.reject_candidate(
        season.season_id, player_a.season_player_id, 9005, actor=SCORER, reason="Not the same person as A"
    )

    # Still quarantined -- player_b's candidate suggestion for the same
    # canonical row is still pending.
    assert pool.get_by_id(canonical.season_player_id).eligible is False

    provisional.reject_candidate(
        season.season_id, player_b.season_player_id, 9005, actor=SCORER, reason="Not the same person as B either"
    )
    # Now that neither provisional player has a pending claim on it, it is
    # released.
    assert pool.get_by_id(canonical.season_player_id).eligible is True


def test_reconciliation_does_not_release_a_losing_candidate_still_pending_for_another_provisional_player():
    """As above, but for `reconcile`'s own losing-candidate cleanup loop."""
    db, season, _entries = setup_domain()
    player_a = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    player_b = _create(db, season.season_id, given="Jordan2", family="Newrecruit2")
    with transaction(db) as conn:
        conn.execute(
            "UPDATE season_player_pool SET given_name='Jordan', family_name='Newrecruit' WHERE season_player_id=?",
            (player_b.season_player_id,),
        )
    pool = PlayerPoolRepository(db)
    winner = pool.refresh_player(
        season.season_id, 9006, "Jordan Newrecruit One", given_name="Jordan", family_name="Newrecruit"
    )
    shared = pool.refresh_player(
        season.season_id, 9007, "Jordan Newrecruit Two", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    provisional = ProvisionalPlayerRepository(db)
    provisional.reconcile(
        season.season_id, player_a.season_player_id, winner.season_player_id, actor=SCORER, reason="Confirmed A"
    )

    # `shared` (candidate 9007) is still a live pending suggestion for
    # player_b -- reconciling A must not release it.
    assert pool.get_by_id(shared.season_player_id).eligible is False


def test_reconciliation_does_not_reapply_a_rejected_candidates_stale_eligibility():
    """Codex review on PR #258 (P2, fourth round): `reconcile`'s
    losing-candidate cleanup loop must only release a still-*pending*
    losing candidate. A candidate already rejected (and released at
    rejection time) may have since had its canonical row's eligibility
    changed for an unrelated reason; reapplying its recorded
    `restore_eligible_on_release` at reconciliation time would silently
    undo that unrelated change."""
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    winner = pool.refresh_player(
        season.season_id, 9501, "Jordan Newrecruit One", given_name="Jordan", family_name="Newrecruit"
    )
    loser = pool.refresh_player(
        season.season_id, 9502, "Jordan Newrecruit Two", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    provisional = ProvisionalPlayerRepository(db)
    provisional.reject_candidate(season.season_id, player.season_player_id, 9502, actor=SCORER, reason="Not this one")
    # Released back to eligible -- rejection recorded restore_eligible_on_release=True.
    assert pool.get_by_id(loser.season_player_id).eligible is True

    # Made ineligible for an unrelated reason (a fresh afl-api refresh),
    # independent of the now-rejected candidate suggestion.
    pool.refresh_player(
        season.season_id,
        9502,
        "Jordan Newrecruit Two",
        given_name="Jordan",
        family_name="Newrecruit",
        eligible=False,
    )

    provisional.reconcile(
        season.season_id, player.season_player_id, winner.season_player_id, actor=SCORER, reason="Confirmed"
    )

    # The rejected candidate's stale recorded value must not be reapplied.
    assert pool.get_by_id(loser.season_player_id).eligible is False


def test_rejecting_a_candidate_is_refused_once_season_is_completed():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    pool.refresh_player(season.season_id, 9008, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit")
    detect_candidates(db, season.season_id, actor=ActorContext.system())
    with transaction(db) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season.season_id,))
    with pytest.raises(SeasonCompletedError):
        ProvisionalPlayerRepository(db).reject_candidate(
            season.season_id, player.season_player_id, 9008, actor=SCORER, reason="too late"
        )


def test_deferring_a_candidate_is_refused_once_season_is_completed():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    pool.refresh_player(season.season_id, 9009, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit")
    detect_candidates(db, season.season_id, actor=ActorContext.system())
    with transaction(db) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season.season_id,))
    with pytest.raises(SeasonCompletedError):
        ProvisionalPlayerRepository(db).defer_candidate(season.season_id, player.season_player_id, 9009, actor=SCORER)


def test_dismissing_a_nomination_is_refused_once_season_is_completed():
    db, season, entries = setup_domain()
    nominations = PlayerNominationRepository(db)
    nomination = nominations.submit(season.season_id, entries[0].season_entry_id, "Jordan Newrecruit", actor=COACH)
    with transaction(db) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season.season_id,))
    with pytest.raises(SeasonCompletedError):
        nominations.dismiss(season.season_id, nomination.nomination_id, actor=SCORER, reason="too late")


def test_reconcile_and_dismiss_reject_a_mismatched_season_id():
    """Codex review on PR #258 (P1): every mutation must bind to the caller-
    supplied `season_id`, not trust the resource id alone -- otherwise a
    Scorer authorized for season A could act on season B's resources by
    passing A's season_id in the URL alongside B's resource ids."""
    db, season_a, _entries_a = setup_domain()
    identities = IdentityRepository(db)
    season_b = SeasonRepository(db).create_season(2090, "2090 cross-season isolation check")
    coach_b = identities.create_coach("Coach B")
    entry_b = identities.create_entry(season_b.season_id, "licence-b", coach_b.coach_id, "Team B")
    player_b = _create(db, season_b.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    canonical_b = pool.refresh_player(
        season_b.season_id, 9010, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season_b.season_id, actor=ActorContext.system())
    nomination_b = PlayerNominationRepository(db).submit(
        season_b.season_id, entry_b.season_entry_id, "Someone", actor=COACH
    )

    provisional = ProvisionalPlayerRepository(db)
    with pytest.raises(KeyError):
        provisional.reconcile(
            season_a.season_id, player_b.season_player_id, canonical_b.season_player_id, actor=SCORER, reason="x"
        )
    with pytest.raises(KeyError):
        provisional.reject_candidate(season_a.season_id, player_b.season_player_id, 9010, actor=SCORER, reason="x")
    with pytest.raises(KeyError):
        provisional.defer_candidate(season_a.season_id, player_b.season_player_id, 9010, actor=SCORER)
    with pytest.raises(KeyError):
        PlayerNominationRepository(db).dismiss(season_a.season_id, nomination_b.nomination_id, actor=SCORER, reason="x")


def test_deferring_a_candidate_records_an_audit_event_without_changing_state():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    pool.refresh_player(season.season_id, 9004, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit")
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    provisional = ProvisionalPlayerRepository(db)
    provisional.defer_candidate(
        season.season_id, player.season_player_id, 9004, actor=SCORER, reason="Need more evidence"
    )

    outstanding = provisional.list_outstanding(season.season_id)
    [row] = [o for o in outstanding if o.player.season_player_id == player.season_player_id]
    assert row.has_candidate is True  # unchanged -- still pending

    events = AuditEventRepository(db).list_events(action="player_pool.provisional.candidate_deferred")
    assert len(events) == 1


# -- Reconciliation -------------------------------------------------------------


def test_reconciliation_attaches_canonical_identity_and_preserves_history():
    db, season, entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    canonical = pool.refresh_player(
        season.season_id,
        9201,
        "Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        afl_team_id=5,
        afl_team_name="Some Club",
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    draft = DraftRepository(db)
    draft.accept_order(season.season_id, [e.season_entry_id for e in entries], actor=SCORER)
    draft.execute_pick(season.season_id, entries[0].season_entry_id, player.season_player_id, actor=COACH)

    provisional = ProvisionalPlayerRepository(db)
    reconciled = provisional.reconcile(
        season.season_id,
        player.season_player_id,
        canonical.season_player_id,
        actor=SCORER,
        reason="Confirmed same player",
    )

    assert reconciled.season_player_id == player.season_player_id  # identity preserved
    assert reconciled.canonical_player_id == 9201
    assert reconciled.afl_team_name == "Some Club"
    assert reconciled.was_provisional is True  # permanent historical marker
    assert reconciled.provisional_note == "Verified via club website squad list"

    # Ownership/draft history survived under the same season_player_id.
    squad = OwnershipRepository(db).current_squad(entries[0].season_entry_id)
    assert player.season_player_id in {item.season_player_id for item in squad}
    picks = draft.picks(season.season_id)
    assert any(p.selected_season_player_id == player.season_player_id for p in picks)

    # The duplicate canonical pool row is gone; no duplicate entry remains.
    assert pool.get_by_id(canonical.season_player_id) is None
    assert pool.get(season.season_id, 9201).season_player_id == player.season_player_id

    # Dashboard notice disappears once reconciled.
    outstanding = provisional.list_outstanding(season.season_id)
    assert player.season_player_id not in {o.player.season_player_id for o in outstanding}

    events = AuditEventRepository(db).list_events(action="player_pool.provisional.reconciled")
    assert len(events) == 1
    assert events[0].payload["retired_duplicate_season_player_id"] == canonical.season_player_id


def test_reconciliation_restores_losing_candidates_when_ambiguous_set_is_resolved():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    winner = pool.refresh_player(
        season.season_id, 9301, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    loser = pool.refresh_player(
        season.season_id, 9302, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    ProvisionalPlayerRepository(db).reconcile(
        season.season_id, player.season_player_id, winner.season_player_id, actor=SCORER, reason="Confirmed via club"
    )

    assert pool.get_by_id(loser.season_player_id).eligible is True
    remaining_candidates = db.execute(
        "SELECT COUNT(*) AS n FROM provisional_match_candidate WHERE season_player_id=?", (player.season_player_id,)
    ).fetchone()["n"]
    assert remaining_candidates == 0


def test_reconciliation_preserves_the_targets_pre_quarantine_ineligibility():
    """Codex review on PR #258 (P2, fifth round): if the target canonical
    player was already ineligible for an unrelated policy reason before
    candidate detection quarantined it, reconciling to it must not silently
    make the merged (provisional) row draftable -- a provisional row is
    created eligible, so the merge must carry over the winning candidate's
    recorded pre-quarantine ineligibility rather than leaving the source
    row's own (eligible) value untouched."""
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    target = pool.refresh_player(
        season.season_id,
        9701,
        "Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        eligible=False,
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    reconciled = ProvisionalPlayerRepository(db).reconcile(
        season.season_id, player.season_player_id, target.season_player_id, actor=SCORER, reason="Confirmed"
    )
    assert reconciled.eligible is False


def test_reconciliation_preserves_ineligibility_of_a_target_never_detected_as_a_candidate():
    """As above, but for a target the caller names directly without it ever
    having been suggested by `detect_candidates` -- there is no candidate
    row to consult, so the target's own live `eligible` value (not a
    recorded pre-quarantine one) is authoritative and must still survive
    the merge."""
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="A", family="One")
    pool = PlayerPoolRepository(db)
    target = pool.refresh_player(season.season_id, 9702, "Someone Else", eligible=False)

    reconciled = ProvisionalPlayerRepository(db).reconcile(
        season.season_id, player.season_player_id, target.season_player_id, actor=SCORER, reason="Confirmed"
    )
    assert reconciled.eligible is False


def test_reconciliation_does_not_reapply_a_rejected_winning_candidates_stale_eligibility():
    """Codex review on PR #258 (P2, sixth round): the winning-candidate
    eligibility lookup added for pre-quarantine preservation must also only
    consider a still-*pending* candidate -- a rejected one's recorded value
    can be stale by the time of a later, unrelated eligibility change and a
    manual reconciliation to that same target."""
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    target = pool.refresh_player(
        season.season_id, 9704, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    provisional = ProvisionalPlayerRepository(db)
    provisional.reject_candidate(season.season_id, player.season_player_id, 9704, actor=SCORER, reason="Not them")
    assert pool.get_by_id(target.season_player_id).eligible is True

    # Made ineligible for an unrelated reason after the rejection.
    pool.refresh_player(
        season.season_id,
        9704,
        "Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        eligible=False,
    )

    reconciled = provisional.reconcile(
        season.season_id, player.season_player_id, target.season_player_id, actor=SCORER, reason="Confirmed after all"
    )
    assert reconciled.eligible is False


def test_reconciliation_recreates_another_provisional_players_still_pending_claim_on_the_same_target():
    """Codex review on PR #258 (P2, seventh round): if some *other*
    provisional player still has a pending candidate naming the exact
    canonical id being reconciled here, `fk_candidate_target_same_season`'s
    `ON DELETE CASCADE` (triggered when the target's own pool row is
    deleted to make way for the merge) would otherwise silently destroy
    that unresolved suggestion and let both the merged row and the other
    provisional player become independently draftable -- this decision
    only establishes that *this* provisional player is the target, not
    that the other one is not. Reconciling A must recreate B's claim
    against the same canonical id (now held by the merged row) and keep
    that row quarantined until B's claim is itself decided."""
    db, season, _entries = setup_domain()
    player_a = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    player_b = _create(db, season.season_id, given="Jordan2", family="Newrecruit2")
    with transaction(db) as conn:
        conn.execute(
            "UPDATE season_player_pool SET given_name='Jordan', family_name='Newrecruit' WHERE season_player_id=?",
            (player_b.season_player_id,),
        )
    pool = PlayerPoolRepository(db)
    target = pool.refresh_player(
        season.season_id, 9705, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    provisional = ProvisionalPlayerRepository(db)
    reconciled = provisional.reconcile(
        season.season_id, player_a.season_player_id, target.season_player_id, actor=SCORER, reason="Confirmed A"
    )

    # The merged row stays quarantined -- B's claim on the same canonical
    # id is still open.
    assert reconciled.eligible is False

    # B's claim survives the cascade, now naming the merged row's canonical
    # id.
    remaining = db.execute(
        "SELECT status FROM provisional_match_candidate WHERE season_player_id=? AND canonical_player_id=?",
        (player_b.season_player_id, 9705),
    ).fetchone()
    assert remaining is not None
    assert remaining["status"] == "pending"

    # Deciding B's claim now correctly resolves the merged row's
    # eligibility.
    provisional.reject_candidate(
        season.season_id, player_b.season_player_id, 9705, actor=SCORER, reason="Not the same person as B"
    )
    assert pool.get_by_id(player_a.season_player_id).eligible is True


def test_reconciliation_preserves_a_rejected_pairs_tombstone_across_the_canonical_identity_move():
    """Codex review on PR #258 (P2, ninth round): an already-rejected pair
    for a *different* provisional player naming the same target canonical
    id must also survive `fk_candidate_target_same_season`'s cascade, not
    just a still-pending one -- or the next detection run resurrects a
    suggestion a Scorer explicitly rejected, since the "already known"
    check only consults a live row."""
    db, season, _entries = setup_domain()
    player_a = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    player_b = _create(db, season.season_id, given="Jordan2", family="Newrecruit2")
    with transaction(db) as conn:
        conn.execute(
            "UPDATE season_player_pool SET given_name='Jordan', family_name='Newrecruit' WHERE season_player_id=?",
            (player_b.season_player_id,),
        )
    pool = PlayerPoolRepository(db)
    target = pool.refresh_player(
        season.season_id, 9911, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    provisional = ProvisionalPlayerRepository(db)
    provisional.reject_candidate(season.season_id, player_b.season_player_id, 9911, actor=SCORER, reason="Not B")

    provisional.reconcile(
        season.season_id, player_a.season_player_id, target.season_player_id, actor=SCORER, reason="Confirmed A"
    )

    # B's rejected tombstone survives the merge -- still rejected, not lost.
    tombstone = db.execute(
        "SELECT status FROM provisional_match_candidate WHERE season_player_id=? AND canonical_player_id=?",
        (player_b.season_player_id, 9911),
    ).fetchone()
    assert tombstone is not None
    assert tombstone["status"] == "rejected"

    # The next detection run must not resurrect it as a fresh suggestion.
    detected = detect_candidates(db, season.season_id, actor=ActorContext.system())
    assert detected == 0
    still_rejected = db.execute(
        "SELECT status FROM provisional_match_candidate WHERE season_player_id=? AND canonical_player_id=?",
        (player_b.season_player_id, 9911),
    ).fetchone()
    assert still_rejected["status"] == "rejected"


def test_deferring_an_already_rejected_candidate_is_refused():
    """Codex review on PR #258 (P2, fifth round): a rejected candidate is
    terminal -- deferring it would record a `candidate_deferred` audit
    event for an action that left nothing actionable."""
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="Jordan", family="Newrecruit")
    pool = PlayerPoolRepository(db)
    pool.refresh_player(season.season_id, 9703, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit")
    detect_candidates(db, season.season_id, actor=ActorContext.system())

    provisional = ProvisionalPlayerRepository(db)
    provisional.reject_candidate(season.season_id, player.season_player_id, 9703, actor=SCORER, reason="Not them")

    with pytest.raises(ProvisionalPlayerError):
        provisional.defer_candidate(season.season_id, player.season_player_id, 9703, actor=SCORER)


def test_repeated_reconciliation_fails_safely():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id)
    pool = PlayerPoolRepository(db)
    canonical = pool.refresh_player(season.season_id, 9401, "Someone Else")
    provisional = ProvisionalPlayerRepository(db)
    provisional.reconcile(
        season.season_id, player.season_player_id, canonical.season_player_id, actor=SCORER, reason="confirmed"
    )
    other = pool.refresh_player(season.season_id, 9402, "Another Player")
    with pytest.raises(NotProvisionalError):
        provisional.reconcile(
            season.season_id, player.season_player_id, other.season_player_id, actor=SCORER, reason="again"
        )


def test_reconciliation_refuses_a_target_that_is_itself_provisional():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id, given="A", family="One")
    other_provisional = _create(db, season.season_id, given="B", family="Two")
    with pytest.raises(InvalidReconciliationTargetError):
        ProvisionalPlayerRepository(db).reconcile(
            season.season_id,
            player.season_player_id,
            other_provisional.season_player_id,
            actor=SCORER,
            reason="bad target",
        )


def test_reconciliation_refuses_a_target_that_was_itself_a_reconciled_provisional_player():
    """Codex review on PR #258 (P2, eighth round): a target that was itself
    created provisional and already reconciled (`was_provisional=True`,
    `canonical_player_id` now set) carries its own permanent, stable
    `season_player_id` and history. Accepting it as a *target* here would
    retire and delete it as though it were a disposable freshly-imported
    canonical duplicate -- reversing the earlier reconciliation decision
    that established it -- so it must be refused exactly like a target that
    is still plainly provisional."""
    db, season, _entries = setup_domain()
    player_a = _create(db, season.season_id, given="A", family="One")
    canonical = PlayerPoolRepository(db).refresh_player(season.season_id, 9906, "Someone Else")
    reconciled_a = ProvisionalPlayerRepository(db).reconcile(
        season.season_id, player_a.season_player_id, canonical.season_player_id, actor=SCORER, reason="Confirmed A"
    )
    assert reconciled_a.was_provisional is True

    player_b = _create(db, season.season_id, given="B", family="Two")
    with pytest.raises(InvalidReconciliationTargetError):
        ProvisionalPlayerRepository(db).reconcile(
            season.season_id,
            player_b.season_player_id,
            reconciled_a.season_player_id,
            actor=SCORER,
            reason="bad target -- already-reconciled provisional",
        )


def test_reconciliation_refuses_a_target_from_a_different_season():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id)
    other_season = SeasonRepository(db).create_season(2028, "2028")
    foreign = PlayerPoolRepository(db).refresh_player(other_season.season_id, 9500, "Foreign Player")
    with pytest.raises(InvalidReconciliationTargetError):
        ProvisionalPlayerRepository(db).reconcile(
            season.season_id, player.season_player_id, foreign.season_player_id, actor=SCORER, reason="wrong season"
        )


def test_reconciliation_refuses_a_source_that_is_not_provisional():
    db, season, _entries = setup_domain()
    pool = PlayerPoolRepository(db)
    already_canonical = pool.refresh_player(season.season_id, 9600, "Already Canonical")
    other = pool.refresh_player(season.season_id, 9601, "Other Player")
    with pytest.raises(NotProvisionalError):
        ProvisionalPlayerRepository(db).reconcile(
            season.season_id,
            already_canonical.season_player_id,
            other.season_player_id,
            actor=SCORER,
            reason="not provisional",
        )


def test_reconciliation_refuses_a_target_with_existing_ownership_history():
    db, season, entries = setup_domain()
    player = _create(db, season.season_id)
    pool = PlayerPoolRepository(db)
    owned = pool.refresh_player(season.season_id, 9700, "Owned Already")
    OwnershipRepository(db).acquire(owned.season_player_id, entries[0].season_entry_id, effective_at="2027-01-01")
    with pytest.raises(ReconciliationConflictError):
        ProvisionalPlayerRepository(db).reconcile(
            season.season_id, player.season_player_id, owned.season_player_id, actor=SCORER, reason="already owned"
        )


def test_reconciliation_requires_a_reason():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id)
    canonical = PlayerPoolRepository(db).refresh_player(season.season_id, 9800, "Canonical Player")
    with pytest.raises(ValueError):
        ProvisionalPlayerRepository(db).reconcile(
            season.season_id, player.season_player_id, canonical.season_player_id, actor=SCORER, reason=""
        )


def test_reconciliation_refused_once_season_is_completed():
    db, season, _entries = setup_domain()
    player = _create(db, season.season_id)
    canonical = PlayerPoolRepository(db).refresh_player(season.season_id, 9900, "Canonical Player")
    with transaction(db) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season.season_id,))
    with pytest.raises(SeasonCompletedError):
        ProvisionalPlayerRepository(db).reconcile(
            season.season_id, player.season_player_id, canonical.season_player_id, actor=SCORER, reason="too late"
        )
