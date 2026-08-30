"""Persistent Round 1 rehearsal bootstrap coverage (issue #85):
bootstrap state, duplicate protection, and replay-mode evidence
resolution. See tests/test_round1_rehearsal_acceptance.py for the HTTP
acceptance vertical driven through the real browser/API routes.
"""

import pytest

from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import connect
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.replay import ReplayAflDataSource, ReplayEvidenceError
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository
from scripts.bootstrap_round1_2026 import (
    ENTRY_COUNT,
    RehearsalAlreadyBootstrappedError,
    bootstrap_round1_2026,
)


@pytest.fixture
def rehearsal(tmp_path):
    db_path = tmp_path / "rehearsal.db"
    evidence_path = tmp_path / "evidence.json"
    return bootstrap_round1_2026(f"sqlite:///{db_path}", evidence_path)


def test_bootstrap_seeds_the_expected_persisted_state(rehearsal):
    database = connect(rehearsal.database_url)
    try:
        season = SeasonRepository(database).get_season(rehearsal.season_id)
        assert season is not None
        assert season.year == 2026

        identities = IdentityRepository(database)
        assert len(rehearsal.entries) == ENTRY_COUNT
        team_names = {identities.get_public_team(entry.season_entry_id).team_name for entry in rehearsal.entries}
        assert len(team_names) == ENTRY_COUNT
        for entry in rehearsal.entries:
            coach = identities.get_current_coach(entry.season_entry_id)
            assert coach is not None and coach.coach_id == entry.coach_id
        assert identities.get_coach(rehearsal.coach_a.coach_id).email == rehearsal.coach_a.email

        lifecycle = CompetitionLifecycleRepository(database)
        round_ = lifecycle.get_round(rehearsal.bbbffl_round_id)
        assert round_ is not None
        assert round_.state == "open"
        assert round_.afl_season_id == rehearsal.afl_season_id
        assert round_.afl_round_id == rehearsal.afl_round_id

        matchups = lifecycle.list_matchups(rehearsal.bbbffl_round_id)
        assert len(matchups) == 5
        participating = {entry_id for m in matchups for entry_id in (m.home_season_entry_id, m.away_season_entry_id)}
        assert participating == {entry.season_entry_id for entry in rehearsal.entries}

        mapping = RoundMappingRepository(database).resolve(rehearsal.bbbffl_round_id)
        assert mapping is not None
        assert mapping.state == "accepted"
        assert mapping.afl_season_id == rehearsal.afl_season_id
        assert mapping.afl_round_id == rehearsal.afl_round_id

        pool = PlayerPoolRepository(database)
        ownership = OwnershipRepository(database)
        for entry in rehearsal.entries:
            squad = ownership.current_squad(entry.season_entry_id)
            assert len(squad) == 9
            for period in squad:
                season_player = pool.get_by_id(period.season_player_id)
                assert season_player is not None and season_player.eligible

        submitted_entries = {
            row["season_entry_id"]
            for row in database.execute(
                "SELECT season_entry_id FROM weekly_lineup "
                "WHERE bbbffl_round_id=? AND effective_submission_version IS NOT NULL",
                (rehearsal.bbbffl_round_id,),
            ).fetchall()
        }
        # Nine of the ten entries already have a reconstructed scorer-proxy
        # Round 1 lineup; Coach A's is deliberately left unsubmitted for the
        # operator to complete through the real browser flow.
        expected_submitted = {entry.season_entry_id for entry in rehearsal.entries} - {
            rehearsal.coach_a.season_entry_id
        }
        assert submitted_entries == expected_submitted
        proxy_sources = {
            row["source_type"]
            for row in database.execute(
                "SELECT DISTINCT s.source_type FROM weekly_lineup_submission s "
                "JOIN weekly_lineup l ON l.lineup_id=s.lineup_id AND l.effective_submission_version=s.version "
                "WHERE l.bbbffl_round_id=?",
                (rehearsal.bbbffl_round_id,),
            ).fetchall()
        }
        assert proxy_sources == {"scorer_proxy"}

        trigger = database.execute(
            "SELECT 1 FROM bbbffl_round_lockout_trigger WHERE bbbffl_round_id=?", (rehearsal.bbbffl_round_id,)
        ).fetchone()
        assert trigger is not None
    finally:
        database.close()


def test_rerunning_bootstrap_refuses_and_leaves_the_database_unchanged(rehearsal):
    with pytest.raises(RehearsalAlreadyBootstrappedError):
        bootstrap_round1_2026(rehearsal.database_url, rehearsal.evidence_path)

    database = connect(rehearsal.database_url)
    try:
        seasons = database.execute("SELECT season_id FROM bbbffl_season").fetchall()
        assert [row["season_id"] for row in seasons] == [rehearsal.season_id]
        entries = database.execute("SELECT season_entry_id FROM season_entry").fetchall()
        assert len(entries) == ENTRY_COUNT
        rounds = database.execute("SELECT bbbffl_round_id FROM bbbffl_round_lifecycle").fetchall()
        assert [row["bbbffl_round_id"] for row in rounds] == [rehearsal.bbbffl_round_id]
    finally:
        database.close()


def test_bootstrapped_evidence_resolves_through_the_replay_boundary_and_never_falls_back_live(rehearsal):
    source = ReplayAflDataSource(rehearsal.evidence_path)
    season = source.get_current_season()
    assert season.season_id == rehearsal.afl_season_id

    matches = source.get_matches(rehearsal.afl_round_id)
    assert [match.match_id for match in matches] == [rehearsal.afl_match_id]
    assert matches[0].status == "UPCOMING"

    # ReplayAflDataSource has no live HTTP transport at all (see
    # app/replay.py's module docstring) -- there is no code path that could
    # silently fall back to a live afl-api call. Missing/incompatible
    # evidence fails closed instead of guessing:
    with pytest.raises(ReplayEvidenceError):
        ReplayAflDataSource(rehearsal.evidence_path + ".does-not-exist")


def test_app_boots_in_replay_mode_against_the_bootstrapped_evidence(rehearsal, monkeypatch):
    """The composition root (`app.main`) picks the same
    `ReplayAflDataSource` boundary this bootstrap's evidence is written
    for -- the running application never falls through to `AflApiClient`
    while `BBBFFL_AFL_MODE=replay` (see app/main.py's lifespan)."""
    from fastapi.testclient import TestClient

    import app.main as main_module

    monkeypatch.setenv("BBBFFL_DATABASE_URL", rehearsal.database_url)
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("BBBFFL_AFL_MODE", "replay")
    monkeypatch.setenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", rehearsal.evidence_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("live AflApiClient constructed while BBBFFL_AFL_MODE=replay")

    monkeypatch.setattr(main_module, "AflApiClient", forbidden)
    with TestClient(main_module.app) as client:
        assert client.app.state.settings.afl_mode == "replay"
        assert isinstance(client.app.state.afl_client, ReplayAflDataSource)
        matches = client.app.state.afl_client.get_matches(rehearsal.afl_round_id)
        assert [match.match_id for match in matches] == [rehearsal.afl_match_id]
        assert client.get("/health").status_code == 200
