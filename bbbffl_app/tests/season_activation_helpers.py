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
    freeze_fixture: bool = True,
):
    """Build a season with each activation-readiness prerequisite
    individually switchable. Later stages imply their prerequisites (a
    caller asking for `finalize_draft` need not also spell out
    `complete_picks`/`accept_order`; `freeze_fixture` needs `create_entries`
    only) -- callers pass exactly the flags for the readiness state under
    test."""
    complete_picks = complete_picks and accept_order and create_entries
    finalize_draft = finalize_draft and complete_picks
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
        "fixtures": fixtures,
        "ownership": ownership,
        "player_pool": pool,
    }
