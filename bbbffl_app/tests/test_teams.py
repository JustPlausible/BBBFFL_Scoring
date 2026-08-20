from pathlib import Path

from app.scoring import ROSTER_SLOTS
from app.teams import load_teams

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "grand_final_teams.json"


def test_sample_grand_final_config_loads_and_has_two_full_rosters():
    teams = load_teams(str(DATA_PATH))
    assert len(teams) == 2
    assert {t.team_key for t in teams} == {"team_a", "team_b"}
    for team in teams:
        assert set(team.roster.keys()) == set(ROSTER_SLOTS)
