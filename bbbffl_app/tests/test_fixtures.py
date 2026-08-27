"""Golden and persistence tests for the documented 2026 BBBFFL fixture."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.audit import ActorContext, AuditEventRepository
from app.db import transaction
from app.fixtures import BASE_ROTATION, FixtureRepository, fixture_number_rotation
from app.identity import IdentityRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


def _season_with_entries(database, year, prefix="Team"):
    season = SeasonRepository(database).create_season(year, str(year))
    identities = IdentityRepository(database)
    entries = []
    for number in range(1, 11):
        coach = identities.create_coach(f"Coach {year}-{number}")
        entries.append(identities.create_entry(season.season_id, f"licence-{number}", coach.coach_id, f"{prefix} {number}"))
    return season, entries


@pytest.fixture
def fixture_tree():
    database = migrated_connection()
    season, entries = _season_with_entries(database, 2026)
    return database, season, entries, FixtureRepository(database)


def test_documented_2026_fixture_number_golden_and_invariants():
    expected = (
        ((1, 2), (3, 4), (5, 6), (7, 8), (9, 10)),
        ((1, 3), (2, 8), (9, 6), (7, 4), (10, 5)),
        ((1, 4), (2, 6), (3, 5), (8, 10), (7, 9)),
        ((1, 6), (2, 4), (5, 7), (3, 10), (8, 9)),
        ((1, 5), (2, 7), (4, 10), (3, 9), (6, 8)),
        ((1, 7), (2, 9), (4, 5), (6, 10), (3, 8)),
        ((1, 8), (2, 10), (4, 6), (3, 7), (5, 9)),
        ((1, 9), (2, 5), (4, 8), (3, 6), (7, 10)),
        ((1, 10), (2, 3), (4, 9), (5, 8), (6, 7)),
    )
    assert BASE_ROTATION == expected
    rotation = fixture_number_rotation()
    assert rotation == fixture_number_rotation()  # deterministic
    assert len(rotation) == 20
    assert all(len(round_) == 5 for round_ in rotation)
    assert all(sorted(n for pair in round_ for n in pair) == list(range(1, 11)) for round_ in rotation)
    assert {frozenset(pair) for round_ in rotation[:9] for pair in round_} == {
        frozenset((a, b)) for a in range(1, 11) for b in range(a + 1, 11)
    }
    assert rotation[9:18] == tuple(tuple((away, home) for home, away in round_) for round_ in expected)
    assert rotation[18:] == expected[:2]


def test_persists_stable_entry_pairings_not_mutable_names(fixture_tree):
    database, season, entries, repository = fixture_tree
    draw = repository.save_draft(season.season_id, [entry.season_entry_id for entry in entries])
    before = repository.list_matchups(season.season_id)
    assert len(before) == 100
    IdentityRepository(database).rename_team(entries[0].season_entry_id, "Renamed")
    replacement = IdentityRepository(database).create_coach("Replacement")
    IdentityRepository(database).transfer_entry(entries[1].season_entry_id, replacement.coach_id)
    assert repository.list_matchups(season.season_id) == before
    assert repository.get_draw(season.season_id).fixture_draw_id == draw.fixture_draw_id


def test_seasons_have_independent_draws_without_leakage():
    database = migrated_connection()
    first, first_entries = _season_with_entries(database, 2026)
    second, second_entries = _season_with_entries(database, 2027)
    repository = FixtureRepository(database)
    first_draw = repository.save_draft(first.season_id, [e.season_entry_id for e in first_entries])
    second_draw = repository.save_draft(second.season_id, [e.season_entry_id for e in reversed(second_entries)])
    assert first_draw.fixture_draw_id != second_draw.fixture_draw_id
    assert {m.season_id for m in repository.list_matchups(first.season_id)} == {first.season_id}
    assert set(repository.fixture_numbers(first.season_id).values()).isdisjoint(repository.fixture_numbers(second.season_id).values())


def test_duplicate_fixture_assignment_and_cross_season_entry_are_rejected(fixture_tree):
    database, season, entries, repository = fixture_tree
    with pytest.raises(ValueError, match="distinct"):
        repository.save_draft(season.season_id, [entries[0].season_entry_id] * 10)
    repository.save_draft(season.season_id, [e.season_entry_id for e in entries])
    with pytest.raises(IntegrityError):
        with transaction(database) as conn:
            conn.execute("INSERT INTO season_fixture_number VALUES (?, ?, ?, ?)", (repository.get_draw(season.season_id).fixture_draw_id, season.season_id, 10, entries[0].season_entry_id))


def test_draft_correction_is_atomic_audited_and_freeze_is_immutable(fixture_tree):
    database, season, entries, repository = fixture_tree
    ordered = [e.season_entry_id for e in entries]
    draft = repository.save_draft(season.season_id, ordered)
    corrected_order = ordered[1::-1] + ordered[2:]
    corrected = repository.save_draft(season.season_id, corrected_order, actor=ActorContext.anonymous_operator("scorer"), reason="draw transcription correction")
    assert corrected.fixture_draw_id == draft.fixture_draw_id
    assert corrected.version == 2
    assert repository.list_matchups(season.season_id, 1)[0].home_season_entry_id == corrected_order[0]
    frozen = repository.freeze(season.season_id, reason="accepted by scorer")
    assert frozen.state == "frozen" and frozen.frozen_at
    with pytest.raises(ValueError, match="immutable"):
        repository.save_draft(season.season_id, ordered)
    with pytest.raises(IntegrityError, match="immutable"):
        with transaction(database) as conn:
            conn.execute("DELETE FROM season_fixture_matchup WHERE fixture_draw_id=?", (draft.fixture_draw_id,))
    events = AuditEventRepository(database).list_events(entity_type="fixture_draw", entity_id=draft.fixture_draw_id)
    assert [event.action for event in events] == ["fixture.draw.created", "fixture.draw.corrected", "fixture.draw.frozen"]
    assert events[1].reason == "draw transcription correction"


def test_draw_requires_all_ten_entries_of_its_season(fixture_tree):
    database, season, entries, repository = fixture_tree
    other, other_entries = _season_with_entries(database, 2027)
    invalid = [e.season_entry_id for e in entries[:-1]] + [other_entries[0].season_entry_id]
    with pytest.raises(ValueError, match="exactly one season"):
        repository.save_draft(season.season_id, invalid)
