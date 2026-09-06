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
from app.lockouts import LockedSelectionError, LockoutTriggerRepository
from app.opening_round import OpeningRoundNominationRepository, OpeningRoundRuleRepository
from app.routes import delegated_operations
from tests.test_competition_lifecycle import KnownRound
from tests.test_lockouts import (
    ALL_MATCHES,
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


def afl_client(matches):
    """Duck-typed AFL client: `get_matches` ignores the requested AFL round
    id and always returns `matches` -- every scenario below uses a single
    mapped round, exactly like `RoundMatchFactsProvider`'s production
    composition expects (app/lockouts.py). `get_rounds` is a bare stub only
    so `LineupValidationService`'s (unrelated) availability advisory has
    something to call once a submission exists -- these tests are not
    about bye-round advisories."""
    return SimpleNamespace(get_matches=lambda afl_round_id: matches, get_rounds=lambda afl_season_id: [])


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


def test_main_lockout_locks_every_selected_ordinary_player_and_preserves_vacancies():
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
    # A deliberately vacant position keeps its documented semantics --
    # editable/"empty", never invented into a fabricated lock.
    assert locks["M2"]["state"] == "editable"
    assert locks["M2"]["lock_type"] == "vacant"
    assert locks["M2"]["reason_code"] == "empty"
    assert locks["M2"]["season_player_id"] is None


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
