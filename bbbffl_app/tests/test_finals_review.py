"""Focused finals publication/correction provenance tests (issue #191)."""

from contextlib import contextmanager
from types import SimpleNamespace

from app.audit import ActorContext
from app.db import transaction
from app.finals import FinalsBracketRepository, _StaleMatchupDuringDownstreamCheck
from app.finals_review import _mathematical_wooden_spoon, correct_finals_result
from tests.finals_helpers import build_finals_ready_season, seed_finals_seeding_snapshot_row


def test_ladder_seeded_bracket_resolves_wooden_spoon_from_frozen_order_and_references():
    built = build_finals_ready_season(year=2710)
    repo = FinalsBracketRepository(built["database"])
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ActorContext.anonymous_operator("test"),
        reason="ladder wooden spoon provenance",
    )["bracket"]
    expected = (
        built["database"]
        .execute(
            "SELECT season_entry_id FROM finals_bracket_seed WHERE bracket_id=? AND seed_position=10",
            (bracket.bracket_id,),
        )
        .fetchone()["season_entry_id"]
    )
    with transaction(built["database"]) as conn:
        entry_id, provenance = _mathematical_wooden_spoon(conn, bracket.bracket_id)
    assert entry_id == expected
    assert provenance["seed_source"] == "ladder"
    assert provenance["through_round"] == 20
    assert len(provenance["result_references"]) == 100


def test_snapshot_seeded_bracket_uses_mathematical_rank_not_historical_seed_ten():
    built = build_finals_ready_season(year=2711)
    mathematical_tenth = built["entries"][9].season_entry_id
    historical_order = [entry.season_entry_id for entry in built["entries"]]
    historical_order[8], historical_order[9] = historical_order[9], historical_order[8]
    seed_finals_seeding_snapshot_row(
        built["database"], built["season"].season_id, built["ordinary_competition_id"], historical_order
    )
    snapshot = (
        built["database"]
        .execute("SELECT snapshot_id FROM finals_seeding_snapshot WHERE season_id=?", (built["season"].season_id,))
        .fetchone()
    )
    with transaction(built["database"]) as conn:
        conn.execute(
            "INSERT INTO finals_seeding_snapshot_mathematical_row VALUES "
            "('math-ten', ?, ?, 10, 0, 20, 0, 0, 20, '0', '1', '0', 0)",
            (snapshot["snapshot_id"], mathematical_tenth),
        )
    repo = FinalsBracketRepository(built["database"])
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ActorContext.anonymous_operator("test"),
        reason="snapshot wooden spoon provenance",
    )["bracket"]
    with transaction(built["database"]) as conn:
        entry_id, provenance = _mathematical_wooden_spoon(conn, bracket.bracket_id)
    historical_tenth = historical_order[9]
    assert mathematical_tenth != historical_tenth
    assert entry_id == mathematical_tenth
    assert provenance == {
        "seed_source": "snapshot",
        "finals_seeding_snapshot_id": snapshot["snapshot_id"],
        "through_round": 20,
    }


def test_correction_retries_the_whole_transaction_after_stale_materialisation(monkeypatch):
    """The private stale-materialisation signal never escapes as a 500."""
    import app.finals_review as module

    context = {
        "matchup_id": "finals-match",
        "bbbffl_round_id": "finals-round",
        "effective_official_version": 1,
    }
    review = SimpleNamespace(blockers=[], matchup_id="finals-match")
    attempts = []

    monkeypatch.setattr(module, "_finals_matchup_context", lambda *_: context)
    monkeypatch.setattr(module.MatchupCalculationService, "calculate_round", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "build_matchup_review", lambda *_args, **_kwargs: review)
    monkeypatch.setattr(module, "_freeze_matchup_inputs", lambda *_: {"calculation_revision": 1})

    def attempt(*_args):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise _StaleMatchupDuringDownstreamCheck

    monkeypatch.setattr(module, "_correct_finals_result_transaction", attempt)

    @contextmanager
    def evidence_batch():
        yield SimpleNamespace(is_evidence_fresh=lambda: True)

    afl_client = SimpleNamespace(evidence_batch=evidence_batch)
    lifecycle = SimpleNamespace(
        get_matchup=lambda _mid: object(),
        effective_result=lambda mid: SimpleNamespace(matchup_id=mid, version=2),
    )
    result = correct_finals_result(
        object(),
        afl_client,
        lifecycle,
        object(),
        object(),
        "finals-match",
        actor=ActorContext.anonymous_operator("test"),
        reason="materialisation retry",
    )
    assert attempts == [1, 2]
    assert result.version == 2
