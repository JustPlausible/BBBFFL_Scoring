"""Issue #181's CLI quality-of-life: `scripts/replay_2026_midseason_draft.py`
resolves team/coach/player identities in its printed output rather than
showing raw UUIDs as the primary label, while still retaining the id in
parentheses for audit/debugging.
"""

import argparse

from app.midseason_draft import MidseasonDraftRepository
from app.season import SeasonRepository
from scripts.replay_2026_midseason_draft import cmd_confirm_ladder, cmd_delist, cmd_pick
from tests.midseason_draft_helpers import build_season


def _ns(**kwargs):
    return argparse.Namespace(reason=None, **kwargs)


def test_confirm_ladder_output_names_teams_not_only_uuids(capsys):
    ctx = build_season(year=7001, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season, entries, competition = ctx["season"], ctx["entries"], ctx["competition"]
    SeasonRepository(ctx["database"]).set_midseason_draft_trigger_round(season.season_id, 10)
    midseason = MidseasonDraftRepository(ctx["database"])

    worst_entry = entries[9]
    worst_team = ctx["identities"].get_public_team(worst_entry.season_entry_id)

    cmd_confirm_ladder(midseason, _ns(season_id=season.season_id, competition_id=competition.competition_id))
    output = capsys.readouterr().out

    assert worst_team.team_name in output
    # The raw id remains available as secondary/audit detail, never the
    # only identifier shown.
    assert worst_entry.season_entry_id in output
    assert "1. " in output


def test_delist_and_pick_output_name_the_player_not_only_the_id(capsys):
    ctx = build_season(year=7002, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season, entries, competition = ctx["season"], ctx["entries"], ctx["competition"]
    SeasonRepository(ctx["database"]).set_midseason_draft_trigger_round(season.season_id, 10)
    midseason = MidseasonDraftRepository(ctx["database"])

    midseason.confirm_ladder(season.season_id, competition.competition_id)
    midseason.open_delisting_window(season.season_id)

    worst_entry = entries[9]
    worst_squad = ctx["ownership"].current_squad(worst_entry.season_entry_id)
    ownership_period = worst_squad[0]
    player = ctx["player_pool"].get_by_id(ownership_period.season_player_id)

    cmd_delist(
        midseason,
        _ns(
            season_id=season.season_id,
            season_entry_id=worst_entry.season_entry_id,
            season_player_id=ownership_period.season_player_id,
        ),
    )
    output = capsys.readouterr().out
    assert player.display_name in output
    assert player.season_player_id in output

    midseason.lock_delistings(season.season_id)
    midseason.generate_selection_table(season.season_id)
    pool = midseason.available_player_pool(season.season_id)
    pick = midseason.next_pick(season.season_id)

    cmd_pick(
        midseason,
        _ns(
            season_id=season.season_id,
            season_entry_id=pick.current_season_entry_id,
            season_player_id=pool[0].season_player_id,
        ),
    )
    output = capsys.readouterr().out
    assert pool[0].display_name in output
    team = ctx["identities"].get_public_team(pick.current_season_entry_id)
    assert team.team_name in output
