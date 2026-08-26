"""Season identity, isolation, lifecycle, rules and AFL-reference invariants."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.audit import AuditEventRepository
from app.db import transaction
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


@pytest.fixture
def repository():
    return SeasonRepository(migrated_connection())


def _season_tree(repo, year):
    season = repo.create_season(
        year, f"{year} {'Replay' if year == 2026 else 'Season'}"
    )
    rules = repo.create_rules_version(
        season.season_id, "canonical", 1, "Canonical scoring"
    )
    ordinary = repo.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "BBBFFL", "ordinary"
    )
    superscore = repo.create_competition(
        season.season_id,
        rules.rules_version_id,
        "superscore",
        "SuperScore",
        "superscore",
    )
    ordinary_round = repo.create_round(ordinary.competition_id, "r1", "Round 1", 1)
    superscore_round = repo.create_round(superscore.competition_id, "ss1", "SS1", 1)
    return season, rules, ordinary, superscore, ordinary_round, superscore_round


def test_2026_replay_and_2027_operational_seasons_are_isolated(repository):
    replay = _season_tree(repository, 2026)
    operational = _season_tree(repository, 2027)
    repository.transition_lifecycle(operational[0].season_id, "active")
    assert replay[0].season_id != operational[0].season_id
    assert {
        c.stream_key for c in repository.list_competitions(replay[0].season_id)
    } == {"ordinary", "superscore"}
    assert {
        c.competition_id for c in repository.list_competitions(replay[0].season_id)
    }.isdisjoint(
        {
            c.competition_id
            for c in repository.list_competitions(operational[0].season_id)
        }
    )
    assert repository.list_rounds(replay[2].competition_id) == [replay[4]]
    assert repository.list_rounds(operational[2].competition_id) == [operational[4]]


def test_lifecycle_is_forward_only_and_audited(repository):
    season = repository.create_season(2027, "2027")
    assert repository.transition_lifecycle(season.season_id, "active").version == 2
    assert repository.transition_lifecycle(season.season_id, "completed").version == 3
    with pytest.raises(ValueError, match="completed -> active"):
        repository.transition_lifecycle(season.season_id, "active")
    events = AuditEventRepository(repository.database).list_events(
        entity_type="season", entity_id=season.season_id
    )
    assert [e.action for e in events] == [
        "season.lifecycle.changed",
        "season.lifecycle.changed",
    ]
    assert events[-1].after_state == {"lifecycle_state": "completed"}


def test_lifecycle_returns_exact_values_written_without_post_commit_read(
    repository, monkeypatch
):
    season = repository.create_season(2027, "2027")
    written_at = "2027-03-01T02:03:04+00:00"
    monkeypatch.setattr("app.season._now", lambda: written_at)
    monkeypatch.setattr(
        repository,
        "get_season",
        lambda _season_id: pytest.fail("transition performed an unlocked read"),
    )
    transitioned = repository.transition_lifecycle(season.season_id, "active")
    assert transitioned.lifecycle_state == "active"
    assert transitioned.version == 2
    assert transitioned.updated_at == written_at
    assert transitioned.created_at == season.created_at


def test_rules_are_immutable_versions_with_stable_references(repository):
    season = repository.create_season(2027, "2027")
    v1 = repository.create_rules_version(
        season.season_id, "canonical", 1, "2027 launch"
    )
    repository.create_competition(
        season.season_id, v1.rules_version_id, "ordinary", "BBBFFL", "ordinary"
    )
    v2 = repository.create_rules_version(
        season.season_id, "canonical", 2, "Clarified rules"
    )
    assert v1.rules_version_id != v2.rules_version_id
    assert (
        repository.list_competitions(season.season_id)[0].rules_version_id
        == v1.rules_version_id
    )
    assert not hasattr(repository, "update_rules_version")
    assert [
        r.version_number for r in repository.list_rules_versions(season.season_id)
    ] == [1, 2]
    other = repository.create_season(2026, "2026 Replay")
    other_rules = repository.create_rules_version(
        other.season_id, "canonical", 1, "Same logical formula, explicit record"
    )
    assert other_rules.rules_version_id != v1.rules_version_id
    with pytest.raises(ValueError, match="belong"):
        repository.create_competition(
            other.season_id, v1.rules_version_id, "ordinary", "BBBFFL", "ordinary"
        )


def test_scoped_uniqueness_rejects_collisions_only_inside_parent(repository):
    first = _season_tree(repository, 2026)
    second = _season_tree(repository, 2027)
    with pytest.raises(IntegrityError):
        repository.create_competition(
            first[0].season_id,
            first[1].rules_version_id,
            "ordinary",
            "Duplicate",
            "ordinary",
        )
    assert first[2].stream_key == second[2].stream_key


def test_stream_type_is_sporting_context_not_execution_context(repository):
    season = repository.create_season(2026, "2026 Replay")
    rules = repository.create_rules_version(
        season.season_id, "canonical", 1, "2026 rules"
    )
    repository.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "BBBFFL", "ordinary"
    )
    with pytest.raises(IntegrityError):
        repository.create_competition(
            season.season_id, rules.rules_version_id, "replay", "Replay", "replay"
        )


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        (
            "INSERT INTO season_rules_version VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("rules", "missing-season", "canonical", 1, "Rules", None, "t", None),
        ),
        (
            "INSERT INTO bbbffl_round VALUES (?, ?, ?, ?, ?, ?)",
            ("round", "missing-competition", "r1", "Round 1", 1, "t"),
        ),
        (
            "INSERT INTO bbbffl_round_afl_reference VALUES (?, ?, ?, ?, ?, ?)",
            ("mapping", "missing-round", "afl-api-v1", 85, 1412, "t"),
        ),
    ],
)
def test_sqlite_rejects_orphan_season_domain_rows(repository, statement, parameters):
    pragma = repository.database.execute("PRAGMA foreign_keys").fetchone()
    assert pragma["foreign_keys"] == 1
    with pytest.raises(IntegrityError):
        with transaction(repository.database) as connection:
            connection.execute(statement, parameters)


def test_sqlite_restricts_deleting_historical_parents(repository):
    season, *_ = _season_tree(repository, 2027)
    with pytest.raises(IntegrityError):
        with transaction(repository.database) as connection:
            connection.execute(
                "DELETE FROM bbbffl_season WHERE season_id=?", (season.season_id,)
            )


def test_database_rejects_competition_rules_from_another_season(repository):
    first = _season_tree(repository, 2026)
    second = _season_tree(repository, 2027)
    with pytest.raises(IntegrityError):
        with transaction(repository.database) as connection:
            connection.execute(
                "INSERT INTO competition_stream VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "invalid-cross-season-stream",
                    first[0].season_id,
                    second[1].rules_version_id,
                    "invalid",
                    "Invalid",
                    "ordinary",
                    "t",
                ),
            )


def test_sqlite_restricts_deleting_round_with_afl_reference(repository):
    tree = _season_tree(repository, 2027)
    bbbffl_round = tree[4]
    repository.map_afl_round(bbbffl_round.bbbffl_round_id, 85, 1412)
    with pytest.raises(IntegrityError):
        with transaction(repository.database) as connection:
            connection.execute(
                "DELETE FROM bbbffl_round WHERE bbbffl_round_id=?",
                (bbbffl_round.bbbffl_round_id,),
            )


def test_bbbffl_rounds_are_independent_of_afl_numbering_and_many_contexts_can_map(
    repository,
):
    tree = _season_tree(repository, 2027)
    # BBBFFL Round 1 and SS1 intentionally map to AFL provider round id 1412;
    # neither BBBFFL sequence/key is inferred from that provider identity.
    repository.map_afl_round(tree[4].bbbffl_round_id, 85, 1412)
    repository.map_afl_round(tree[5].bbbffl_round_id, 85, 1412)
    mapped = repository.rounds_for_afl_reference(85, 1412)
    assert {r.bbbffl_round_id for r in mapped} == {
        tree[4].bbbffl_round_id,
        tree[5].bbbffl_round_id,
    }
    assert {(r.round_key, r.sequence) for r in mapped} == {("r1", 1), ("ss1", 1)}
