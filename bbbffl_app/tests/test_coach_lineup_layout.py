"""Desktop lineup grouping coverage (issue #90): `coach_lineup.html` must
visually group Forwards (F1-F3), Midfield (M1-M3), Specialists (Ruck,
Tackler) and Interchange, via one shared slot-rendering macro, rather than
the previous flat two-column grid that split those groups apart.

Regression-only for the rest of the page: every original `position_<slot>`
form field name must still exist exactly once, whatever its group.
"""

import re

from app.lineups import POSITIONS
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import operational


def _group_sections(html: str) -> dict[str, str]:
    sections = re.findall(r'<section class="position-group" aria-label="([^"]+)">(.*?)</section>', html, re.S)
    assert sections, "no position-group sections found"
    return dict(sections)


def _position_names(section_html: str) -> list[str]:
    return re.findall(r'class="position">([^<]+)<', section_html)


def _render_lineup_html(monkeypatch):
    from types import SimpleNamespace

    from fastapi.templating import Jinja2Templates

    from app.coach_lineup import COACH_LINEUP_POSITION_GROUPS, COACH_LINEUP_POSITIONS, CoachLineupService
    from app.config import BASE_DIR

    database = migrated_connection()
    _, round_, entries = operational(database, 2051, 501)
    entry = entries[0]
    coach = database.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (entry.season_entry_id,),
    ).fetchone()
    scope = database.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()

    pool = PlayerPoolRepository(database)
    ownership = OwnershipRepository(database)
    ownership.configure_squad_limit(scope["season_id"], 9)
    for index, position in enumerate(POSITIONS):
        player = pool.refresh_player(scope["season_id"], 92000 + index, f"Player {position}")
        ownership.acquire(player.season_player_id, entry.season_entry_id, effective_at="2031-01-01T00:00:00+00:00")

    service = CoachLineupService(database, afl_client=SimpleNamespace())
    monkeypatch.setattr(service.lockouts, "lock_state", lambda *args, **kwargs: SimpleNamespace(positions={}))
    context = service.view(coach["coach_id"], scope["season_id"], round_.bbbffl_round_id)

    templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
    template = templates.get_template("coach_lineup.html")
    return template.render(
        coach=SimpleNamespace(display_name="Test Coach"),
        lineup=context,
        positions=COACH_LINEUP_POSITIONS,
        position_groups=COACH_LINEUP_POSITION_GROUPS,
        errors=(),
        notice=None,
        csrf_token="token",
    )


def test_desktop_positions_are_grouped_by_football_role(monkeypatch):
    html = _render_lineup_html(monkeypatch)
    groups = _group_sections(html)

    assert _position_names(groups["Forwards"]) == ["F1", "F2", "F3"]
    assert _position_names(groups["Midfield"]) == ["M1", "M2", "M3"]
    assert set(_position_names(groups["Specialists"])) == {"Ruck", "Tackler"}
    assert _position_names(groups["Interchange"]) == ["Interchange"]


def test_every_position_form_field_appears_exactly_once(monkeypatch):
    html = _render_lineup_html(monkeypatch)
    for position in POSITIONS:
        assert html.count(f'name="position_{position}"') == 1, position


def test_position_groups_cover_exactly_the_domain_positions():
    from app.coach_lineup import COACH_LINEUP_POSITION_GROUPS

    grouped = {position for _, group_positions in COACH_LINEUP_POSITION_GROUPS for position in group_positions}
    assert grouped == set(POSITIONS)
