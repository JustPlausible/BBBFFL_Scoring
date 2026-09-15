"""Issue #208: coach lineup resolution/submission for the `finals` and
`superscore` streams. `CoachLineupService.resolve`/`view`/`submit` already
generalised their gates to accept `ordinary`, `finals` and `superscore`
rounds (`app.finals_participation.require_round_participant`/`app.
superscore_participation.require_superscore_entry_eligible`) -- this proves
that generalisation end-to-end: a valid finals participant can open and
submit a Finals Week 1 lineup, a non-participating/eliminated finals team
cannot, and all ten season entries can open and submit a SuperScore 1
lineup with no fabricated opponent."""

from app.audit import ActorContext
from app.coach_lineup import CoachLineupService
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from tests.finals_helpers import accept_week_mapping, build_finals_ready_season
from tests.superscore_helpers import build_superscore_ready_season, open_superscore_round

ACTOR = ActorContext.anonymous_operator("test")


class _StubAflClient:
    """Minimal fake AFL client -- no live network access. A fully-vacant
    lineup (every position `None`, a valid deliberate submission -- see
    `app.routes.coach_lineup`'s vacancy-confirmation convention) needs
    nothing more than "the round has no matches/rounds to resolve" from
    both `app.lockouts`' lock evaluation and `app.lineup_validation`'s
    availability check."""

    def get_matches(self, afl_round_id):
        return []

    def get_rounds(self, afl_season_id):
        return []


def _coach_id(database, season_entry_id):
    row = database.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (season_entry_id,),
    ).fetchone()
    return row["coach_id"]


def _open_finals_week1(year=2401):
    built = build_finals_ready_season(year=year)
    repo = FinalsBracketRepository(built["database"])
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="issue #208 test bracket",
    )["bracket"]
    round_id = repo.get_week_round_id(bracket.bracket_id, 1)
    accept_week_mapping(built["database"], round_id, year=year, afl_round_id=9001)
    open_finals_week(built["database"], bracket.bracket_id, 1, actor=ACTOR)
    built["bracket"] = bracket
    built["week1_round_id"] = round_id
    return built


def test_coach_can_open_a_valid_finals_week1_lineup():
    built = _open_finals_week1()
    database = built["database"]
    participant = built["entries"][1]  # seed 2 -- plays in the Week 1 qualifying final
    coach_id = _coach_id(database, participant.season_entry_id)
    service = CoachLineupService(database, afl_client=_StubAflClient())

    entry = service.resolve(coach_id, built["season"].season_id, built["week1_round_id"])
    assert entry is not None
    assert entry["season_entry_id"] == participant.season_entry_id

    context = service.view(coach_id, built["season"].season_id, built["week1_round_id"])
    assert context is not None
    assert context.round["id"] == built["week1_round_id"]
    assert context.round["label"] == "Finals Week 1"


def test_coach_can_submit_a_valid_finals_week1_lineup():
    built = _open_finals_week1()
    database = built["database"]
    participant = built["entries"][1]
    coach_id = _coach_id(database, participant.season_entry_id)
    service = CoachLineupService(database, afl_client=_StubAflClient())

    entry = service.resolve(coach_id, built["season"].season_id, built["week1_round_id"])
    draft = service.ensure_draft(built["season"].season_id, built["week1_round_id"], entry)
    submitted = service.submit(draft, submission_version=0, coach_id=coach_id)

    assert submitted.submission.version == 1
    stored = service.lineups.get_effective_submission(draft.lineup_id)
    assert stored is not None
    assert stored.version == 1


def test_non_participating_finals_team_cannot_open_or_submit_a_finals_lineup():
    """A team that belongs to the season but did not qualify/was eliminated
    (seed 10 here -- not in any Week 1 slot) must not be able to resolve or
    submit a finals lineup merely because it belongs to the season."""
    built = _open_finals_week1()
    database = built["database"]
    eliminated = built["entries"][9]  # seed 10 -- no Week 1 pairing at all
    coach_id = _coach_id(database, eliminated.season_entry_id)
    service = CoachLineupService(database, afl_client=_StubAflClient())

    assert service.resolve(coach_id, built["season"].season_id, built["week1_round_id"]) is None
    assert service.view(coach_id, built["season"].season_id, built["week1_round_id"]) is None


def _open_superscore1(year=2402):
    built = build_superscore_ready_season(year=year)
    round_id = built["superscore_rounds"][1]
    open_superscore_round(built["database"], round_id)
    built["ss1_round_id"] = round_id
    return built


def test_coach_can_open_a_valid_superscore1_lineup():
    built = _open_superscore1()
    database = built["database"]
    entry_obj = built["entries"][4]
    coach_id = _coach_id(database, entry_obj.season_entry_id)
    service = CoachLineupService(database, afl_client=_StubAflClient())

    context = service.view(coach_id, built["season"].season_id, built["ss1_round_id"])
    assert context is not None
    assert context.round["id"] == built["ss1_round_id"]
    assert context.round["label"] == "SuperScore 1"
    # No fabricated opponent/matchup for SuperScore.
    assert context.opponent is None


def test_coach_can_submit_a_valid_superscore1_lineup():
    built = _open_superscore1()
    database = built["database"]
    entry_obj = built["entries"][4]
    coach_id = _coach_id(database, entry_obj.season_entry_id)
    service = CoachLineupService(database, afl_client=_StubAflClient())

    entry = service.resolve(coach_id, built["season"].season_id, built["ss1_round_id"])
    assert entry is not None
    draft = service.ensure_draft(built["season"].season_id, built["ss1_round_id"], entry)
    submitted = service.submit(draft, submission_version=0, coach_id=coach_id)

    assert submitted.submission.version == 1


def test_all_ten_season_entries_are_accepted_for_superscore():
    built = _open_superscore1()
    database = built["database"]
    service = CoachLineupService(database, afl_client=_StubAflClient())
    for entry_obj in built["entries"]:
        coach_id = _coach_id(database, entry_obj.season_entry_id)
        entry = service.resolve(coach_id, built["season"].season_id, built["ss1_round_id"])
        assert entry is not None, f"season_entry_id {entry_obj.season_entry_id} was rejected for SuperScore"
