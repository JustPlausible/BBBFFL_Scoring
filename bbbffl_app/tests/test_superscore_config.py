import json

import pytest

from app.scoring import ROSTER_SLOTS
from app.superscore import load_superscore_config
from app.teams import TeamConfigError


def _lineup(base: int) -> dict:
    return {slot: base + i for i, slot in enumerate(ROSTER_SLOTS)}


def _write_config(tmp_path, entries=None, **overrides):
    if entries is None:
        entries = [
            {"team_key": f"team_{n}", "coach": f"Coach {n}", "lineup": _lineup(n * 1000)}
            for n in range(1, 11)
        ]
    config = {
        "season": 2026,
        "afl_round": 20,
        "competition_type": "SUPERSCORE",
        "entries": entries,
        **overrides,
    }
    path = tmp_path / "superscore.json"
    path.write_text(json.dumps(config))
    return str(path)


def test_ten_superscore_entries_load(tmp_path):
    config = load_superscore_config(_write_config(tmp_path))

    assert config.season == 2026
    assert config.afl_round == 20
    assert len(config.entries) == 10
    assert {e.team_key for e in config.entries} == {f"team_{n}" for n in range(1, 11)}
    assert {e.name for e in config.entries} == {f"Coach {n}" for n in range(1, 11)}


def test_entries_reuse_the_same_roster_schema_as_grand_final(tmp_path):
    """SuperScore's 'lineup' must accept exactly the same nine BBBFFL slots
    as the Grand Final's 'roster' -- there is no separate SuperScore lineup
    schema."""
    config = load_superscore_config(_write_config(tmp_path))

    for entry in config.entries:
        assert set(entry.roster.keys()) == set(ROSTER_SLOTS)


def test_entry_count_is_not_hardcoded_to_two(tmp_path):
    """Unlike load_teams() (exactly two), SuperScore accepts an arbitrary
    entry count -- proving the loader doesn't inherit the Grand Final's
    two-team assumption."""
    entries = [{"team_key": "solo", "coach": "Solo Coach", "lineup": _lineup(1000)}]
    config = load_superscore_config(_write_config(tmp_path, entries=entries))
    assert len(config.entries) == 1


def test_wrong_competition_type_is_rejected(tmp_path):
    with pytest.raises(TeamConfigError):
        load_superscore_config(_write_config(tmp_path, competition_type="HEAD_TO_HEAD"))


def test_duplicate_team_key_is_rejected(tmp_path):
    entries = [
        {"team_key": "dup", "coach": "A", "lineup": _lineup(1000)},
        {"team_key": "dup", "coach": "B", "lineup": _lineup(2000)},
    ]
    with pytest.raises(TeamConfigError):
        load_superscore_config(_write_config(tmp_path, entries=entries))


def test_missing_lineup_slot_is_rejected(tmp_path):
    lineup = _lineup(1000)
    del lineup["Ruck"]
    entries = [{"team_key": "team_1", "coach": "Coach 1", "lineup": lineup}]
    with pytest.raises(TeamConfigError):
        load_superscore_config(_write_config(tmp_path, entries=entries))


def test_empty_entries_list_is_rejected(tmp_path):
    with pytest.raises(TeamConfigError):
        load_superscore_config(_write_config(tmp_path, entries=[]))
