"""Coach privacy, season licence, history, integrity and audit behaviour."""

from dataclasses import asdict

import pytest
from sqlalchemy.exc import IntegrityError

from app.audit import ActorContext, AuditEventRepository
from app.db import transaction
from app.identity import IdentityRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


@pytest.fixture
def repos():
    db = migrated_connection()
    return IdentityRepository(db), SeasonRepository(db)


def test_same_coach_has_collision_free_replay_and_live_entries(repos):
    identities, seasons = repos
    coach = identities.create_coach("Barry", email="private@example.test")
    replay = seasons.create_season(2026, "2026 Replay")
    live = seasons.create_season(2027, "2027")
    old = identities.create_entry(replay.season_id, "licence-01", coach.coach_id, "Old Boys")
    new = identities.create_entry(live.season_id, "licence-01", coach.coach_id, "New Boys")
    assert old.season_entry_id != new.season_entry_id
    assert {identities.list_assignments(e.season_entry_id)[0].coach_id for e in (old, new)} == {coach.coach_id}


def test_rename_preserves_name_and_person_history_and_public_privacy(repos):
    identities, seasons = repos
    coach = identities.create_coach("Private Person", email="secret@example.test", phone="0400000000", profile_notes="private")
    season = seasons.create_season(2027, "2027")
    entry = identities.create_entry(season.season_id, "licence-01", coach.coach_id, "Original", effective_at="2027-01-01")
    identities.rename_team(entry.season_entry_id, "Renamed", reason="coach request", effective_at="2027-02-01")
    names = identities.list_team_names(entry.season_entry_id)
    assert [(n.team_name, n.ended_at) for n in names] == [("Original", "2027-02-01"), ("Renamed", None)]
    assert identities.list_assignments(entry.season_entry_id)[0].coach_id == coach.coach_id
    public = asdict(identities.get_public_team(entry.season_entry_id))
    assert public == {"season_entry_id": entry.season_entry_id, "season_id": season.season_id, "licence_key": "licence-01", "team_name": "Renamed"}
    assert not ({"email", "phone", "profile_notes", "display_name", "coach_id"} & public.keys())


def test_transfer_retains_original_assignment_and_audit_attribution(repos):
    identities, seasons = repos
    original = identities.create_coach("Original", email="old@example.test")
    replacement = identities.create_coach("Replacement", email="new@example.test")
    season = seasons.create_season(2027, "2027")
    entry = identities.create_entry(season.season_id, "licence-01", original.coach_id, "Same Public Team", effective_at="2027-01-01")
    actor = ActorContext.anonymous_operator("scorer")
    identities.transfer_entry(entry.season_entry_id, replacement.coach_id, actor=actor, reason="league-approved replacement", effective_at="2027-03-01")
    history = identities.list_assignments(entry.season_entry_id)
    assert [(x.coach_id, x.ended_at) for x in history] == [(original.coach_id, "2027-03-01"), (replacement.coach_id, None)]
    events = AuditEventRepository(identities.database).list_events(entity_type="season_entry", entity_id=entry.season_entry_id)
    event = events[-1]
    assert event.action == "identity.season_entry.coach_changed"
    assert event.actor_type == "anonymous_operator" and event.actor_role == "scorer"
    assert event.reason == "league-approved replacement"
    assert event.before_state == {"coach_id": original.coach_id}


def test_uniqueness_foreign_keys_and_single_current_history(repos):
    identities, seasons = repos
    coach = identities.create_coach("Coach")
    season = seasons.create_season(2027, "2027")
    entry = identities.create_entry(season.season_id, "licence-01", coach.coach_id, "Team")
    with pytest.raises(IntegrityError):
        identities.create_entry(season.season_id, "licence-01", coach.coach_id, "Duplicate")
    with pytest.raises(IntegrityError):
        identities.create_entry("missing-season", "licence-02", coach.coach_id, "Orphan")
    with pytest.raises(IntegrityError):
        identities.create_entry(season.season_id, "licence-02", "missing-coach", "Orphan")
    with pytest.raises(IntegrityError):
        with transaction(identities.database) as conn:
            conn.execute("INSERT INTO season_entry_team_name_history VALUES (?, ?, ?, ?, ?, ?)", ("extra", entry.season_entry_id, "Conflict", "later", None, None))


def test_rename_and_creation_events_do_not_audit_private_contact_data(repos):
    identities, seasons = repos
    coach = identities.create_coach("Person", email="never-in-event@example.test")
    season = seasons.create_season(2027, "2027")
    entry = identities.create_entry(season.season_id, "licence", coach.coach_id, "Team", reason="initial allocation")
    identities.rename_team(entry.season_entry_id, "Team Two", reason="rename")
    events = AuditEventRepository(identities.database).list_events(entity_type="season_entry", entity_id=entry.season_entry_id)
    assert [event.action for event in events] == ["identity.season_entry.created", "identity.team_name.changed"]
    assert "never-in-event@example.test" not in repr(events)
