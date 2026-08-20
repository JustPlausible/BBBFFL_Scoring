import json
from pathlib import Path

import pytest

from app.scoring import ROSTER_SLOTS
from app.teams import TeamConfigError, load_teams

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "grand_final_teams.json"


def _write_config(tmp_path, team_a_roster, team_b_roster=None):
    if team_b_roster is None:
        team_b_roster = {slot: 900 + i for i, slot in enumerate(ROSTER_SLOTS)}
    config = {
        "teams": [
            {"team_key": "team_a", "name": "Team A", "roster": team_a_roster},
            {"team_key": "team_b", "name": "Team B", "roster": team_b_roster},
        ]
    }
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(config))
    return str(path)


def test_sample_grand_final_config_loads_and_has_two_full_rosters():
    teams = load_teams(str(DATA_PATH))
    assert len(teams) == 2
    assert {t.team_key for t in teams} == {"team_a", "team_b"}
    for team in teams:
        assert set(team.roster.keys()) == set(ROSTER_SLOTS)


def test_fully_populated_roster_still_loads(tmp_path):
    roster = {slot: 100 + i for i, slot in enumerate(ROSTER_SLOTS)}
    path = _write_config(tmp_path, roster)

    teams = load_teams(path)

    team_a = next(t for t in teams if t.team_key == "team_a")
    assert team_a.roster == roster
    assert all(v is not None for v in team_a.roster.values())


def test_partial_roster_with_null_slots_loads_successfully(tmp_path):
    """The Thursday-night Interchange loophole: only the Interchange player
    is named, everything else awaits Friday team announcements."""
    roster = dict.fromkeys(ROSTER_SLOTS)
    roster["Interchange"] = 396
    path = _write_config(tmp_path, roster)

    teams = load_teams(path)

    team_a = next(t for t in teams if t.team_key == "team_a")
    assert set(team_a.roster.keys()) == set(ROSTER_SLOTS)
    assert team_a.roster["Interchange"] == 396
    for slot in ROSTER_SLOTS:
        if slot != "Interchange":
            assert team_a.roster[slot] is None


def test_missing_roster_key_still_fails_validation(tmp_path):
    roster = {slot: 100 + i for i, slot in enumerate(ROSTER_SLOTS)}
    del roster["Ruck"]
    path = _write_config(tmp_path, roster)

    with pytest.raises(TeamConfigError):
        load_teams(path)


def test_invalid_non_null_player_id_still_fails(tmp_path):
    roster = {slot: 100 + i for i, slot in enumerate(ROSTER_SLOTS)}
    roster["Forward1"] = "not-a-number"
    path = _write_config(tmp_path, roster)

    with pytest.raises(ValueError):
        load_teams(path)
