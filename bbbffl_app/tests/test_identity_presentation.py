"""Issue #151: shared server-side human-readable identity projection
helpers -- `app.identity.team_display_label`, `app.player_pool.PlayerLabel`/
`PlayerPoolRepository.labels_by_id`, and `app.season.RulesVersion.
display_label`/`SeasonRepository.get_rules_version`. Each Scorer/Admin
surface these back (round-review blockers/evidence, lineup-adjudication
previews, carry-forward/correction errors) is exercised in its own test
module; this module covers the shared helpers directly, including their
deterministic missing/deleted-identity fallback behaviour.
"""

import pytest

from app.identity import IdentityRepository, team_display_label
from app.player_pool import PlayerPoolRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


@pytest.fixture
def repos():
    db = migrated_connection()
    return IdentityRepository(db), SeasonRepository(db), PlayerPoolRepository(db)


def test_team_display_label_prefers_the_current_public_team_name(repos):
    identities, seasons, _pool = repos
    coach = identities.create_coach("Barry")
    season = seasons.create_season(2027, "2027")
    entry = identities.create_entry(season.season_id, "licence-01", coach.coach_id, "Fitzroy Phoenix")
    assert team_display_label(identities, entry.season_entry_id) == "Fitzroy Phoenix"


def test_team_display_label_falls_back_deterministically_for_an_unknown_entry(repos):
    """A season_entry_id with no matching row (deleted/never existed) must
    degrade to an explicit, diagnostic-safe placeholder -- never a blank
    string, never the bare UUID standing in as though it were a name."""
    identities, _seasons, _pool = repos
    label = team_display_label(identities, "no-such-entry")
    assert label == "Unknown team (no-such-entry)"
    assert "no-such-entry" in label  # id retained only as a diagnostic


def test_team_display_label_falls_back_when_no_identities_repository_is_available():
    """A caller with no identities repository at all (identities=None, the
    same optional-and-display-only convention `app.round_review` already
    uses) still gets the same deterministic, id-qualified fallback as an
    unresolvable entry -- never a blank/silently-wrong label."""
    assert team_display_label(None, "any-entry") == "Unknown team (any-entry)"


def test_team_display_label_handles_a_none_entry_id(repos):
    identities, _seasons, _pool = repos
    assert team_display_label(identities, None) == "Unknown team"


def test_player_labels_by_id_resolves_display_name_and_afl_club(repos):
    _identities, seasons, pool = repos
    season = seasons.create_season(2027, "2027")
    player = pool.refresh_player(season.season_id, 501, "Sam Example", afl_team_name="Fitzroy")
    labels = pool.labels_by_id([player.season_player_id])
    label = labels[player.season_player_id]
    assert label.display_name == "Sam Example"
    assert label.afl_club == "Fitzroy"
    assert label.label == "Sam Example"
    assert label.label_with_club == "Sam Example (Fitzroy)"


def test_player_labels_by_id_omits_unresolvable_ids_and_label_falls_back(repos):
    """A season_player_id with no matching pool row (a historical reference
    to a since-removed entry) is simply absent from the bulk result --
    callers must use `PlayerLabel.label`'s fallback rather than assuming
    presence; there is no silent, misleading default player name."""
    _identities, _seasons, pool = repos
    assert pool.labels_by_id(["missing-player-id"]) == {}
    assert pool.labels_by_id([None, ""]) == {}


def test_rules_version_display_label_combines_name_and_version(repos):
    _identities, seasons, _pool = repos
    season = seasons.create_season(2027, "2027")
    rules = seasons.create_rules_version(season.season_id, "ordinary", 3, "2027 Ordinary Rules")
    assert rules.display_label == "2027 Ordinary Rules (v3)"


def test_get_rules_version_resolves_by_id_or_returns_none(repos):
    _identities, seasons, _pool = repos
    season = seasons.create_season(2027, "2027")
    rules = seasons.create_rules_version(season.season_id, "ordinary", 1, "Rules")
    resolved = seasons.get_rules_version(rules.rules_version_id)
    assert resolved is not None
    assert resolved.display_label == "Rules (v1)"
    assert seasons.get_rules_version("no-such-rules-version") is None
