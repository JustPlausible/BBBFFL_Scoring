"""Issue #237: `scripts/bootstrap_2026_first_half.py` is 2026 replay-only
tooling and must refuse to touch a production environment, exactly like
its sibling replay/bootstrap scripts -- before connecting to, migrating or
writing any database, in every mode (including `--readiness-only` and
`--provision-operator`)."""

import pytest

import scripts.bootstrap_2026_first_half as cli
from tests.db_helpers import migrated_connection
from tests.test_replay_bootstrap import _files


def _database_url(database):
    return str(database.engine.url)


@pytest.mark.parametrize("environment", ["production", "Production", " PRODUCTION "])
@pytest.mark.parametrize("mode", [[], ["--readiness-only"], ["--provision-operator"]])
def test_refuses_production_before_touching_any_database(monkeypatch, capsys, environment, mode):
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", environment)
    monkeypatch.setenv("BBBFFL_REPLAY_OPERATOR_PASSWORD", "unused-password-123")

    def _fail(*_args, **_kwargs):
        raise AssertionError("must not connect/bootstrap while BBBFFL_ENVIRONMENT=production")

    for name in ("connect", "bootstrap_first_half", "replay_readiness", "provision_replay_operator"):
        monkeypatch.setattr(cli, name, _fail)
    monkeypatch.setattr(
        "sys.argv", ["bootstrap_2026_first_half", "--config", "unused.json", "--database-url", "sqlite:///x.db", *mode]
    )
    assert cli.main() == 1
    assert "BBBFFL_ENVIRONMENT=production" in capsys.readouterr().err


def test_production_refusal_leaves_a_real_database_with_a_valid_config_unchanged(monkeypatch, tmp_path):
    """The same guard against a genuine migrated database and a complete,
    valid 2026 replay configuration: nothing is created at all."""
    database = migrated_connection()
    config_path = _files(tmp_path)
    counts = lambda: {  # noqa: E731
        table: database.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("bbbffl_season", "season_player_pool", "season_draft", "audit_event")
    }
    before = counts()
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "production")
    monkeypatch.setattr(
        "sys.argv",
        ["bootstrap_2026_first_half", "--config", str(config_path), "--database-url", _database_url(database)],
    )
    assert cli.main() == 1
    assert counts() == before


def test_non_production_environments_still_run_the_replay_bootstrap(monkeypatch, tmp_path):
    """The guard is production-only: the historical replay behaviour is
    otherwise intact."""
    database = migrated_connection()
    config_path = _files(tmp_path)
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setattr(
        "sys.argv",
        ["bootstrap_2026_first_half", "--config", str(config_path), "--database-url", _database_url(database)],
    )
    cli.main()
    assert database.execute("SELECT COUNT(*) AS n FROM bbbffl_season WHERE year=2026").fetchone()["n"] == 1
