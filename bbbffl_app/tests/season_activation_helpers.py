"""Shared season-builder for issue #239's season-activation tests: a season
whose entries/player-pool/completed-squads/ordinary-competition/fixture-draw/
preseason-draft are each individually switchable, so a test can stop at
exactly the readiness state it needs to exercise. Reuses the same domain-
repository calls `tests/season_setup_helpers.py`/`tests/admin_dashboard_
helpers.py` already establish (`SeasonRepository.initialize_ordinary_
competition`, `DraftRepository.accept_order`/`next_pick`/`execute_pick`/
`finalize`, `FixtureRepository.save_draft`/`freeze`) rather than re-deriving
draft/fixture/ownership mechanics."""

from app.audit import ActorContext
from app.draft import DraftRepository
from app.fixtures import FixtureRepository
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.preseason import PreseasonRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection

ACTOR = ActorContext.anonymous_operator("test")
TEAM_COUNT = 10


def build_activation_ready_season(
    db=None,
    *,
    year: int,
    entry_count: int = TEAM_COUNT,
    regular_season_round_count: int = 3,
    squad_limit: int = 2,
    create_entries: bool = True,
    initialize_competition: bool = True,
    populate_pool: bool = True,
    configure_squad: bool = True,
    accept_order: bool = True,
    complete_picks: bool = True,
    finalize_draft: bool = True,
    open_preseason: bool = True,
    close_preseason: bool = True,
    freeze_fixture: bool = True,
):
    """Build a season with each activation-readiness prerequisite
    individually switchable. Every flag only narrows downward -- a caller
    asking for `finalize_draft` need not also spell out
    `complete_picks`/`accept_order`, and `close_preseason`/`open_preseason`
    are silently forced off when their own prerequisite is off (an explicit
    `open_preseason=False` therefore also disables `close_preseason`,
    regardless of that parameter's own default) -- `freeze_fixture` needs
    `create_entries` only. Callers pass exactly the flags for the readiness
    state under test. A fully-ready season needs the preseason trade window
    closed, not merely a finalized draft: `close_window` is the operation
    that validates every squad and freezes the authoritative opening-squad
    snapshot (see `app/preseason.py`'s module docstring)."""
    complete_picks = complete_picks and accept_order and create_entries
    finalize_draft = finalize_draft and complete_picks
    open_preseason = open_preseason and finalize_draft
    close_preseason = close_preseason and open_preseason
    freeze_fixture = freeze_fixture and create_entries

    db = db or migrated_connection()
    seasons = SeasonRepository(db)
    season = seasons.create_season(year, f"{year} BBBFFL Season", regular_season_round_count=regular_season_round_count)

    identities = IdentityRepository(db)
    entries = []
    if create_entries:
        for number in range(1, entry_count + 1):
            coach = identities.create_coach(f"Coach {year}-{number}")
            entries.append(
                identities.create_entry(
                    season.season_id, f"licence-{year}-{number}", coach.coach_id, f"Team {year}-{number}"
                )
            )

    if initialize_competition:
        seasons.initialize_ordinary_competition(season.season_id, actor=ACTOR, reason="test setup")

    pool = PlayerPoolRepository(db)
    players = []
    if populate_pool:
        needed = max(entry_count * squad_limit, 1)
        players = [pool.refresh_player(season.season_id, 9000 + n, f"Player {year}-{n}") for n in range(needed)]

    ownership = OwnershipRepository(db)
    if configure_squad:
        ownership.configure_squad_limit(season.season_id, squad_limit)

    draft = DraftRepository(db)
    if accept_order and entries:
        draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    if complete_picks:
        for _ in range(entry_count * squad_limit):
            pick = draft.next_pick(season.season_id)
            draft.execute_pick(
                season.season_id, pick.current_season_entry_id, players[pick.overall_number - 1].season_player_id
            )
    if finalize_draft:
        draft.finalize(season.season_id)

    preseason = PreseasonRepository(db)
    if open_preseason:
        preseason.open_window(season.season_id)
    if close_preseason:
        preseason.close_window(season.season_id)

    fixtures = FixtureRepository(db)
    if freeze_fixture:
        fixtures.save_draft(season.season_id, [entry.season_entry_id for entry in entries])
        fixtures.freeze(season.season_id)

    return {
        "database": db,
        "season": season,
        "entries": entries,
        "players": players,
        "seasons": seasons,
        "identities": identities,
        "draft": draft,
        "preseason": preseason,
        "fixtures": fixtures,
        "ownership": ownership,
        "player_pool": pool,
    }
