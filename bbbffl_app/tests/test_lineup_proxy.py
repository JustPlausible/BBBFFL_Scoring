"""Scorer/admin proxy entry over the weekly-lineup aggregate (roadmap
package 22, issue #55). See app/lineup_proxy.py's module docstring: this
reuses `WeeklyLineupRepository.save_draft`/`submit` (#33) and
`app.audit.append_event` (#17) directly -- there is no second proxy
selection store or proxy audit log to test here, only the actor/reason
provenance and authorization this module adds on top."""

from datetime import timedelta

import pytest

from app.audit import ENTITY_TYPE_LINEUP, LINEUP_SUBMITTED, ActorContext, AuditEventRepository
from app.lineup_proxy import LineupProxyError, LineupProxyService, UnauthorizedProxyActorError
from app.lineups import WeeklyLineupRepository
from app.lockouts import LockedSelectionError, LockoutRepository, LockoutTriggerRepository
from tests.test_carry_forward import acquire_players, context
from tests.test_carry_forward_lockouts import acquire_club_player
from tests.test_lockouts import (
    ALL_MATCHES,
    EARLY_HOME,
    EARLY_MATCH_ID,
    EARLY_START,
    LATE_HOME,
    FakeMatchFacts,
    configure_selective,
)

SCORER = ActorContext.anonymous_operator("scorer")
ADMIN = ActorContext.anonymous_operator("admin")
COACH = ActorContext.anonymous_operator("coach")


def test_scorer_can_create_and_submit_a_lineup_on_behalf_of_an_entry_with_provenance():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 2)
    proxy = LineupProxyService(db)

    draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {"F1": players[0].season_player_id, "M1": players[1].season_player_id},
        expected_revision=0,
        actor=SCORER,
    )
    submitted = proxy.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        actor=SCORER,
        reason="coach unreachable before lockout, entering on their behalf",
    )

    assert submitted.positions == {
        "F1": players[0].season_player_id,
        "M1": players[1].season_player_id,
        **{p: None for p in ("F2", "F3", "M2", "M3", "Ruck", "Tackler", "Interchange")},
    }
    assert submitted.source_type == "scorer_proxy"
    assert submitted.source_type != "coach"
    assert submitted.actor_type == "anonymous_operator"
    assert submitted.actor_role == "scorer"
    assert submitted.reason == "coach unreachable before lockout, entering on their behalf"
    # The actor is the operator, never the receiving entry/coach.
    assert submitted.actor_id != entry.season_entry_id


def test_proxy_submission_requires_a_reason():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    proxy = LineupProxyService(db)
    draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {},
        expected_revision=0,
        actor=SCORER,
    )
    with pytest.raises(LineupProxyError):
        proxy.submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=0,
            actor=SCORER,
            reason="",
        )


@pytest.mark.parametrize("bad_actor", [COACH, ActorContext.system(), ActorContext.anonymous_operator(None)])
def test_non_operator_or_non_scorer_admin_actor_is_rejected(bad_actor):
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    proxy = LineupProxyService(db)
    with pytest.raises(UnauthorizedProxyActorError):
        proxy.create_or_amend(
            scope["season_id"],
            scope["competition_id"],
            rounds[0],
            entry.season_entry_id,
            {},
            expected_revision=0,
            actor=bad_actor,
        )
    lineup_id, _ = WeeklyLineupRepository(db).get_or_create_header(
        scope["season_id"], scope["competition_id"], rounds[0], entry.season_entry_id
    )
    with pytest.raises(UnauthorizedProxyActorError):
        proxy.submit(lineup_id, expected_draft_revision=1, expected_submission_version=0, actor=bad_actor, reason="x")


def test_admin_role_is_also_an_authorised_proxy_actor():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 1)
    proxy = LineupProxyService(db)
    draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {"F1": players[0].season_player_id},
        expected_revision=0,
        actor=ADMIN,
    )
    submitted = proxy.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        actor=ADMIN,
        reason="commissioner correction",
    )
    assert submitted.actor_role == "admin"
    assert submitted.source_type == "scorer_proxy"


def test_proxy_resubmission_preserves_every_prior_immutable_version():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 2)
    proxy = LineupProxyService(db)
    lineups = WeeklyLineupRepository(db)

    draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {"F1": players[0].season_player_id},
        expected_revision=0,
        actor=SCORER,
    )
    first = proxy.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        actor=SCORER,
        reason="first",
    )
    draft2 = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {"M1": players[1].season_player_id},
        expected_revision=draft.revision,
        actor=SCORER,
    )
    second = proxy.submit(
        draft2.lineup_id,
        expected_draft_revision=draft2.revision,
        expected_submission_version=first.version,
        actor=SCORER,
        reason="corrected",
    )

    assert second.version == first.version + 1
    # The first version is untouched, immutable history -- resubmission
    # never rewrites it.
    assert lineups.get_submission(draft.lineup_id, first.version) == first
    assert lineups.get_submission(draft.lineup_id, first.version).positions["F1"] == players[0].season_player_id
    assert lineups.get_effective_submission(draft.lineup_id) == second


def test_proxy_submission_writes_one_attributable_append_only_audit_event():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 1)
    proxy = LineupProxyService(db)
    draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {"F1": players[0].season_player_id},
        expected_revision=0,
        actor=SCORER,
    )
    submitted = proxy.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        actor=SCORER,
        reason="proxy entry",
    )

    events = AuditEventRepository(db).list_events(entity_type=ENTITY_TYPE_LINEUP, entity_id=draft.lineup_id)
    assert [event.action for event in events] == [LINEUP_SUBMITTED]
    event = events[0]
    assert event.actor_type == "anonymous_operator"
    assert event.actor_role == "scorer"
    assert event.reason == "proxy entry"
    assert event.entity_version == str(submitted.version)


# ---------------------------------------------------------------------------
# Lockout integration: proxy gets no special exemption (#34)
# ---------------------------------------------------------------------------


def test_proxy_cannot_replace_an_already_locked_player():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    early = acquire_club_player(pool, ownership, scope, entry, 1, EARLY_HOME)
    other_early = acquire_club_player(pool, ownership, scope, entry, 2, EARLY_HOME, name="Bench")
    lineups, proxy = WeeklyLineupRepository(db), LineupProxyService(db)
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, rounds[0], [EARLY_MATCH_ID], key="early-1", sequence=1)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))

    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {"F1": early.season_player_id},
        expected_revision=0,
    )
    first = lineups.submit(
        draft.lineup_id, expected_draft_revision=1, expected_submission_version=0, lock_guard=pre_lock_guard
    )

    draft2 = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {"F1": other_early.season_player_id},
        expected_revision=draft.revision,
        actor=SCORER,
    )
    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    with pytest.raises(LockedSelectionError, match="F1"):
        proxy.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=first.version,
            actor=SCORER,
            reason="attempted correction after lockout",
            lock_guard=late_guard,
        )


def test_proxy_can_still_change_editable_positions_after_lockout():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    early = acquire_club_player(pool, ownership, scope, entry, 1, EARLY_HOME)
    interchange_player = acquire_club_player(pool, ownership, scope, entry, 100, LATE_HOME, name="Interchange Late")
    lineups, proxy = WeeklyLineupRepository(db), LineupProxyService(db)
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, rounds[0], [EARLY_MATCH_ID], key="early-1", sequence=1)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))

    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {"F1": early.season_player_id},
        expected_revision=0,
    )
    first = lineups.submit(
        draft.lineup_id, expected_draft_revision=1, expected_submission_version=0, lock_guard=pre_lock_guard
    )

    draft2 = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entry.season_entry_id,
        {"F1": early.season_player_id, "Interchange": interchange_player.season_player_id},
        expected_revision=draft.revision,
        actor=SCORER,
    )
    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    submitted = proxy.submit(
        draft2.lineup_id,
        expected_draft_revision=draft2.revision,
        expected_submission_version=first.version,
        actor=SCORER,
        reason="filling still-open Interchange slot",
        lock_guard=late_guard,
    )
    assert submitted.positions["F1"] == early.season_player_id
    assert submitted.positions["Interchange"] == interchange_player.season_player_id
