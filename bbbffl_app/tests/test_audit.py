"""Invariant/regression coverage for the append-only audit-event boundary
(see app/audit.py and docs/audit-events.md).

Uses the same `decisions` fixture (a migrated SQLite DecisionsRepository)
that tests/test_service.py relies on, plus a matching AuditEventRepository
over the same connection so a test can read back what a mutation recorded.
"""

import pytest
from sqlalchemy.exc import IntegrityError

import app.db as db_module
from app.audit import DNP_CHANGED, OVERRIDE_CHANGED, ActorContext, AuditEventRepository, append_event
from app.db import transaction
from tests.db_helpers import migrated_connection


@pytest.fixture
def audit_events(decisions):
    return AuditEventRepository(decisions.conn)


# -- DNP: create, change, history preserved --------------------------------


def test_dnp_change_creates_one_audit_event(decisions, audit_events):
    decisions.set_dnp("team_a", "Forward1", True, actor=ActorContext.anonymous_operator(role="scorer"))

    events = audit_events.list_events(entity_type="scoring.slot")
    assert len(events) == 1
    event = events[0]
    assert event.action == DNP_CHANGED
    assert event.entity_id == "grand_final:team_a:Forward1"
    assert event.before_state == {"dnp": None}
    assert event.after_state == {"dnp": True}


def test_second_dnp_change_appends_and_first_event_is_unchanged(decisions, audit_events):
    decisions.set_dnp("team_a", "Forward1", True)
    first = audit_events.list_events(entity_type="scoring.slot")[0]

    decisions.set_dnp("team_a", "Forward1", False)

    events = audit_events.list_events(entity_type="scoring.slot")
    assert len(events) == 2
    # The first event, read again after a second mutation, is byte-for-byte
    # identical to what was read before that second mutation happened.
    assert events[0] == first
    second = events[1]
    assert second.before_state == {"dnp": True}
    assert second.after_state == {"dnp": False}
    # before/after chain together to describe the sequence: event 2's
    # "before" is event 1's "after".
    assert second.before_state["dnp"] == first.after_state["dnp"]


def test_dnp_sequence_is_useful_for_a_second_slot_independently(decisions, audit_events):
    decisions.set_dnp("team_a", "Forward1", True)
    decisions.set_dnp("team_a", "Forward2", True)
    decisions.set_dnp("team_a", "Forward1", False)

    forward1_events = audit_events.list_events(entity_id="grand_final:team_a:Forward1")
    forward2_events = audit_events.list_events(entity_id="grand_final:team_a:Forward2")
    assert [e.after_state["dnp"] for e in forward1_events] == [True, False]
    assert [e.after_state["dnp"] for e in forward2_events] == [True]


# -- Manual override: create, change, append not update ---------------------


def test_override_change_emits_attributable_event(decisions, audit_events):
    decisions.set_override(
        "team_a", "Ruck", 42.0, "late data correction", actor=ActorContext.anonymous_operator(role="scorer")
    )

    events = audit_events.list_events(entity_type="scoring.override")
    assert len(events) == 1
    event = events[0]
    assert event.action == OVERRIDE_CHANGED
    assert event.actor_type == "anonymous_operator"
    assert event.actor_role == "scorer"
    assert event.reason == "late data correction"
    assert event.after_state == {"override_score": 42.0, "reason": "late data correction"}


def test_repeated_override_changes_append_rather_than_update(decisions, audit_events):
    decisions.set_override("team_a", "Ruck", 42.0, "first correction")
    decisions.set_override("team_a", "Ruck", 55.0, "revised correction")
    decisions.set_override("team_a", "Ruck", None, None)  # clears the override

    events = audit_events.list_events(entity_type="scoring.override", entity_id="grand_final:team_a:Ruck")
    assert [e.after_state["override_score"] for e in events] == [42.0, 55.0, None]
    # Every prior event is untouched by the later ones.
    assert events[0].before_state == {"override_score": None, "reason": None}
    assert events[1].before_state == {"override_score": 42.0, "reason": "first correction"}
    assert events[2].before_state == {"override_score": 55.0, "reason": "revised correction"}


def test_clearing_an_override_records_a_null_after_state_even_with_an_explanatory_reason(decisions, audit_events):
    """override_score=None deletes the score_override row -- get_overrides()
    reports nothing for it afterwards -- so after_state must say the same
    (None/None), never a residual reason string that would disagree with
    the actual domain state. The caller's explanatory text is still kept,
    but as the event's own `reason` (why it was cleared), not as part of
    after_state."""
    decisions.set_override("team_a", "Ruck", 42.0, "first correction")
    decisions.set_override("team_a", "Ruck", None, "clearing because it was a mistake")

    events = audit_events.list_events(entity_type="scoring.override", entity_id="grand_final:team_a:Ruck")
    clear_event = events[-1]
    assert clear_event.after_state == {"override_score": None, "reason": None}
    assert clear_event.reason == "clearing because it was a mistake"
    assert decisions.get_overrides() == {}


# -- Deterministic ordering ---------------------------------------------------


def test_events_are_returned_in_deterministic_historical_order(decisions, audit_events):
    decisions.set_dnp("team_a", "Forward1", True)
    decisions.set_dnp("team_a", "Forward2", True)
    decisions.set_dnp("team_a", "Forward1", False)
    decisions.set_override("team_a", "Ruck", 10.0, "x")

    events = audit_events.list_events()
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))


def test_limit_keeps_the_most_recent_events_not_the_oldest(decisions, audit_events):
    """A limited read of a diagnostic surface should stay useful once more
    events exist than the limit -- it must not get permanently stuck
    showing only the oldest `limit` rows forever."""
    for i in range(5):
        decisions.set_dnp("team_a", "Forward1", i % 2 == 0)

    limited = audit_events.list_events(limit=2)
    all_events = audit_events.list_events()
    assert [e.event_id for e in limited] == [e.event_id for e in all_events[-2:]]
    # Still chronologically ascending within the returned page.
    assert [e.sequence for e in limited] == sorted(e.sequence for e in limited)


# -- Actor/context -------------------------------------------------------------


def test_actor_and_context_are_explicitly_recorded(decisions, audit_events):
    decisions.set_dnp("team_a", "Forward1", True, actor=ActorContext.anonymous_operator(role="admin"))
    event = audit_events.list_events()[0]
    assert event.actor_type == "anonymous_operator"
    assert event.actor_role == "admin"


def test_default_actor_is_a_well_defined_pre_authentication_identity(decisions, audit_events):
    """Call sites that don't yet care about auditing (most of the existing
    scoring test suite) still get a real, well-defined actor -- never a
    silently-empty one."""
    decisions.set_dnp("team_a", "Forward1", True)
    event = audit_events.list_events()[0]
    assert event.actor_type == "anonymous_operator"


@pytest.mark.parametrize("actor_type", ["coach", "scorer", "admin", "authenticated_user", ""])
def test_unauthenticated_actions_cannot_masquerade_as_authenticated_identities(decisions, actor_type):
    """Package 19/20 introduces real authentication. Until then, nothing in
    this codebase may claim one of its actor types -- append_event refuses
    any actor_type outside the pre-auth allowlist (system/legacy/
    anonymous_operator)."""
    bogus_actor = ActorContext(actor_type=actor_type)
    with pytest.raises(ValueError, match="Unknown actor_type"):
        decisions.set_dnp("team_a", "Forward1", True, actor=bogus_actor)


def test_system_and_legacy_actors_are_accepted_and_distinguishable(decisions, audit_events):
    decisions.set_dnp("team_a", "Forward1", True, actor=ActorContext.system())
    decisions.set_dnp("team_b", "Forward1", True, actor=ActorContext.legacy())

    events = {e.entity_id: e for e in audit_events.list_events()}
    assert events["grand_final:team_a:Forward1"].actor_type == "system"
    assert events["grand_final:team_b:Forward1"].actor_type == "legacy"


# -- Payload/schema version ------------------------------------------------


def test_event_payload_version_is_persisted(decisions, audit_events):
    decisions.set_dnp("team_a", "Forward1", True)
    event = audit_events.list_events()[0]
    assert event.payload_version == 1


# -- Append-only enforcement -------------------------------------------------


def test_audit_event_repository_exposes_no_update_or_delete():
    assert not hasattr(AuditEventRepository, "update_event")
    assert not hasattr(AuditEventRepository, "delete_event")


def test_database_rejects_update_to_existing_audit_event(decisions, audit_events):
    decisions.set_dnp("team_a", "Forward1", True)
    event = audit_events.list_events()[0]

    with pytest.raises(IntegrityError, match="append-only"):
        decisions.conn.execute("UPDATE audit_event SET reason = 'tampered' WHERE event_id = ?", (event.event_id,))


def test_database_rejects_delete_of_existing_audit_event(decisions, audit_events):
    decisions.set_dnp("team_a", "Forward1", True)
    event = audit_events.list_events()[0]

    with pytest.raises(IntegrityError, match="append-only"):
        decisions.conn.execute("DELETE FROM audit_event WHERE event_id = ?", (event.event_id,))

    # The row genuinely survived the rejected attempt.
    assert audit_events.get_event(event.event_id) is not None


# -- Transactional boundary ---------------------------------------------------


def test_failed_audit_append_rolls_back_the_domain_mutation(decisions, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(db_module, "append_event", _boom)

    with pytest.raises(RuntimeError, match="simulated audit failure"):
        decisions.set_dnp("team_a", "Forward1", True)

    # The domain write issued on the same connection, before append_event
    # was called, never committed either.
    assert decisions.get_dnp_map() == {}


def test_failed_audit_append_rolls_back_an_override_mutation(decisions, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(db_module, "append_event", _boom)

    with pytest.raises(RuntimeError, match="simulated audit failure"):
        decisions.set_override("team_a", "Ruck", 42.0, "reason")

    assert decisions.get_overrides() == {}


# -- Correlation IDs ------------------------------------------------------


def test_multiple_events_can_share_a_correlation_id(decisions, audit_events):
    correlation_id = "cmd-12345"
    decisions.set_dnp("team_a", "Forward1", True, correlation_id=correlation_id)
    decisions.set_interchange_assignment("team_a", "Forward1", correlation_id=correlation_id)
    decisions.set_dnp("team_a", "Forward2", True)  # independent command, own correlation_id

    grouped = audit_events.list_events(correlation_id=correlation_id)
    assert len(grouped) == 2
    assert {e.action for e in grouped} == {DNP_CHANGED, "scoring.interchange.changed"}

    ungrouped = audit_events.list_events(entity_id="grand_final:team_a:Forward2")
    assert ungrouped[0].correlation_id != correlation_id


def test_append_event_generates_a_correlation_id_when_none_is_given(decisions):
    conn = migrated_connection()
    repo = AuditEventRepository(conn)
    with transaction(conn) as t:
        event = append_event(
            t,
            actor=ActorContext.system(),
            action="scoring.dnp.changed",
            entity_type="scoring.slot",
            entity_id="grand_final:team_a:Forward1",
            after_state={"dnp": True},
        )
    assert event.correlation_id
    assert repo.get_event(event.event_id).correlation_id == event.correlation_id


# -- Reconstructing a representative scorer-change sequence -----------------


def test_trail_reconstructs_a_representative_scorer_change_sequence(decisions, audit_events):
    """A DNP is called, the interchange is assigned to cover it, a manual
    override corrects the resulting score, and the round is finalised. The
    resulting audit trail should read back as exactly that story, in order,
    with each step's before matching the previous step's after where the
    same entity is touched twice."""
    decisions.set_dnp("team_a", "Forward1", True, reason="Late withdrawal")
    decisions.set_interchange_assignment("team_a", "Forward1")
    decisions.set_override("team_a", "Forward1", 24.0, "Interchange potential score confirmed")
    decisions.set_dnp("team_a", "Forward1", False, reason="Withdrawal reversed; player played")
    decisions.finalize("Round confirmed by scorer")

    trail = audit_events.list_events()
    actions = [e.action for e in trail]
    assert actions == [
        "scoring.dnp.changed",
        "scoring.interchange.changed",
        "scoring.override.changed",
        "scoring.dnp.changed",
        "scoring.result.finalized",
    ]

    dnp_events = [e for e in trail if e.entity_type == "scoring.slot" and e.entity_id == "grand_final:team_a:Forward1"]
    assert dnp_events[0].after_state == {"dnp": True}
    assert dnp_events[0].reason == "Late withdrawal"
    assert dnp_events[1].before_state == {"dnp": True}
    assert dnp_events[1].after_state == {"dnp": False}
    assert dnp_events[1].reason == "Withdrawal reversed; player played"

    finalize_event = trail[-1]
    assert finalize_event.entity_type == "scoring.matchup"
    assert finalize_event.after_state["finalized"] is True
    assert finalize_event.reason == "Round confirmed by scorer"


# -- Existing scoring/public-read behaviour is unchanged ---------------------


def test_existing_repository_read_apis_are_unaffected_by_auditing(decisions):
    """The DNP/interchange/override/finalize read paths that the rest of the
    application (and its tests) depend on still return exactly the same
    shapes now that every write also records an audit event."""
    decisions.set_dnp("team_a", "Forward1", True)
    decisions.set_interchange_assignment("team_a", "Forward1")
    decisions.set_override("team_a", "Forward1", 24.0, "reason")

    assert decisions.get_dnp_map() == {("team_a", "Forward1"): True}
    assert decisions.get_interchange_assignments()["team_a"].target_position == "Forward1"
    assert decisions.get_overrides()[("team_a", "Forward1")].override_score == 24.0

    state = decisions.get_matchup_state()
    assert state.finalized is False
