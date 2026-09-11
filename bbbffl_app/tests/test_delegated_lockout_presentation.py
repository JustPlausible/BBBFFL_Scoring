"""Issue #138: the delegated Replay Operator weekly-lineup page must use
and display the same authoritative staged lock-state read model as the
ordinary Coach lineup page, rather than continuing to render ordinary
positions as editable once a selective or main trigger has activated.

These tests exercise `app.routes.delegated_operations._lineup_view` (the
exact function the delegated HTTP route calls) directly against the real
`CoachLineupService`/`LockoutRepository`/`OpeningRoundNominationRepository`
boundaries -- never a parallel delegated-only lock calculation -- so a
regression that reintroduces a second read model would fail here exactly
as it would on the Coach page's own `tests/test_lockouts.py` /
`tests/test_staged_lockout_rehearsal.py` suites.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.audit import ActorContext
from app.authorization import Principal, Role
from app.coach_lineup import CoachLineupService
from app.identity import IdentityRepository
from app.lineup_proxy import LineupProxyService
from app.lineups import WeeklyLineupRepository
from app.lockouts import InvalidSelectionError, LockedSelectionError, LockoutTriggerRepository
from app.opening_round import OpeningRoundNominationRepository, OpeningRoundRuleRepository
from app.routes import delegated_operations
from tests.test_competition_lifecycle import KnownRound
from tests.test_lockouts import (
    ALL_MATCHES,
    BYE_TEAM,
    EARLY_HOME,
    EARLY_MATCH_ID,
    EARLY_START,
    LATE_MATCH_ID,
    LATE_START,
    UNCOVERED_HOME,
    acquire,
    configure_main,
    configure_selective,
)
from tests.test_lockouts import context as lockout_context

OPERATOR = ActorContext.anonymous_operator("replay_operator")

# `lockout_context()`'s default `operational(db, 2027, None)` accepts a
# mapping of afl_season_id=2027, afl_round_id=2027 (see
# tests.test_competition_lifecycle.configured) -- `afl_client_with_bye`
# below must answer for that exact afl_round_id so
# `RoundMatchFactsProvider.byes_for` (the real production wiring) resolves
# a positive bye confirmation end to end.
MAPPED_AFL_ROUND_ID = 2027


def afl_client(matches):
    """Duck-typed AFL client: `get_matches` ignores the requested AFL round
    id and always returns `matches` -- every scenario below uses a single
    mapped round, exactly like `RoundMatchFactsProvider`'s production
    composition expects (app/lockouts.py). `get_rounds` is a bare stub only
    so `LineupValidationService`'s (unrelated) availability advisory has
    something to call once a submission exists -- these tests are not
    about bye-round advisories."""
    return SimpleNamespace(get_matches=lambda afl_round_id: matches, get_rounds=lambda afl_season_id: [])


def afl_client_with_bye(matches, bye_team, *, afl_round_id=MAPPED_AFL_ROUND_ID):
    """Like `afl_client`, but `get_rounds` positively confirms `bye_team` as
    this round's AFL bye (issue #185) -- exercising the same
    `RoundMatchFactsProvider.byes_for` production wiring `app.lockouts.
    resolve_byes` consults, never a hand-rolled `FakeMatchFacts`."""
    return SimpleNamespace(
        get_matches=lambda requested_round_id: matches,
        get_rounds=lambda afl_season_id: [SimpleNamespace(round_id=afl_round_id, round_number=1, byes=(bye_team,))],
    )


def _request(db, client):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=db, afl_client=client)))


def _principal(entry):
    return Principal(
        Role.REPLAY_OPERATOR,
        coach_id="authenticated-replay-operator",
        display_name="Authenticated Replay Operator",
        represented_season_entry_id=entry.season_entry_id,
    )


def _scope(db, round_, scope_row, entry):
    team = IdentityRepository(db).get_public_team(entry.season_entry_id)
    return {
        "bbbffl_round_id": round_.bbbffl_round_id,
        "season_id": scope_row["season_id"],
        "competition_id": scope_row["competition_id"],
        "season_entry_id": entry.season_entry_id,
        "team_name": team.team_name,
        "season_label": "Test season",
        "round_label": round_.label,
        "sequence": round_.sequence,
    }


def _lock_by_position(view):
    return {row["position"]: row for row in view["lock_state"]}


def _save_and_submit(db, round_, scope_row, entry, positions, *, matches, evaluation_at, expected_version=0):
    proxy = LineupProxyService(db)
    draft = proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        positions,
        expected_revision=0,
        actor=OPERATOR,
    )
    from app.lockouts import LockoutRepository

    class _Facts:
        def matches_for(self, bbbffl_round_id):
            return matches

    guard = LockoutRepository(db).guard(match_facts=_Facts(), evaluation_at=evaluation_at)
    return proxy.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=expected_version,
        actor=OPERATOR,
        reason="issue #138 delegated lockout presentation test scenario",
        lock_guard=guard,
    )


# ---------------------------------------------------------------------------
# 1-3: progressive selective/main lockout parity with the Coach page.
# ---------------------------------------------------------------------------


def test_before_any_trigger_activates_ordinary_delegated_controls_are_editable():
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )

    request = _request(db, afl_client(ALL_MATCHES))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    locks = _lock_by_position(view)
    assert locks["F1"]["state"] == "editable"
    assert locks["F1"]["editable"] is True
    assert locks["F1"]["reason_code"] == "not_yet_triggered"


def test_selective_boundary_locks_covered_players_leaves_uncovered_editable():
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    uncovered = acquire(pool, ownership, scope_row, entry, 2, UNCOVERED_HOME)
    _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )

    request = _request(db, afl_client(ALL_MATCHES))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    locks = _lock_by_position(view)
    # The GET itself evaluates "now" (real wall-clock), which is nowhere near
    # EARLY_START/LATE_START (both fixed in 2027) -- so nothing has fired yet
    # from a fresh GET. Drive the boundary explicitly through the same
    # `LockoutRepository` the read model calls, mirroring how the real HTTP
    # route always evaluates at the moment of the request.
    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), afl_client(ALL_MATCHES))
    lineup_id = db.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_.bbbffl_round_id, entry.season_entry_id),
    ).fetchone()["lineup_id"]
    LockoutRepository(db).lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        match_facts=match_facts,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    locks = _lock_by_position(view)
    assert locks["F1"]["state"] == "locked"
    assert locks["F1"]["lock_type"] == "selective_trigger"
    assert locks["F1"]["reason_code"] == "selective_trigger_activated"
    assert locks["F1"]["irreversible"] is True
    assert locks["M1"]["state"] == "editable"


def test_main_lockout_locks_every_selected_ordinary_player_and_the_remaining_vacancy():
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    uncovered = acquire(pool, ownership, scope_row, entry, 2, UNCOVERED_HOME)
    _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )
    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), afl_client(ALL_MATCHES))
    lineup_id = db.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_.bbbffl_round_id, entry.season_entry_id),
    ).fetchone()["lineup_id"]
    LockoutRepository(db).lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        match_facts=match_facts,
        evaluation_at=LATE_START,
    )

    request = _request(db, afl_client(ALL_MATCHES))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    locks = _lock_by_position(view)
    assert locks["F1"]["state"] == "locked" and locks["F1"]["lock_type"] == "selective_trigger"
    assert locks["M1"]["state"] == "locked" and locks["M1"]["lock_type"] == "main_trigger"
    # Issue #155: a deliberately vacant position is never invented into a
    # fabricated player-level lock, but Main lockout still makes it
    # immutable -- every *remaining* ordinary position locks the instant
    # Main activates, vacant ones included. It renders as main-locked, its
    # control disabled, and its human-readable value stays "Vacant"
    # (`season_player_id` stays `None`) -- never as an editable, enabled
    # dropdown.
    assert locks["M2"]["state"] == "locked"
    assert locks["M2"]["editable"] is False
    assert locks["M2"]["lock_type"] == "main_trigger"
    assert locks["M2"]["reason_code"] == "main_lockout_triggered"
    assert locks["M2"]["season_player_id"] is None
    assert locks["M2"]["irreversible"] is False


def test_delegated_proxy_submission_cannot_populate_a_vacancy_after_main_lockout():
    """Issue #155: the delegated/proxy submission path (`LineupProxyService`,
    the exact service `app.routes.delegated_operations.submit` calls) must
    reject -- atomically, with the prior authoritative submission
    unchanged -- an attempt to introduce a player into a position that was
    a deliberate vacancy right through Main lockout. A private draft save
    is not itself an authoritative mutation and is allowed to hold the
    attempted (never-submitted) change; only `submit` is the enforcement
    boundary, and it must not provide a bypass."""
    from app.lineup_proxy import LineupProxyService
    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    late_filler = acquire(pool, ownership, scope_row, entry, 2, UNCOVERED_HOME, name="Post-Main Vacancy Filler")
    submitted = _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )
    assert submitted.positions["M2"] is None

    lineup_id = db.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_.bbbffl_round_id, entry.season_entry_id),
    ).fetchone()["lineup_id"]

    class _Facts:
        def matches_for(self, bbbffl_round_id):
            return ALL_MATCHES

    proxy = LineupProxyService(db)
    attempted_fill = {"F1": early.season_player_id, "M2": late_filler.season_player_id}
    # The private draft itself is not the authoritative record -- saving it
    # succeeds (a scorer/operator may need to stage a candidate change),
    # but that alone must never move the effective/submitted lineup.
    proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        attempted_fill,
        expected_revision=1,
        actor=OPERATOR,
    )
    assert WeeklyLineupRepository(db).get_effective_submission(lineup_id).positions == submitted.positions

    draft = WeeklyLineupRepository(db).get_draft(
        scope_row["season_id"], scope_row["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    main_guard = LockoutRepository(db).guard(
        match_facts=RoundMatchFactsProvider(RoundMappingRepository(db), afl_client(ALL_MATCHES)),
        evaluation_at=LATE_START,
    )
    with pytest.raises(LockedSelectionError, match="M2"):
        proxy.submit(
            lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=submitted.version,
            actor=OPERATOR,
            reason="issue #155 delegated proxy post-main vacancy fill attempt",
            lock_guard=main_guard,
        )
    # Rejected atomically: the prior authoritative submission survives
    # completely unchanged, never a partial write of the attempted change.
    final = WeeklyLineupRepository(db).get_effective_submission(lineup_id)
    assert final.version == submitted.version
    assert final.positions == submitted.positions
    assert final.positions["M2"] is None


# ---------------------------------------------------------------------------
# 4: Opening Round deferred locks render independently, never merged into
# the ordinary selective/main lock state.
# ---------------------------------------------------------------------------


def test_opening_round_deferred_lock_renders_as_its_own_distinct_lock_type():
    from app.afl_client import Match, Team
    from tests import opening_round_evidence as evidence

    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    ev = evidence.EVIDENCE_2026
    bye_round_id = ev.compensating_bye_round["GWS"]

    class _MultiRound(KnownRound):
        def round_exists(self, season, round_id):
            return (season, round_id) in {(ev.afl_season_id, ev.afl_opening_round_id), (ev.afl_season_id, bye_round_id)}

    rule = OpeningRoundRuleRepository(db).accept(
        scope_row["season_id"],
        15,
        ev.afl_season_id,
        ev.afl_opening_round_id,
        bye_round_id,
        round_.bbbffl_round_id,
        _MultiRound(ev.afl_season_id, ev.afl_opening_round_id),
        actor=ActorContext.anonymous_operator("admin"),
        reason="issue #138 opening round deferred presentation test",
    )
    player = pool.refresh_player(
        scope_row["season_id"], 900001, "GWS Deferred Player", afl_team_id=15, afl_team_name="GWS Giants"
    )
    ownership.acquire(player.season_player_id, entry.season_entry_id)
    nominations = OpeningRoundNominationRepository(db)
    opening_matches_client = SimpleNamespace(
        get_matches=lambda round_id: [
            Match(match_id=7001, home_team=Team(15, "GWS Giants"), away_team=Team(1, "Adelaide"), status="CONCLUDED")
        ]
    )
    nomination = nominations.nominate(
        rule.rule_id,
        entry.season_entry_id,
        "F1",
        player.season_player_id,
        opening_matches_client,
        actor=OPERATOR,
        reason="issue #138 test nomination",
    )
    nominations.preload_target_lineup(
        WeeklyLineupRepository(db),
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
    )

    request = _request(db, afl_client(ALL_MATCHES))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    locks = _lock_by_position(view)
    assert locks["F1"]["state"] == "locked"
    assert locks["F1"]["lock_type"] == "opening_round_deferred"
    assert locks["F1"]["reason_code"] == "opening_round_deferred"
    assert locks["F1"]["deferred_context"]["nomination_id"] == nomination.nomination_id
    assert locks["F1"]["season_player_id"] == player.season_player_id
    # Never conflated with an ordinary selective/main lock reason.
    assert locks["F1"]["reason_code"] not in ("selective_trigger_activated", "main_lockout_triggered")


# ---------------------------------------------------------------------------
# 5: the delegated read model materialises the same durable evidence the
# Coach page's own `view()` does, and reports the same lock state for it.
# ---------------------------------------------------------------------------


def test_delegated_read_model_materialises_evidence_consistently_with_coach_page():
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )
    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), afl_client(ALL_MATCHES))
    lineup_id = db.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_.bbbffl_round_id, entry.season_entry_id),
    ).fetchone()["lineup_id"]

    # Nothing has evaluated this round's lockout plan yet -- no persisted
    # activation/lock rows exist.
    assert db.execute("SELECT 1 FROM bbbffl_round_lockout_trigger_activation").fetchall() == []
    assert db.execute("SELECT 1 FROM weekly_lineup_lock").fetchall() == []

    LockoutRepository(db).lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early.season_player_id},
        match_facts=match_facts,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )

    # A fresh delegated GET, with no further explicit evaluation, sees
    # exactly the same already-persisted, irreversible evidence -- it does
    # not need to (and does not) recompute a second, delegated-only answer.
    request = _request(db, afl_client(ALL_MATCHES))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    delegated_lock = _lock_by_position(view)["F1"]
    assert delegated_lock["state"] == "locked"
    assert delegated_lock["irreversible"] is True

    coach_id = db.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (entry.season_entry_id,),
    ).fetchone()["coach_id"]
    coach_view = CoachLineupService(db, afl_client(ALL_MATCHES)).view(
        coach_id, scope_row["season_id"], round_.bbbffl_round_id
    )
    coach_lock = coach_view.locks["F1"]
    assert coach_lock.state.value == delegated_lock["state"]
    assert coach_lock.reason == delegated_lock["reason_code"]
    assert coach_lock.irreversible == delegated_lock["irreversible"]
    assert coach_lock.afl_match_id == delegated_lock["afl_match_id"]


# ---------------------------------------------------------------------------
# 6: human-readable presentation -- names/clubs, not UUIDs as the primary
# display value.
# ---------------------------------------------------------------------------


def test_locked_position_presentation_is_human_readable_not_a_uuid():
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME, name="Readable Forward")
    _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )
    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), afl_client(ALL_MATCHES))
    lineup_id = db.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_.bbbffl_round_id, entry.season_entry_id),
    ).fetchone()["lineup_id"]
    LockoutRepository(db).lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early.season_player_id},
        match_facts=match_facts,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )

    request = _request(db, afl_client(ALL_MATCHES))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    row = _lock_by_position(view)["F1"]
    assert row["player_display_name"] == "Readable Forward"
    assert row["afl_club_name"] == EARLY_HOME.name
    assert row["player_display_name"] != row["season_player_id"]
    assert view["player_display_names"][early.season_player_id] == "Readable Forward"


# ---------------------------------------------------------------------------
# 7-9: a crafted prohibited submission is still rejected server-side, and
# the resulting divergence between the (rejected) attempted draft and the
# unchanged authoritative submission is visible in the reloaded read model.
# ---------------------------------------------------------------------------


def test_prohibited_submission_is_still_rejected_and_divergent_draft_is_reported():
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    other_early = acquire(pool, ownership, scope_row, entry, 2, EARLY_HOME, name="Bench Forward")
    first = _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )

    proxy = LineupProxyService(db)
    draft2 = proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": other_early.season_player_id},
        expected_revision=1,
        actor=OPERATOR,
    )

    from app.lockouts import LockoutRepository

    class _Facts:
        def matches_for(self, bbbffl_round_id):
            return ALL_MATCHES

    late_guard = LockoutRepository(db).guard(match_facts=_Facts(), evaluation_at=EARLY_START + timedelta(minutes=1))
    with pytest.raises(LockedSelectionError, match="F1"):
        proxy.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=first.version,
            actor=OPERATOR,
            reason="crafted prohibited attempt after selective lockout",
            lock_guard=late_guard,
        )

    # The authoritative submission is untouched by the rejected attempt...
    from app.lineups import WeeklyLineupRepository

    effective = WeeklyLineupRepository(db).get_effective_submission(draft2.lineup_id)
    assert effective.version == first.version
    assert effective.positions["F1"] == early.season_player_id

    # ...but the private draft (save-then-submit) now holds the attempted,
    # rejected change -- the reloaded read model must make that divergence
    # visible rather than silently presenting the rejected change as though
    # it were accepted.
    request = _request(db, afl_client(ALL_MATCHES))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    assert view["draft"]["positions"]["F1"] == other_early.season_player_id
    assert view["submission"]["positions"]["F1"] == early.season_player_id
    assert view["draft_diverges_from_submission"] is True

    # The per-position lock_state (what the delegated template actually
    # renders) must show the authoritative submitted player as F1's
    # primary value -- never the divergent, rejected draft replacement --
    # and separately flag the divergence (Codex review on PR #143).
    row = _lock_by_position(view)["F1"]
    assert row["state"] == "locked"
    assert row["editable"] is False
    assert row["season_player_id"] == early.season_player_id
    assert row["draft_season_player_id"] == other_early.season_player_id
    assert row["draft_diverges"] is True
    assert row["draft_player_display_name"] == other_early.display_name


def test_divergent_draft_replacement_from_an_uncovered_match_still_reports_the_position_locked():
    """The sharper form of the same defect Codex flagged: the draft's
    divergent replacement is a *real* player (not vacant) whose own AFL
    match is not itself covered by any activated trigger. Naively live-
    evaluating that replacement's own match would report it editable, even
    though `guard_transition` refuses to change F1 away from its
    authoritatively locked, submitted value regardless of the
    replacement's own match state."""
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    uncovered_replacement = acquire(pool, ownership, scope_row, entry, 2, UNCOVERED_HOME, name="Uncovered Replacement")
    first = _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )

    proxy = LineupProxyService(db)
    proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": uncovered_replacement.season_player_id},
        expected_revision=1,
        actor=OPERATOR,
    )

    request = _request(db, afl_client(ALL_MATCHES))
    # Materialise F1's lock evidence via a GET before the trigger fires is
    # not required here -- evaluating at/after EARLY_START below drives it.
    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), afl_client(ALL_MATCHES))
    lineup_id = db.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_.bbbffl_round_id, entry.season_entry_id),
    ).fetchone()["lineup_id"]
    LockoutRepository(db).lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        first.positions,
        match_facts=match_facts,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )

    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    row = _lock_by_position(view)["F1"]
    assert row["state"] == "locked"
    assert row["lock_type"] == "selective_trigger"
    assert row["editable"] is False
    assert row["season_player_id"] == early.season_player_id
    assert row["draft_season_player_id"] == uncovered_replacement.season_player_id
    assert row["draft_diverges"] is True
    assert row["draft_player_display_name"] == uncovered_replacement.display_name
    assert view["draft_diverges_from_submission"] is True


# ---------------------------------------------------------------------------
# 12: replay-mode UPCOMING plus an activated match_time_reached trigger
# must display without contradiction -- never treat UPCOMING as editable.
# ---------------------------------------------------------------------------


def test_replay_upcoming_match_with_activated_trigger_shows_locked_not_editable():
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    # The match itself never leaves UPCOMING -- only replay evaluation time
    # reaching its scheduled start drives activation (`match_time_reached`).
    upcoming_matches = ALL_MATCHES
    _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id},
        matches=upcoming_matches,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )
    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), afl_client(upcoming_matches))
    lineup_id = db.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_.bbbffl_round_id, entry.season_entry_id),
    ).fetchone()["lineup_id"]
    LockoutRepository(db).lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early.season_player_id},
        match_facts=match_facts,
        evaluation_at=EARLY_START,  # exactly at the boundary: still UPCOMING, time reached
    )

    request = _request(db, afl_client(upcoming_matches))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    row = _lock_by_position(view)["F1"]
    assert row["observed_status"] == "UPCOMING"
    assert row["state"] == "locked"
    assert row["editable"] is False
    assert row["lock_type"] == "selective_trigger"

    trigger_view = next(t for t in view["lockout_plan"] if t["trigger_key"] == "early-1")
    assert trigger_view["activated"] is True
    assert trigger_view["activation_reason"] == "match_time_reached"
    assert trigger_view["observed_status"] == "UPCOMING"
    assert trigger_view["effective_lock_at"] is not None
    assert trigger_view["configured_matches"][0]["observed_status"] == "UPCOMING"


# ---------------------------------------------------------------------------
# Lockout-plan sequencing/presentation.
# ---------------------------------------------------------------------------


def test_discard_and_rebase_draft_onto_submission_clears_the_divergence_flag():
    """Codex review (PR #143): the discard/rebase action (an ordinary
    `create_or_amend`/`PUT .../lineup/draft` call saving the authoritative
    submitted positions back into the draft) advances the draft's
    revision even though its positions now exactly match the submission
    again -- `draft_diverges_from_submission` must reflect that actual
    equality, not merely "has the revision moved since submission"."""
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    other = acquire(pool, ownership, scope_row, entry, 2, EARLY_HOME, name="Divergent Draft Choice")
    submitted = _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )

    proxy = LineupProxyService(db)
    diverged_draft = proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": other.season_player_id},
        expected_revision=1,
        actor=OPERATOR,
    )
    request = _request(db, afl_client(ALL_MATCHES))
    diverged_view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    assert diverged_view["draft_diverges_from_submission"] is True

    # Discard/rebase: save the authoritative submitted positions back into
    # the draft. This bumps the draft revision again but now exactly
    # matches the submission.
    proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        expected_revision=diverged_draft.revision,
        actor=OPERATOR,
    )
    rebased_view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    assert rebased_view["draft"]["revision"] > 1
    assert rebased_view["draft_diverges_from_submission"] is False


def test_delegated_lineup_page_renders_lock_state_driven_disabled_controls_client_side():
    """The delegated lineup page is a session-native JSON-driven surface
    (see app/routes/delegated_operations.py's module docstring): the
    server route below only serves the page shell, and the browser then
    fetches `/api/operations/rounds/{round_id}/lineup` and renders from
    its `lock_state` -- this is the same architecture
    `tests/test_delegated_operations.py` already exercises at the JSON
    boundary. This test proves the served template's own rendering logic
    still gates every non-editable position behind a `disabled` control
    keyed off the read model's `editable` flag (never client-computed lock
    state) -- a regression here would silently let a locked/indeterminate
    position render as an enabled, submittable `<select>`."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/operations/rounds/some-round-id/lineup")
        assert response.status_code == 200
        assert "some-round-id" in response.text

    template_path = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "templates" / "delegated_lineup.html"
    )
    source = template_path.read_text(encoding="utf-8")
    # Every non-editable slot renders a disabled control; only an editable
    # one gets a live, submittable `<select data-position>`. `positions()`
    # (which builds the payload Submit/Save send) only reads
    # `[data-position]` elements, so a slot the server marked non-editable
    # is never included as an editable input in the first place.
    assert "row.editable" in source
    assert "if(row.editable){" in source
    assert "<input disabled" in source
    assert "<select data-position=" in source
    # Lock state, never recomputed client-side from raw match status.
    assert "row.state" in source and "row.lock_type" in source
    assert "row.observed_status" in source
    # Codex review (PR #143): the "Submitted + private changes" badge must
    # key off the server-computed `draft_diverges_from_submission` (an
    # actual position-by-position comparison), never off draft revision
    # ordering alone -- every draft save advances the revision even when
    # positions are unchanged, and the discard/rebase action itself saves
    # the submitted positions as a newer revision, which would otherwise
    # still (wrongly) read as "changed" immediately after discarding.
    assert "d.draft_diverges_from_submission" in source
    assert "d.draft.revision>d.submission.based_on_draft_revision" not in source
    # Second Codex review round (PR #143): the discard/rebase action must
    # stay available for a persisted divergence, not only immediately
    # after a client-observed rejection.
    assert "d.draft_diverges_from_submission?" in source
    # positions() (the Save/Submit payload builder) must serialise every
    # non-editable slot's *authoritative* lock-state value, never the raw,
    # possibly-divergent private draft underneath it.
    assert "state.lock_state.forEach(row=>p[row.position]=row.season_player_id)" in source


def test_still_open_position_under_the_submission_shows_the_operators_own_live_draft_pick():
    """A position the authoritative submission leaves open (e.g. still
    vacant, or a player whose own match has not yet been covered by any
    activated trigger) must keep showing the operator's own in-progress
    draft pick, live-evaluated -- not silently reverted to the
    submission's own (e.g. vacant) value. This is the pre-submission
    "would this pick be rejected" preview the read model has always
    given; deriving locked-position immutability from the effective
    submission (Codex review on PR #143) must not remove it for positions
    the submission does not itself lock."""
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)
    uncovered = acquire(pool, ownership, scope_row, entry, 2, UNCOVERED_HOME)
    # M1 is left vacant in the authoritative submission.
    _save_and_submit(
        db,
        round_,
        scope_row,
        entry,
        {"F1": early.season_player_id},
        matches=ALL_MATCHES,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )

    proxy = LineupProxyService(db)
    proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        expected_revision=1,
        actor=OPERATOR,
    )

    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), afl_client(ALL_MATCHES))
    lineup_id = db.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_.bbbffl_round_id, entry.season_entry_id),
    ).fetchone()["lineup_id"]
    LockoutRepository(db).lock_state(
        lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early.season_player_id},
        match_facts=match_facts,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )

    request = _request(db, afl_client(ALL_MATCHES))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    m1 = _lock_by_position(view)["M1"]
    assert m1["state"] == "editable"
    assert m1["editable"] is True
    assert m1["season_player_id"] == uncovered.season_player_id
    assert m1["draft_season_player_id"] == uncovered.season_player_id
    assert m1["draft_diverges"] is False
    # F1 is still the authoritative, locked, unaffected selection.
    f1 = _lock_by_position(view)["F1"]
    assert f1["state"] == "locked" and f1["season_player_id"] == early.season_player_id


def test_lockout_plan_orders_triggers_by_configured_sequence():
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    # Created out of sequence order -- the presented plan must still order
    # by each trigger's configured `sequence`, not creation/insertion order.
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=99)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    acquire(pool, ownership, scope_row, entry, 1, EARLY_HOME)

    request = _request(db, afl_client(ALL_MATCHES))
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    assert [t["trigger_key"] for t in view["lockout_plan"]] == ["early-1", "main"]
    assert [t["sequence"] for t in view["lockout_plan"]] == [1, 99]


# ---------------------------------------------------------------------------
# Issue #185: delegated-Scorer flow parity with the Coach page for an
# invalid (bye-player) selection -- editable pre-lockout, submission
# refused while it remains selected, replacement lets submission succeed.
# ---------------------------------------------------------------------------


def test_delegated_bye_player_position_is_editable_with_a_human_readable_explanation():
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope_row, entry, 1, BYE_TEAM, name="Bye Club Player")
    client = afl_client_with_bye(ALL_MATCHES, BYE_TEAM)

    LineupProxyService(db, client).create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": bye_player.season_player_id},
        expected_revision=0,
        actor=OPERATOR,
    )

    request = _request(db, client)
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    f1 = _lock_by_position(view)["F1"]
    # Never disabled like a genuinely locked/indeterminate position -- the
    # exact bug the 2026 Round 12 replay exposed.
    assert f1["state"] == "invalid_selection"
    assert f1["editable"] is True
    assert f1["reason_display"] == "This player cannot be selected because Bye FC have no AFL match in this round."
    # The raw provider team id/diagnostics remain available separately.
    assert f1["afl_club_id"] == BYE_TEAM.team_id


def test_delegated_view_reflects_a_proxy_draft_replacement_saved_over_a_submitted_bye_player():
    """Codex review (PR #186): once the operator saves a replacement to the
    proxy draft for a position the effective submission holds a bye player
    in, `_lineup_view` must render the replacement, not keep echoing the
    old submitted bye player -- otherwise a subsequent Submit would resend
    the stale bye player from `positions()`'s `row.season_player_id` and be
    rejected again, even though the operator had already picked a valid
    replacement and saved it."""
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope_row, entry, 1, BYE_TEAM, name="Bye Club Player")
    replacement = acquire(pool, ownership, scope_row, entry, 2, UNCOVERED_HOME, name="Valid Replacement")
    client = afl_client_with_bye(ALL_MATCHES, BYE_TEAM)
    proxy = LineupProxyService(db, client)

    draft = proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": bye_player.season_player_id},
        expected_revision=0,
        actor=OPERATOR,
    )
    submitted = proxy.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        actor=OPERATOR,
        reason="issue #185: historical pre-existing bye selection",
        lock_guard=None,
    )
    proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {**submitted.positions, "F1": replacement.season_player_id},
        expected_revision=draft.revision,
        actor=OPERATOR,
    )

    request = _request(db, client)
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    f1 = _lock_by_position(view)["F1"]
    assert f1["state"] == "editable"
    assert f1["season_player_id"] == replacement.season_player_id
    assert f1["draft_diverges"] is False


def test_delegated_view_keeps_an_invalid_selection_editable_after_a_bad_draft_candidate():
    """Codex re-review (PR #186): the delegated flow shares the exact same
    draft-overlay logic as the coach flow (`app.coach_lineup.
    DRAFT_DEFERRING_LOCK_STATES`), so it must not regress the same way: a
    proxy draft replacement that would itself currently be rejected (a
    player whose own match an already-activated selective trigger now
    covers) must never make an INVALID_SELECTION position render as
    locked/indeterminate -- the operator must retain a further way to
    correct it."""
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope_row, entry, 1, BYE_TEAM, name="Bye Club Player")
    bad_candidate = acquire(pool, ownership, scope_row, entry, 2, EARLY_HOME, name="Now-Locked Candidate")
    client = afl_client_with_bye(ALL_MATCHES, BYE_TEAM)
    proxy = LineupProxyService(db, client)

    draft = proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": bye_player.season_player_id},
        expected_revision=0,
        actor=OPERATOR,
    )
    submitted = proxy.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        actor=OPERATOR,
        reason="issue #185: historical pre-existing bye selection",
        lock_guard=None,
    )

    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    # Durably activate the selective trigger before the operator saves
    # their (bad) draft pick -- mirrors an earlier GET having already
    # observed it, exactly as production always does before any save.
    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), client)
    LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=match_facts,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )

    proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {**submitted.positions, "F1": bad_candidate.season_player_id},
        expected_revision=draft.revision,
        actor=OPERATOR,
    )

    request = _request(db, client)
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    f1 = _lock_by_position(view)["F1"]
    # Still an editable invalid selection -- the original submitted bye
    # player, never the now-locked bad candidate, and never disabled.
    assert f1["state"] == "invalid_selection"
    assert f1["editable"] is True
    assert f1["season_player_id"] == bye_player.season_player_id


def test_delegated_submission_rejected_while_bye_player_selected_then_succeeds_after_replacement():
    """The delegated-Scorer flow (`LineupProxyService`, `source_type=
    'scorer_proxy'`) must behave exactly like the coach flow: fail closed
    while a bye player remains selected, and accept a normal submission
    once it is replaced -- both going through the identical shared
    `guard_transition`/`InvalidSelectionError` boundary, never a
    delegated-only bypass."""
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope_row, entry, 1, BYE_TEAM, name="Bye Club Player")
    replacement = acquire(pool, ownership, scope_row, entry, 2, UNCOVERED_HOME, name="Valid Replacement")
    client = afl_client_with_bye(ALL_MATCHES, BYE_TEAM)
    proxy = LineupProxyService(db, client)

    draft = proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": bye_player.season_player_id},
        expected_revision=0,
        actor=OPERATOR,
    )

    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), client)
    guard = LockoutRepository(db).guard(match_facts=match_facts, evaluation_at=EARLY_START - timedelta(minutes=5))

    with pytest.raises(InvalidSelectionError, match="Bye FC"):
        proxy.submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=0,
            actor=OPERATOR,
            reason="issue #185 delegated lockout presentation test",
            lock_guard=guard,
        )

    draft2 = proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": replacement.season_player_id},
        expected_revision=draft.revision,
        actor=OPERATOR,
    )
    submitted = proxy.submit(
        draft2.lineup_id,
        expected_draft_revision=draft2.revision,
        expected_submission_version=0,
        actor=OPERATOR,
        reason="issue #185 delegated lockout presentation test replacement",
        lock_guard=guard,
    )
    assert submitted.positions["F1"] == replacement.season_player_id

    request = _request(db, client)
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    assert _lock_by_position(view)["F1"]["state"] == "editable"


def test_delegated_main_lockout_still_freezes_a_bye_position_immutably():
    """After the applicable lockout has genuinely activated, the delegated
    flow's immutability is unchanged by this fix -- an invalid selection is
    not a general bypass around lockout enforcement. A bye player already
    sitting in a position from an earlier, unguarded submission (mirroring
    the replay's historical data) becomes genuinely locked, not merely an
    editable invalid selection, once main fires."""
    db, _, round_, entries, scope_row, pool, ownership = lockout_context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID])
    bye_player = acquire(pool, ownership, scope_row, entry, 1, BYE_TEAM, name="Bye Club Player")
    replacement = acquire(pool, ownership, scope_row, entry, 2, UNCOVERED_HOME, name="Too Late Replacement")
    client = afl_client_with_bye(ALL_MATCHES, BYE_TEAM)
    proxy = LineupProxyService(db, client)

    draft = proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": bye_player.season_player_id},
        expected_revision=0,
        actor=OPERATOR,
    )
    # Historical data already contains the bye player, exactly as the 2026
    # replay's earlier submissions do -- established without a lock_guard,
    # since the ordinary guarded path already refuses to accept a bye
    # player into a brand-new submission (see the "rejected" test above).
    submitted = proxy.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        actor=OPERATOR,
        reason="issue #185: historical pre-existing bye selection",
        lock_guard=None,
    )

    from app.lockouts import LockoutRepository, RoundMatchFactsProvider
    from app.round_mapping import RoundMappingRepository

    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), client)
    late_guard = LockoutRepository(db).guard(match_facts=match_facts, evaluation_at=LATE_START)
    draft2 = proxy.create_or_amend(
        scope_row["season_id"],
        scope_row["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": replacement.season_player_id},
        expected_revision=draft.revision,
        actor=OPERATOR,
    )
    with pytest.raises(LockedSelectionError):
        proxy.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            actor=OPERATOR,
            reason="issue #185: attempted post-main replacement, must be refused",
            lock_guard=late_guard,
        )
    request = _request(db, client)
    view = delegated_operations._lineup_view(request, _principal(entry), _scope(db, round_, scope_row, entry))
    f1 = _lock_by_position(view)["F1"]
    assert f1["state"] == "locked"
    assert f1["editable"] is False
    assert f1["season_player_id"] == bye_player.season_player_id  # the rejected draft edit never took effect
