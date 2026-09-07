"""Shared season-builder for `tests/test_admin_dashboard.py` and
`tests/test_admin_dashboard_api.py`: a fully governed, ten-team BBBFFL
season -- competition/rules, entries, a finalised draft, a closed
preseason window, a frozen fixture draw, an accepted AFL round mapping and
(optionally) an opened ordinary round -- with each stage individually
switchable so a test can stop at exactly the governance state it needs to
exercise (setup-only, draft-in-progress, preseason-open, round-preparation,
weekly-operations). Reuses the same domain-repository calls
`tests/test_competition_lifecycle.py`'s `configured`/`operational` and
`tests/test_preseason.py`'s `domain` already establish, rather than
re-deriving draft/preseason/fixture mechanics.
"""

from dataclasses import dataclass
from types import SimpleNamespace

from app.competition_lifecycle import CompetitionLifecycleRepository
from app.draft import DraftRepository
from app.fixtures import FixtureRepository
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.preseason import PreseasonRepository
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


@dataclass
class KnownRound:
    """Duck-typed `AflApiReferenceValidator` stand-in accepting exactly one
    (afl_season_id, afl_round_id) pair -- the same shape
    `tests/test_competition_lifecycle.py`'s own `KnownRound` uses."""

    reference: tuple[int, int]

    def round_exists(self, afl_season_id, afl_round_id):
        return (afl_season_id, afl_round_id) == self.reference


def build_governed_season(
    db=None,
    *,
    year: int,
    entries: int = 10,
    squad_limit: int = 1,
    accept_draft_order: bool = True,
    finalize_draft: bool = False,
    open_preseason: bool = False,
    close_preseason: bool = False,
    freeze_fixture: bool = False,
    accept_mapping: bool = False,
    open_round: bool = False,
    afl_round: int = 100,
) -> SimpleNamespace:
    """Build a season with each governance stage individually switchable.
    Later flags require the earlier ones (`open_round` implies
    `accept_mapping` implies `freeze_fixture`; `open_preseason`/
    `close_preseason` each imply `finalize_draft`) --
    callers pass exactly the flags for the state under test, matching
    `app.admin_dashboard`'s own stage progression
    (setup -> draft -> preseason -> round preparation -> weekly operations)."""
    # Later stages imply their prerequisites -- a caller asking for
    # `open_round` need not also spell out `accept_mapping`/
    # `freeze_fixture`/`finalize_draft`/`close_preseason`.
    if open_round:
        accept_mapping = True
    if accept_mapping:
        freeze_fixture = True
    if close_preseason:
        finalize_draft = True
    if open_preseason:
        finalize_draft = True

    db = db or migrated_connection()
    seasons = SeasonRepository(db)
    season = seasons.create_season(year, str(year))
    rules = seasons.create_rules_version(season.season_id, "ordinary", 1, f"Rules {year}")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "Ordinary", "ordinary"
    )
    logical_round = seasons.create_round(competition.competition_id, "round-1", "Round 1", 1)

    identities = IdentityRepository(db)
    season_entries = [
        identities.create_entry(
            season.season_id,
            f"licence-{year}-{number}",
            identities.create_coach(f"Coach {year}-{number}").coach_id,
            f"Team {year}-{number}",
        )
        for number in range(entries)
    ]

    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(season.season_id, squad_limit)
    pool = PlayerPoolRepository(db)
    players = [
        pool.refresh_player(season.season_id, number + 1, f"Player {year}-{number}")
        for number in range(entries * squad_limit)
    ]

    draft = DraftRepository(db)
    if accept_draft_order or finalize_draft or close_preseason:
        draft.accept_order(season.season_id, [entry.season_entry_id for entry in season_entries])
    if finalize_draft or close_preseason:
        for _ in range(entries * squad_limit):
            pick = draft.next_pick(season.season_id)
            draft.execute_pick(
                season.season_id, pick.current_season_entry_id, players[pick.overall_number - 1].season_player_id
            )
        draft.finalize(season.season_id)

    preseason = PreseasonRepository(db)
    window = None
    if open_preseason or close_preseason:
        window = preseason.open_window(season.season_id)
    if close_preseason:
        window = preseason.close_window(season.season_id)

    fixtures = FixtureRepository(db)
    if freeze_fixture:
        fixtures.save_draft(season.season_id, [entry.season_entry_id for entry in season_entries])
        fixtures.freeze(season.season_id)

    if accept_mapping or open_round:
        RoundMappingRepository(db).accept(logical_round.bbbffl_round_id, year, afl_round, KnownRound((year, afl_round)))

    lifecycle = CompetitionLifecycleRepository(db)
    if accept_mapping or open_round:
        lifecycle.create_ordinary_round(logical_round.bbbffl_round_id)
        if open_round:
            lifecycle.transition(logical_round.bbbffl_round_id, "open")

    return SimpleNamespace(
        database=db,
        season=season,
        rules=rules,
        competition=competition,
        logical_round=logical_round,
        entries=season_entries,
        players=players,
        ownership=ownership,
        player_pool=pool,
        draft=draft,
        preseason=preseason,
        window=window,
        fixtures=fixtures,
        lifecycle=lifecycle,
        seasons=seasons,
        identities=identities,
    )
