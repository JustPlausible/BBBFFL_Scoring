"""Issue #211 P1 regression: `build_round_preflight` -- the read model the
shared `/admin/round-preflight/{round_id}` page renders for AFL mapping and
lockout-trigger configuration on *every* stream (there is no finals-specific
equivalent for that configuration, see replay evidence in issue #211) --
must render a finals week's actual persisted bracket pairings/bye, never the
ordinary season's own five-match fixture draw for whichever BBBFFL round
number happens to match the finals week number. It must also never let this
page's own ordinary-only "Open Round" action open a finals (or SuperScore)
round's lifecycle -- that stays each stream's own stream-specific open
action (`app.finals_preflight.open_finals_week`, `app.superscore_round.
open_round`)."""

from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import FinalsBracketRepository
from app.identity import IdentityRepository
from app.round_preflight import build_round_preflight
from tests.finals_helpers import build_finals_ready_season

ACTOR = ActorContext.anonymous_operator("test")


class _StubAflClient:
    def get_matches(self, afl_round_id):
        return []

    def get_rounds(self, afl_season_id):
        return []


def _finals_week1(year=7100):
    built = build_finals_ready_season(year=year)
    database = built["database"]
    repo = FinalsBracketRepository(database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="issue #211 preflight regression bracket",
    )["bracket"]
    week1_round_id = repo.get_week_round_id(bracket.bracket_id, 1)
    built["bracket"] = bracket
    built["week1_round_id"] = week1_round_id
    return built


def _preflight(built, round_id):
    database = built["database"]
    lifecycle = CompetitionLifecycleRepository(database)
    identities = IdentityRepository(database)
    return build_round_preflight(database, lifecycle, identities, _StubAflClient(), round_id)


def test_finals_round_preflight_renders_the_persisted_bracket_not_the_ordinary_fixture():
    built = _finals_week1()
    preflight = _preflight(built, built["week1_round_id"])

    assert preflight["finals_bracket"] is not None
    assert preflight["finals_bracket"]["week_number"] == 1
    assert preflight["finals_bracket"]["week_label"] == "Finals Week 1"
    assert preflight["finals_bracket"]["bye"] is not None
    assert preflight["finals_bracket"]["bye"]["team_name"]
    assert {m["slot"] for m in preflight["finals_bracket"]["matchups"]} == {"qf", "ef"}
    for matchup in preflight["finals_bracket"]["matchups"]:
        assert matchup["home_team_name"] and matchup["away_team_name"]
    # Pairing exists pre-open even though matchup rows aren't materialised
    # until the week actually opens (mirrors the Scorer dashboard's own
    # `matchup_id?'':'Pairing not yet materialised.'` handling).

    # The historical defect: the ordinary season's own five-match fixture
    # draw must never leak into a finals round's preflight, and the
    # ordinary-only "exactly five matchups" blocker must never fire for it.
    assert preflight["fixture_matchups"] == []
    assert not any(b["code"] == "fixture_invalid" for b in preflight["readiness"]["blockers"])


def test_finals_round_preflight_blocks_this_pages_own_ordinary_open_action():
    """Relaxing the ordinary fixture blocker for a finals round must never
    make this page's own "Open Round" action (which always creates an
    *ordinary* lifecycle row) appear safe to use against a finals round."""
    built = _finals_week1(7101)
    preflight = _preflight(built, built["week1_round_id"])

    assert preflight["readiness"]["safe_to_open"] is False
    assert any(b["code"] == "non_ordinary_stream_open_unsupported" for b in preflight["readiness"]["blockers"])


def test_ordinary_round_preflight_is_unaffected_by_the_finals_stream_branch():
    built = _finals_week1(7102)
    ordinary_round_id = (
        built["database"]
        .execute(
            "SELECT bbbffl_round_id FROM bbbffl_round WHERE competition_id=? AND sequence=1",
            (built["ordinary_competition_id"],),
        )
        .fetchone()["bbbffl_round_id"]
    )

    preflight = _preflight(built, ordinary_round_id)
    assert preflight["finals_bracket"] is None
    assert len(preflight["fixture_matchups"]) == 5
    assert not any(b["code"] == "non_ordinary_stream_open_unsupported" for b in preflight["readiness"]["blockers"])
