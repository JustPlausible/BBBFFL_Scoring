"""Regression coverage for issue #176.

At a replay evidence-package boundary (e.g. the 2026 second-half handover),
a BBBFFL round can be persisted as `final` while its mapped AFL round has
been intentionally left out of the *currently active* replay evidence
package -- that AFL round belongs to an earlier, separately preserved
package. The Administrator and Scorer Dashboards share
`app.scorer_dashboard._build_round_dashboard`/`_build_team_readiness` to
build their "current round" summary, and both used to fetch live AFL match/
lockout evidence for *any* round that was not brand new -- including an
already-published `final` round -- so a finalized historical round outside
the active package made both dashboards fail closed with
`ReplayEvidenceError` merely because it happened to still be the latest
persisted lifecycle round.

These tests reproduce that boundary with a client that raises exactly the
exception a real `app.replay.ReplayAflDataSource` raises for evidence
outside its package (`ReplayEvidenceError`, a `ValueError` the dashboards'
existing `except (AflApiError, MatchResolutionError)` handlers never
caught), and assert -- not merely that the page returns something, but --
that the unwanted lookup for the historical AFL round never happens at all.
"""

from datetime import datetime, timezone

import pytest

from app.admin_dashboard import build_admin_dashboard
from app.afl_client import Match, Team
from app.audit import ActorContext, AuditEventRepository
from app.auth import RoleGrantRepository
from app.lineups import WeeklyLineupRepository
from app.lockouts import LockoutRepository, LockoutTriggerRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.replay import ReplayEvidenceError
from app.round_mapping import RoundMappingRepository
from app.round_review import RoundReviewRepository
from app.scorer_dashboard import build_scorer_dashboard
from tests.admin_dashboard_helpers import KnownRound, build_governed_season

# Mirrors the real 2026 second-half boundary: BBBFFL Round 9 mapped to AFL
# round 1352 (preserved history, outside the active package), and BBBFFL
# Round 10 mapped to AFL round 1353 (the first round the active second-half
# package actually carries evidence for).
PRIOR_PHASE_AFL_ROUND = 1352
CURRENT_PHASE_AFL_ROUND = 1353


class BoundaryReplayAflClient:
    """Records every `get_matches` request and fails exactly like a real
    `app.replay.ReplayAflDataSource` restricted to one evidence package:
    `ReplayEvidenceError` for any AFL round outside `available_round_ids`,
    the concrete matches list for one inside it."""

    def __init__(self, available_round_ids):
        self.available_round_ids = set(available_round_ids)
        self.requested_round_ids: list[int] = []

    def get_matches(self, round_id):
        self.requested_round_ids.append(round_id)
        if round_id not in self.available_round_ids:
            raise ReplayEvidenceError(f"required AFL round evidence is missing: {round_id}")
        return []

    def get_match_player_stats(self, match_id):
        return {}

    def get_rounds(self, season_id):
        return []


def _admin_dashboard(g, client, *, round_id=None):
    return build_admin_dashboard(
        g.database,
        g.seasons,
        g.identities,
        g.draft,
        g.preseason,
        g.player_pool,
        g.lifecycle,
        g.fixtures,
        RoundReviewRepository(g.database),
        AuditEventRepository(g.database),
        RoleGrantRepository(g.database),
        client,
        g.season.season_id,
        round_id=round_id,
    )


def _scorer_dashboard(g, client, *, round_id=None):
    return build_scorer_dashboard(
        g.database,
        g.lifecycle,
        g.identities,
        g.seasons,
        RoundReviewRepository(g.database),
        AuditEventRepository(g.database),
        client,
        g.season.season_id,
        round_id=round_id,
    )


def _finalize_the_open_round(g):
    for target in ("live", "review"):
        g.lifecycle.transition(g.logical_round.bbbffl_round_id, target, actor=ActorContext.anonymous_operator("scorer"))
    matchups = g.lifecycle.list_matchups(g.logical_round.bbbffl_round_id)
    g.lifecycle.publish_results(
        g.logical_round.bbbffl_round_id, {m.matchup_id: (100, 90) for m in matchups}, reason="approved"
    )


def _prior_phase_season(year: int):
    """A season whose one, and only, ordinary round is `final` and mapped
    to `PRIOR_PHASE_AFL_ROUND` -- no next round has been created/opened
    yet, exactly the pre-Round-10 Phase 2 boundary issue #176 describes."""
    g = build_governed_season(year=year, close_preseason=True, open_round=True, afl_round=PRIOR_PHASE_AFL_ROUND)
    _finalize_the_open_round(g)
    return g


def test_admin_dashboard_renders_when_prior_final_round_evidence_is_outside_the_active_package():
    g = _prior_phase_season(year=17601)
    client = BoundaryReplayAflClient(available_round_ids={CURRENT_PHASE_AFL_ROUND})

    dashboard = _admin_dashboard(g, client)

    assert dashboard["current_round"]["state"] == "final"
    assert dashboard["scorer_summary"] is not None
    assert dashboard["scorer_summary"]["round_state"] == "final"
    # The core assertion: building this governance/read-only summary must
    # never even attempt to fetch evidence for the historical AFL round --
    # not merely that some fallback swallowed the resulting error.
    assert PRIOR_PHASE_AFL_ROUND not in client.requested_round_ids


def test_scorer_dashboard_renders_when_prior_final_round_evidence_is_outside_the_active_package():
    """Issue #176 explicitly asks whether the Scorer Dashboard shares the
    same defect -- it does, via the same `_build_round_dashboard` path
    `app.admin_dashboard._scorer_summary` calls; this proves it independently."""
    g = _prior_phase_season(year=17602)
    client = BoundaryReplayAflClient(available_round_ids={CURRENT_PHASE_AFL_ROUND})

    view = _scorer_dashboard(g, client)

    assert view["round"]["state"] == "final"
    assert view["next_action"]["category"] == "advisory"
    assert PRIOR_PHASE_AFL_ROUND not in client.requested_round_ids


def test_explicitly_selecting_the_historical_final_round_still_avoids_its_evidence():
    """An operator (or the Scorer Dashboard's own round selector) explicitly
    viewing the historical final round by id must be exactly as safe as the
    default "current round" selection above -- this is a general rule about
    `final` rounds, not merely about how one happens to get selected."""
    g = _prior_phase_season(year=17603)
    client = BoundaryReplayAflClient(available_round_ids={CURRENT_PHASE_AFL_ROUND})

    view = _scorer_dashboard(g, client, round_id=g.logical_round.bbbffl_round_id)

    assert view["round"]["state"] == "final"
    assert PRIOR_PHASE_AFL_ROUND not in client.requested_round_ids


def test_next_round_becoming_current_uses_normal_operational_evidence_lookup():
    """Once Round 10 is created (mapped to the active package's own AFL
    round 1353), it -- not the finalized Round 9 -- becomes "current", and
    normal operational evidence lookup for 1353 must occur exactly as
    before; the fix must not blanket-suppress evidence lookups, only the
    unneeded one for an already-final round outside the active package."""
    g = _prior_phase_season(year=17604)
    round_two = g.seasons.create_round(g.competition.competition_id, "round-2", "Round 2", 2)
    RoundMappingRepository(g.database).accept(
        round_two.bbbffl_round_id, 17604, CURRENT_PHASE_AFL_ROUND, KnownRound((17604, CURRENT_PHASE_AFL_ROUND))
    )
    g.lifecycle.create_ordinary_round(round_two.bbbffl_round_id)

    client = BoundaryReplayAflClient(available_round_ids={CURRENT_PHASE_AFL_ROUND})
    dashboard = _admin_dashboard(g, client)

    assert dashboard["current_round"]["bbbffl_round_id"] == round_two.bbbffl_round_id
    assert dashboard["current_round"]["state"] == "upcoming"
    assert CURRENT_PHASE_AFL_ROUND in client.requested_round_ids
    assert PRIOR_PHASE_AFL_ROUND not in client.requested_round_ids


def test_missing_genuinely_current_round_evidence_still_fails_closed():
    """Non-goal guard: this fix must never weaken fail-closed behaviour for
    a round that is genuinely current and operationally open -- only a
    `final` round's stale evidence requirement is removed."""
    g = build_governed_season(year=17605, close_preseason=True, open_round=True, afl_round=CURRENT_PHASE_AFL_ROUND)
    client = BoundaryReplayAflClient(available_round_ids=set())

    with pytest.raises(ReplayEvidenceError):
        _admin_dashboard(g, client)

    with pytest.raises(ReplayEvidenceError):
        _scorer_dashboard(g, client, round_id=g.logical_round.bbbffl_round_id)


def test_live_mode_first_half_style_final_round_still_renders_with_evidence_present():
    """First-half/normal live-mode behaviour is unchanged: a `final` round
    whose AFL evidence *is* still available renders identically to before
    -- this fix removes an unnecessary requirement, it does not change what
    is shown when evidence happens to be present."""
    g = _prior_phase_season(year=17606)
    client = BoundaryReplayAflClient(available_round_ids={PRIOR_PHASE_AFL_ROUND, CURRENT_PHASE_AFL_ROUND})

    dashboard = _admin_dashboard(g, client)

    assert dashboard["current_round"]["state"] == "final"
    assert dashboard["scorer_summary"]["round_state"] == "final"
    # No evidence lookup is performed for a `final` round either way now --
    # its presence in the package is simply irrelevant to this read.
    assert PRIOR_PHASE_AFL_ROUND not in client.requested_round_ids


# -- Persisted lockout history must survive, not just "not crash" -----------

_TRIGGER_AFL_MATCH_ID = 555001
_TRIGGER_HOME = Team(9101, "Historical FC")
_TRIGGER_AWAY = Team(9102, "Historical Opp")
_TRIGGER_START = datetime(2026, 5, 1, 19, 0, tzinfo=timezone.utc)


class _HistoricalMatchFacts:
    """A fixed, one-time fact source used only to durably materialize
    lockout evidence *while the round was still live* -- entirely separate
    from `BoundaryReplayAflClient`, which stands in for the replay evidence
    package actually active when the dashboard is built afterwards. This
    mirrors how the real lockout plan was decided live, before the second-
    half evidence-package boundary was ever reached."""

    def __init__(self, matches):
        self._matches = matches

    def matches_for(self, bbbffl_round_id):
        return self._matches


def test_final_round_dashboard_shows_persisted_lockout_history_without_live_evidence():
    """Codex review (PR #177): removing the live-evidence requirement for a
    `final` round must not also blank out its already-decided lockout
    history -- a trigger that durably activated, and a lineup position that
    durably locked, while the round was live must still be shown, read
    straight from persisted evidence, never from a live/replay lookup."""
    g = build_governed_season(year=17608, close_preseason=True, open_round=True, afl_round=PRIOR_PHASE_AFL_ROUND)
    round_id = g.logical_round.bbbffl_round_id
    entry = g.entries[0]

    LockoutTriggerRepository(g.database).create(round_id, "main", "main", 1, [_TRIGGER_AFL_MATCH_ID], reason="test")

    # The preseason window is already closed (`close_preseason=True`), so
    # ownership can no longer be changed directly -- reuse the player the
    # draft already assigned to this entry and only refresh its afl-api
    # facts (never ownership), exactly like a routine afl-api sync would.
    pool = PlayerPoolRepository(g.database)
    ownership = OwnershipRepository(g.database)
    owned = ownership.current_squad(entry.season_entry_id)
    assert owned, "the draft must have assigned this entry at least one player"
    season_player_id = owned[0].season_player_id
    canonical_player_id = g.database.execute(
        "SELECT canonical_player_id FROM season_player_pool WHERE season_player_id=?", (season_player_id,)
    ).fetchone()["canonical_player_id"]
    player = pool.refresh_player(
        g.season.season_id,
        canonical_player_id,
        "Historical Player",
        afl_team_id=_TRIGGER_HOME.team_id,
        afl_team_name=_TRIGGER_HOME.name,
    )
    assert player.season_player_id == season_player_id

    lineups = WeeklyLineupRepository(g.database)
    draft = lineups.save_draft(
        g.season.season_id,
        g.competition.competition_id,
        round_id,
        entry.season_entry_id,
        {"F1": player.season_player_id},
        expected_revision=0,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)

    # Materialize durable lockout evidence exactly as the real live/replay
    # flow would -- via `lock_state`, against a concluded match -- *before*
    # the round is finalized and the active evidence package moves on.
    concluded_match = Match(_TRIGGER_AFL_MATCH_ID, _TRIGGER_HOME, _TRIGGER_AWAY, "CONCLUDED", _TRIGGER_START.isoformat())
    LockoutRepository(g.database).lock_state(
        draft.lineup_id,
        round_id,
        entry.season_entry_id,
        {"F1": player.season_player_id},
        match_facts=_HistoricalMatchFacts([concluded_match]),
    )

    _finalize_the_open_round(g)

    client = BoundaryReplayAflClient(available_round_ids={CURRENT_PHASE_AFL_ROUND})
    view = _scorer_dashboard(g, client)

    assert view["round"]["state"] == "final"
    assert PRIOR_PHASE_AFL_ROUND not in client.requested_round_ids

    trigger_rows = view["lockout"]["triggers"]
    assert len(trigger_rows) == 1
    assert trigger_rows[0]["trigger_key"] == "main"
    assert trigger_rows[0]["activated"] is True

    team_row = next(r for r in view["lineups"] if r["season_entry_id"] == entry.season_entry_id)
    assert team_row["lock_summary"] is not None
    assert team_row["lock_summary"]["locked_main"] + team_row["lock_summary"]["locked_selective"] >= 1
