"""A coach can have both a Grand Final entry and a SuperScore entry in the
same round, sometimes selecting the same AFL player in both. These tests
prove scorer state (DNP, interchange, overrides, finalisation) never leaks
between the two competition instances -- even in the adversarial case where
both happen to use the identical team_key -- because every row in
app/db.py is scoped by competition_key.
"""


from tests.db_helpers import migrated_connection

from app.db import GRAND_FINAL_COMPETITION_KEY, DecisionsRepository


def _repos():
    conn = migrated_connection()
    grand_final = DecisionsRepository(conn, GRAND_FINAL_COMPETITION_KEY)
    superscore = DecisionsRepository(conn, competition_key="superscore:2026:20")
    return grand_final, superscore


def test_default_grand_final_repository_uses_the_grand_final_key():
    conn = migrated_connection()
    repo = DecisionsRepository(conn)
    assert repo.competition_key == GRAND_FINAL_COMPETITION_KEY


def test_dnp_set_on_superscore_does_not_affect_grand_final_for_the_same_team_key():
    grand_final, superscore = _repos()

    # Deliberately the *same* team_key in both competitions -- the
    # adversarial case a naming convention alone wouldn't protect against.
    superscore.set_dnp("coach_x", "Forward1", True)

    assert superscore.get_dnp_map()[("coach_x", "Forward1")] is True
    assert ("coach_x", "Forward1") not in grand_final.get_dnp_map()


def test_dnp_set_on_grand_final_does_not_affect_superscore_for_the_same_team_key():
    grand_final, superscore = _repos()

    grand_final.set_dnp("coach_x", "Midfield1", True)

    assert grand_final.get_dnp_map()[("coach_x", "Midfield1")] is True
    assert ("coach_x", "Midfield1") not in superscore.get_dnp_map()


def test_interchange_assignment_is_isolated_per_competition():
    grand_final, superscore = _repos()

    grand_final.set_interchange_assignment("coach_x", "Ruck")
    superscore.set_interchange_assignment("coach_x", "Tackler")

    assert grand_final.get_interchange_assignments()["coach_x"].target_position == "Ruck"
    assert superscore.get_interchange_assignments()["coach_x"].target_position == "Tackler"


def test_override_is_isolated_per_competition():
    grand_final, superscore = _repos()

    grand_final.set_override("coach_x", "Ruck", 10.0, "GF correction")
    superscore.set_override("coach_x", "Ruck", 99.0, "SuperScore correction")

    gf_override = grand_final.get_overrides()[("coach_x", "Ruck")]
    ss_override = superscore.get_overrides()[("coach_x", "Ruck")]
    assert gf_override.override_score == 10.0
    assert ss_override.override_score == 99.0


def test_finalizing_superscore_does_not_finalize_grand_final():
    grand_final, superscore = _repos()

    superscore.finalize("SuperScore round confirmed")

    assert superscore.get_matchup_state().finalized is True
    assert grand_final.get_matchup_state().finalized is False


def test_finalizing_grand_final_does_not_finalize_superscore():
    grand_final, superscore = _repos()

    grand_final.finalize("Grand Final confirmed")

    assert grand_final.get_matchup_state().finalized is True
    assert superscore.get_matchup_state().finalized is False


def test_two_superscore_rounds_are_independently_addressable():
    """Not required for this trial, but proves the competition_key scheme
    (keyed by season+round in superscore.py) keeps each round's SuperScore
    decisions/result distinct -- the basis for retaining SuperScore history
    across the season's four rounds without a separate results table."""
    conn = migrated_connection()
    round_20 = DecisionsRepository(conn, competition_key="superscore:2026:20")
    round_21 = DecisionsRepository(conn, competition_key="superscore:2026:21")

    round_20.finalize("Round 20 confirmed")

    assert round_20.get_matchup_state().finalized is True
    assert round_21.get_matchup_state().finalized is False
