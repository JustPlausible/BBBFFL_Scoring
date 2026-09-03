"""Season Centre application-service behaviour (issue #100): the operator
read-model over `app.season`/`app.identity`, and the thin create/edit
command wrappers around them. Exercises the domain layer directly, the same
way `tests/test_identity.py` and `tests/test_preseason.py` do; the HTTP
surface is covered separately in `tests/test_season_centre_api.py`.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.draft import DraftRepository
from app.identity import IdentityRepository
from app.player_pool import PlayerPoolRepository
from app.preseason import PreseasonRepository
from app.season import SeasonRepository
from app.season_centre import (
    SeasonCentreError,
    build_season_centre,
    create_coach,
    create_entry,
    create_season,
    list_coaches_overview,
    list_seasons_overview,
    rename_team,
    transfer_entry,
    update_coach,
)
from tests.db_helpers import migrated_connection

ACTOR = ActorContext.anonymous_operator("admin")


@pytest.fixture
def repos():
    db = migrated_connection()
    return {
        "database": db,
        "seasons": SeasonRepository(db),
        "identities": IdentityRepository(db),
        "draft": DraftRepository(db),
        "preseason": PreseasonRepository(db),
        "player_pool": PlayerPoolRepository(db),
        "lifecycle": CompetitionLifecycleRepository(db),
    }


def _build(repos, season_id):
    return build_season_centre(
        repos["seasons"],
        repos["identities"],
        repos["draft"],
        repos["preseason"],
        repos["player_pool"],
        repos["lifecycle"],
        season_id,
        repos["database"],
    )


def test_season_centre_is_reachable_for_the_2026_replay_season_and_shows_identity(repos):
    season = create_season(repos["seasons"], 2026, "2026 Replay")
    centre = _build(repos, season["season_id"])
    assert centre["season"]["year"] == 2026
    assert centre["season"]["label"] == "2026 Replay"
    assert centre["season"]["lifecycle_state"] == "setup"
    assert centre["entries"] == []
    assert centre["readiness"]["entries_established"] == 0


def test_ten_replay_entries_can_be_established_with_recognisable_identities(repos):
    season = create_season(repos["seasons"], 2026, "2026 Replay")
    coach_names = [f"Coach {letter}" for letter in "ABCDEFGHIJ"]
    team_names = [f"BBBFFL Team {letter}" for letter in "ABCDEFGHIJ"]
    for coach_name, team_name in zip(coach_names, team_names):
        coach = create_coach(repos["identities"], coach_name)
        create_entry(repos["identities"], season["season_id"], coach["coach_id"], team_name, actor=ACTOR)

    centre = _build(repos, season["season_id"])
    assert centre["readiness"]["entries_established"] == 10
    assert centre["readiness"]["distinct_coaches"] == 10
    assert sorted(entry["team_name"] for entry in centre["entries"]) == sorted(team_names)
    assert sorted(entry["coach_display_name"] for entry in centre["entries"]) == sorted(coach_names)
    # No synthetic "Team A"/"Player 1" fallback labels leak in -- every
    # entry carries the real supplied identity.
    assert all(entry["team_name"] in team_names for entry in centre["entries"])


def test_entries_and_coach_display_names_render_correctly(repos):
    season = create_season(repos["seasons"], 2027, "2027")
    coach = create_coach(repos["identities"], "Barry Smith", email="barry@example.test")
    entry = create_entry(repos["identities"], season["season_id"], coach["coach_id"], "The Mighty Ducks", actor=ACTOR)

    centre = _build(repos, season["season_id"])
    (view,) = centre["entries"]
    assert view["season_entry_id"] == entry["season_entry_id"]
    assert view["team_name"] == "The Mighty Ducks"
    assert view["coach_display_name"] == "Barry Smith"
    assert view["coach_id"] == coach["coach_id"]


def test_permitted_edits_persist_through_the_domain_repository_layer(repos):
    season = create_season(repos["seasons"], 2027, "2027")
    coach = create_coach(repos["identities"], "Original Coach")
    entry = create_entry(repos["identities"], season["season_id"], coach["coach_id"], "Original Name", actor=ACTOR)

    rename_team(repos["identities"], entry["season_entry_id"], "Renamed", actor=ACTOR, reason="operator edit")

    reloaded = repos["identities"].list_entries(season["season_id"])
    assert reloaded[0].team_name == "Renamed"
    # Persisted straight through the same repository a fresh read uses --
    # not merely reflected back from the command's own return value.
    public = repos["identities"].get_public_team(entry["season_entry_id"])
    assert public.team_name == "Renamed"


def test_editing_display_names_does_not_alter_stable_season_entry_id(repos):
    season = create_season(repos["seasons"], 2027, "2027")
    original_coach = create_coach(repos["identities"], "Original Coach")
    replacement_coach = create_coach(repos["identities"], "Replacement Coach")
    entry = create_entry(
        repos["identities"], season["season_id"], original_coach["coach_id"], "Original Name", actor=ACTOR
    )
    entry_id = entry["season_entry_id"]

    rename_team(repos["identities"], entry_id, "New Public Name", actor=ACTOR)
    transfer_entry(repos["identities"], entry_id, replacement_coach["coach_id"], actor=ACTOR)

    centre = _build(repos, season["season_id"])
    (view,) = centre["entries"]
    assert view["season_entry_id"] == entry_id
    assert view["team_name"] == "New Public Name"
    assert view["coach_display_name"] == "Replacement Coach"


def test_coach_private_data_is_not_exposed_by_the_season_centre_read_model(repos):
    season = create_season(repos["seasons"], 2027, "2027")
    coach = create_coach(repos["identities"], "Private Person", email="never-shown@example.test", phone="0400000000")
    create_entry(repos["identities"], season["season_id"], coach["coach_id"], "Team", actor=ACTOR)

    centre = _build(repos, season["season_id"])
    (view,) = centre["entries"]
    assert set(view) == {
        "season_entry_id",
        "season_id",
        "licence_key",
        "created_at",
        "team_name",
        "coach_id",
        "coach_display_name",
    }
    assert "never-shown@example.test" not in repr(centre)


def test_2026_replay_and_2027_season_state_stay_explicitly_separated(repos):
    replay = create_season(repos["seasons"], 2026, "2026 Replay")
    live = create_season(repos["seasons"], 2027, "2027")
    replay_coach = create_coach(repos["identities"], "Replay Coach")
    live_coach = create_coach(repos["identities"], "Live Coach")
    create_entry(repos["identities"], replay["season_id"], replay_coach["coach_id"], "Replay Team", actor=ACTOR)
    create_entry(repos["identities"], live["season_id"], live_coach["coach_id"], "Live Team", actor=ACTOR)

    replay_centre = _build(repos, replay["season_id"])
    live_centre = _build(repos, live["season_id"])
    assert [e["team_name"] for e in replay_centre["entries"]] == ["Replay Team"]
    assert [e["team_name"] for e in live_centre["entries"]] == ["Live Team"]
    assert replay_centre["season"]["season_id"] != live_centre["season"]["season_id"]


def test_build_season_centre_raises_for_unknown_season(repos):
    with pytest.raises(KeyError):
        _build(repos, "missing-season")


def test_create_entry_rejects_blank_team_name(repos):
    season = create_season(repos["seasons"], 2027, "2027")
    coach = create_coach(repos["identities"], "Coach")
    with pytest.raises(SeasonCentreError):
        create_entry(repos["identities"], season["season_id"], coach["coach_id"], "   ", actor=ACTOR)


def test_create_coach_rejects_blank_display_name(repos):
    with pytest.raises(SeasonCentreError):
        create_coach(repos["identities"], "")


def test_create_entry_rejects_unknown_coach_with_a_domain_error_not_a_raw_integrity_error(repos):
    season = create_season(repos["seasons"], 2027, "2027")
    with pytest.raises(SeasonCentreError) as excinfo:
        create_entry(repos["identities"], season["season_id"], "missing-coach", "Team", actor=ACTOR)
    assert not isinstance(excinfo.value, IntegrityError)


def test_create_entry_rejects_duplicate_licence_key(repos):
    season = create_season(repos["seasons"], 2027, "2027")
    coach = create_coach(repos["identities"], "Coach")
    create_entry(
        repos["identities"], season["season_id"], coach["coach_id"], "Team A", licence_key="fixed-key", actor=ACTOR
    )
    with pytest.raises(SeasonCentreError):
        create_entry(
            repos["identities"], season["season_id"], coach["coach_id"], "Team B", licence_key="fixed-key", actor=ACTOR
        )


def test_create_season_rejects_duplicate_year(repos):
    create_season(repos["seasons"], 2027, "2027")
    with pytest.raises(SeasonCentreError):
        create_season(repos["seasons"], 2027, "2027 (again)")


def test_update_coach_changes_display_name_without_touching_season_entry_identity(repos):
    season = create_season(repos["seasons"], 2027, "2027")
    coach = create_coach(repos["identities"], "Old Name", email="keep@example.test")
    entry = create_entry(repos["identities"], season["season_id"], coach["coach_id"], "Team", actor=ACTOR)

    update_coach(repos["identities"], coach["coach_id"], display_name="New Name", actor=ACTOR)

    centre = _build(repos, season["season_id"])
    (view,) = centre["entries"]
    assert view["season_entry_id"] == entry["season_entry_id"]
    assert view["coach_display_name"] == "New Name"
    reloaded = repos["identities"].get_coach(coach["coach_id"])
    assert reloaded.email == "keep@example.test"


def test_update_coach_explicit_none_clears_email_but_omitting_it_leaves_it_alone(repos):
    """Regression test for a Codex review finding: `update_coach`'s
    keywords must distinguish "not supplied" (keep) from an explicit
    ``None`` (clear) -- see `app.identity.UNSET`."""
    coach = create_coach(repos["identities"], "Coach", email="old@example.test", phone="0400000000")

    # Omitting email/phone entirely leaves both untouched.
    update_coach(repos["identities"], coach["coach_id"], display_name="Coach Renamed", actor=ACTOR)
    reloaded = repos["identities"].get_coach(coach["coach_id"])
    assert reloaded.display_name == "Coach Renamed"
    assert reloaded.email == "old@example.test"
    assert reloaded.phone == "0400000000"

    # An explicit None clears the field, and it stops resolving logins.
    update_coach(repos["identities"], coach["coach_id"], email=None, actor=ACTOR)
    reloaded = repos["identities"].get_coach(coach["coach_id"])
    assert reloaded.email is None
    assert reloaded.phone == "0400000000"
    assert repos["identities"].get_coach_by_email("old@example.test") is None


def test_create_coach_rejects_duplicate_email_with_a_domain_error_not_a_raw_integrity_error(repos):
    create_coach(repos["identities"], "First Coach", email="shared@example.test")
    with pytest.raises(SeasonCentreError) as excinfo:
        create_coach(repos["identities"], "Second Coach", email="Shared@Example.Test")
    assert not isinstance(excinfo.value, IntegrityError)


def test_update_coach_rejects_email_already_used_by_another_coach(repos):
    create_coach(repos["identities"], "First Coach", email="taken@example.test")
    other = create_coach(repos["identities"], "Second Coach", email="free@example.test")
    with pytest.raises(SeasonCentreError):
        update_coach(repos["identities"], other["coach_id"], email="taken@example.test", actor=ACTOR)
    # Rejected update must not have partially applied.
    reloaded = repos["identities"].get_coach(other["coach_id"])
    assert reloaded.email == "free@example.test"


def test_list_seasons_and_list_coaches_overviews(repos):
    create_season(repos["seasons"], 2026, "2026 Replay")
    create_season(repos["seasons"], 2027, "2027")
    create_coach(repos["identities"], "Coach One")
    create_coach(repos["identities"], "Coach Two")

    years = {season["year"] for season in list_seasons_overview(repos["seasons"])}
    assert {2026, 2027} <= years
    names = {coach["display_name"] for coach in list_coaches_overview(repos["identities"])}
    assert {"Coach One", "Coach Two"} <= names
