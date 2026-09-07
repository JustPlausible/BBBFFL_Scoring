"""Administrator Dashboard domain-level read model (issue #148).

Exercises `app.admin_dashboard` directly, the same way
`tests/test_scorer_dashboard.py` exercises `app.scorer_dashboard`; the
HTTP-level authorization/season-scope surface is covered separately in
`tests/test_admin_dashboard_api.py`.
"""

import pytest

from app.admin_dashboard import (
    CATEGORY_AUTHORITY_SECURITY,
    CATEGORY_BLOCKING_CONFIGURATION,
    CATEGORY_OPERATIONAL_HANDOFF,
    STAGE_DRAFT,
    STAGE_PRESEASON,
    STAGE_ROUND_PREPARATION,
    STAGE_SEASON_COMPLETE,
    STAGE_SETUP,
    STAGE_WEEKLY_OPERATIONS,
    build_admin_dashboard,
    build_season_portfolio,
)
from app.audit import ActorContext, AuditEventRepository
from app.auth import RoleGrantRepository
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.draft import DraftRepository
from app.fixtures import FixtureRepository
from app.identity import IdentityRepository
from app.player_pool import PlayerPoolRepository
from app.preseason import PreseasonRepository
from app.round_review import RoundReviewRepository
from app.scorer_dashboard import build_scorer_dashboard
from app.season import SeasonRepository
from tests.admin_dashboard_helpers import build_governed_season
from tests.db_helpers import migrated_connection


class NoMatchesAflClient:
    """No live AFL evidence at all -- every governance/attention read must
    stay meaningful without a live afl-api dependency."""

    def get_matches(self, round_id):
        return []

    def get_match_player_stats(self, match_id):
        return []

    def get_rounds(self, season_id):
        return []


def _dashboard(g, *, season_id=None, round_id=None):
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
        NoMatchesAflClient(),
        season_id or g.season.season_id,
        round_id=round_id,
    )


def _bare_season(db, year: int, entries: int = 10):
    """A season with entries but no competition/rules stream at all --
    for the "missing competition configured" governance case, which
    `build_governed_season` always sets up (it needs a competition to
    attach its logical round to)."""
    season = SeasonRepository(db).create_season(year, str(year))
    identities = IdentityRepository(db)
    for number in range(entries):
        identities.create_entry(
            season.season_id, f"licence-{number}", identities.create_coach(f"Coach {number}").coach_id, f"Team {number}"
        )
    return season, identities


def _dashboard_for(db, season_id, identities):
    return build_admin_dashboard(
        db,
        SeasonRepository(db),
        identities,
        DraftRepository(db),
        PreseasonRepository(db),
        PlayerPoolRepository(db),
        CompetitionLifecycleRepository(db),
        FixtureRepository(db),
        RoundReviewRepository(db),
        AuditEventRepository(db),
        RoleGrantRepository(db),
        NoMatchesAflClient(),
        season_id,
    )


# -- Fresh installation / no seasons ----------------------------------------


def test_fresh_installation_has_an_empty_portfolio():
    db = migrated_connection()
    portfolio = build_season_portfolio(
        SeasonRepository(db),
        IdentityRepository(db),
        DraftRepository(db),
        PreseasonRepository(db),
        FixtureRepository(db),
        db,
    )
    assert portfolio == []


# -- Setup-only season --------------------------------------------------


def test_setup_only_season_reports_incomplete_entries_and_setup_stage():
    g = build_governed_season(year=9101, entries=4)
    dashboard = _dashboard(g)
    assert next(s for s in dashboard["workflow_map"] if s["stage"] == STAGE_SETUP)["is_current"]
    incomplete = next(i for i in dashboard["attention"] if i["code"] == "identity:incomplete_entries")
    assert incomplete["category"] == CATEGORY_BLOCKING_CONFIGURATION
    assert "4" in incomplete["detail"] and "10" in incomplete["detail"]


def test_over_provisioned_entries_is_also_a_setup_blocker():
    """Codex review, PR #160: an eleventh entry (`IdentityRepository.
    create_entry` places no upper bound on its own) must be treated as a
    setup blocker exactly like an incomplete roster -- an unqualified
    `< BBBFFL_TEAM_COUNT` check silently let an over-provisioned season
    read as "setup complete" while the fixture repository's own hard
    ten-team requirement left it unable to progress."""
    g = build_governed_season(year=9117, entries=11)
    dashboard = _dashboard(g)
    assert next(s for s in dashboard["workflow_map"] if s["stage"] == STAGE_SETUP)["is_current"]
    over = next(i for i in dashboard["attention"] if i["code"] == "identity:incomplete_entries")
    assert over["category"] == CATEGORY_BLOCKING_CONFIGURATION
    assert "11" in over["detail"] and "10" in over["detail"]


def test_setup_only_season_never_confuses_round_definitions_with_lifecycle_rows():
    """The core issue #148 acceptance criterion: a round that exists as a
    logical definition but was never opened must never present as "0
    rounds created"."""
    g = build_governed_season(year=9102)
    dashboard = _dashboard(g)
    assert dashboard["readiness"]["rounds_defined"] == 1
    assert dashboard["readiness"]["rounds_opened"] == 0
    assert dashboard["current_round"]["state"] == "not_created"


# -- Draft state -----------------------------------------------------------


def test_draft_in_progress_is_reported_with_pick_counts():
    g = build_governed_season(year=9103, squad_limit=2)
    dashboard = _dashboard(g)
    assert next(s for s in dashboard["workflow_map"] if s["stage"] == STAGE_DRAFT)["is_current"]
    item = next(i for i in dashboard["attention"] if i["code"] == "draft:incomplete")
    assert "0/20" in item["detail"]


def test_finalized_draft_advances_to_preseason_stage():
    g = build_governed_season(year=9104, finalize_draft=True)
    dashboard = _dashboard(g)
    assert next(s for s in dashboard["workflow_map"] if s["stage"] == STAGE_PRESEASON)["is_current"]
    codes = {item["code"] for item in dashboard["attention"]}
    assert "preseason:window_not_opened" in codes


# -- Preseason window/freeze readiness --------------------------------------


def test_open_preseason_window_is_a_data_evidence_attention_item():
    g = build_governed_season(year=9105, open_preseason=True)
    dashboard = _dashboard(g)
    item = next(i for i in dashboard["attention"] if i["code"] == "preseason:window_open")
    assert item["category"] == "data_evidence_readiness"


def test_closed_preseason_window_without_frozen_fixture_blocks_on_fixture():
    g = build_governed_season(year=9106, close_preseason=True, freeze_fixture=False)
    dashboard = _dashboard(g)
    codes = {item["code"] for item in dashboard["attention"]}
    assert "fixture:not_frozen" in codes


# -- Fixture/Opening Round readiness (round preparation) --------------------


def test_round_preparation_stage_once_preseason_and_fixture_are_settled():
    g = build_governed_season(year=9107, close_preseason=True, freeze_fixture=True, accept_mapping=True)
    dashboard = _dashboard(g)
    stage = next(s for s in dashboard["workflow_map"] if s["stage"] == STAGE_ROUND_PREPARATION)
    assert stage["is_current"]
    assert stage["url"] == f"/admin/round-preflight/{g.logical_round.bbbffl_round_id}"


# -- Active ordinary-season state / weekly operations handoff ---------------


def test_active_round_reaches_weekly_operations_stage_and_scorer_summary_agrees():
    g = build_governed_season(year=9108, close_preseason=True, open_round=True)
    dashboard = _dashboard(g)
    assert next(s for s in dashboard["workflow_map"] if s["stage"] == STAGE_WEEKLY_OPERATIONS)["is_current"]
    assert dashboard["scorer_summary"] is not None
    assert dashboard["scorer_summary"]["round_state"] == "open"
    assert dashboard["scorer_summary"]["round_label"] == dashboard["current_round"]["round_label"]
    handoff_codes = {i["code"] for i in dashboard["attention"] if i["category"] == CATEGORY_OPERATIONAL_HANDOFF}
    assert "scorer:next_action" in handoff_codes


def test_scorer_handoff_links_carry_the_selected_season_and_round():
    """Codex review, PR #160: without the season/round in the query
    string, the Scorer Dashboard page has no way to know which season an
    Administrator meant and silently falls back to the newest season on
    load -- every handoff link (workflow map, attention queue, scorer
    summary, portfolio row) must carry both."""
    g = build_governed_season(year=9116, close_preseason=True, open_round=True)
    dashboard = _dashboard(g)
    expected = f"/scorer?season_id={g.season.season_id}&round_id={g.logical_round.bbbffl_round_id}"
    assert dashboard["scorer_summary"]["scorer_dashboard_url"] == expected
    weekly_stage = next(s for s in dashboard["workflow_map"] if s["stage"] == STAGE_WEEKLY_OPERATIONS)
    assert weekly_stage["url"] == expected
    handoff_items = [i for i in dashboard["attention"] if i["category"] == CATEGORY_OPERATIONAL_HANDOFF]
    assert handoff_items and all(i["url"] == expected for i in handoff_items)

    portfolio = build_season_portfolio(g.seasons, g.identities, g.draft, g.preseason, g.fixtures, g.database)
    row = next(r for r in portfolio if r["season_id"] == g.season.season_id)
    assert row["scorer_dashboard_url"] == expected


def test_administrator_summary_agrees_with_the_underlying_scorer_state():
    """Issue #148's cross-dashboard consistency requirement: the concise
    summary shown here must be extracted from -- never diverge from --
    `app.scorer_dashboard.build_scorer_dashboard`'s own authoritative
    result for the identical season/round."""
    g = build_governed_season(year=9109, close_preseason=True, open_round=True)
    dashboard = _dashboard(g)
    scorer = build_scorer_dashboard(
        g.database,
        g.lifecycle,
        g.identities,
        g.seasons,
        RoundReviewRepository(g.database),
        AuditEventRepository(g.database),
        NoMatchesAflClient(),
        g.season.season_id,
    )
    assert dashboard["scorer_summary"]["round_label"] == scorer["round"]["round_label"]
    assert dashboard["scorer_summary"]["round_state"] == scorer["round"]["state"]
    assert dashboard["scorer_summary"]["next_action"]["code"] == scorer["next_action"]["code"]


# -- Completed/archive season state ------------------------------------


def test_completed_season_reaches_the_season_complete_stage():
    g = build_governed_season(year=9199, close_preseason=True, open_round=True)
    for target in ("active", "completed"):
        g.seasons.transition_lifecycle(g.season.season_id, target, actor=ActorContext.anonymous_operator("admin"))
    dashboard = _dashboard(g)
    assert dashboard["season"]["lifecycle_state"] == "completed"
    assert next(s for s in dashboard["workflow_map"] if s["stage"] == STAGE_SEASON_COMPLETE)["is_current"]


# -- Season portfolio ---------------------------------------------------


def test_multiple_season_portfolio_lists_every_season_with_labels():
    db = migrated_connection()
    older = build_governed_season(db, year=9200, entries=10)
    build_governed_season(db, year=9201, entries=3)
    portfolio = build_season_portfolio(
        older.seasons, older.identities, older.draft, older.preseason, older.fixtures, db
    )
    years = {row["year"] for row in portfolio}
    assert years == {9200, 9201}
    newer_row = next(row for row in portfolio if row["year"] == 9201)
    assert newer_row["team_count"] == 3
    assert newer_row["blockers"]


def test_portfolio_does_not_assume_the_newest_season_is_operationally_active():
    """Issue #148's explicit acceptance criterion. Build an *older* season
    that is fully operational and a *newer* season that is merely in
    setup -- the portfolio must describe each season on its own facts, and
    the dashboard for the older, operational season must not be shadowed
    by the newer one's setup-only state."""
    db = migrated_connection()
    active = build_governed_season(db, year=9300, close_preseason=True, open_round=True)
    build_governed_season(db, year=9301, entries=2)
    portfolio = build_season_portfolio(
        active.seasons, active.identities, active.draft, active.preseason, active.fixtures, db
    )
    active_row = next(row for row in portfolio if row["year"] == 9300)
    newer_row = next(row for row in portfolio if row["year"] == 9301)
    assert active_row["current_round"]["state"] == "open"
    assert active_row["blockers"] == []
    assert newer_row["blockers"]
    dashboard = _dashboard(active, season_id=active.season.season_id)
    assert dashboard["current_round"]["state"] == "open"


# -- Identity completeness / duplicate identity -----------------------------


def test_duplicate_team_name_is_flagged_as_blocking():
    g = build_governed_season(year=9110, entries=10)
    g.identities.rename_team(g.entries[1].season_entry_id, "Team 9110-0")
    dashboard = _dashboard(g)
    dup = next(i for i in dashboard["attention"] if i["code"] == "identity:duplicate_team_name")
    assert dup["category"] == CATEGORY_BLOCKING_CONFIGURATION
    assert g.entries[0].season_entry_id in dup["diagnostics"]["season_entry_ids"]
    assert g.entries[1].season_entry_id in dup["diagnostics"]["season_entry_ids"]


def test_no_duplicate_identity_issues_for_a_clean_roster():
    g = build_governed_season(year=9111, entries=10)
    dashboard = _dashboard(g)
    assert dashboard["identity_issues"] == []


# -- Role-grant readiness ----------------------------------------------


def test_no_administrator_grant_is_an_authority_security_item():
    g = build_governed_season(year=9112)
    dashboard = _dashboard(g)
    item = next(i for i in dashboard["attention"] if i["code"] == "authority:no_standing_administrator")
    assert item["category"] == CATEGORY_AUTHORITY_SECURITY


def test_granted_administrator_clears_the_authority_item():
    g = build_governed_season(year=9113)
    admin_coach = g.identities.create_coach("Standing Admin")
    RoleGrantRepository(g.database).grant(admin_coach.coach_id, "admin", actor=ActorContext.anonymous_operator("admin"))
    dashboard = _dashboard(g)
    codes = {item["code"] for item in dashboard["attention"]}
    assert "authority:no_standing_administrator" not in codes
    assert dashboard["role_overview"]["grants_by_role"]["admin"][0]["display_name"] == "Standing Admin"


def test_audit_summary_excludes_role_grants_scoped_to_a_different_season():
    """Codex review, PR #160: a coach participating in this season may
    separately hold a season-scoped grant for a *different* season --
    that other season's role-grant audit events must never appear in this
    season's audit feed. A global (unscoped) grant for the same coach
    still must appear, proving the fix does not over-filter."""
    db = migrated_connection()
    season_a = build_governed_season(db, year=9118, entries=10)
    season_b = build_governed_season(db, year=9119, entries=10)
    shared_coach_id = season_a.identities.list_entries(season_a.season.season_id)[0].coach_id
    grants = RoleGrantRepository(db)
    grants.grant(
        shared_coach_id, "scorer", season_id=season_b.season.season_id, actor=ActorContext.anonymous_operator("admin")
    )
    global_grant = grants.grant(
        shared_coach_id, "secretary", season_id=None, actor=ActorContext.anonymous_operator("admin")
    )
    dashboard = _dashboard(season_a)
    role_grant_diagnostics = {
        e["diagnostics"]["entity_id"] for e in dashboard["audit"] if e["entity_type"] == "identity.role_grant"
    }
    scoped_to_b = next(
        g for g in grants.list_all_for_coach(shared_coach_id) if g.season_id == season_b.season.season_id
    )
    assert global_grant.grant_id in role_grant_diagnostics
    assert scoped_to_b.grant_id not in role_grant_diagnostics


# -- Rules/competition readiness --------------------------------------------


def test_missing_competition_stream_is_a_blocking_item():
    db = migrated_connection()
    season, identities = _bare_season(db, 9114)
    dashboard = _dashboard_for(db, season.season_id, identities)
    codes = {item["code"] for item in dashboard["attention"]}
    assert "season:no_competition_configured" in codes
    assert dashboard["season"]["rules_label"] is None


# -- Unknown season -------------------------------------------------------


def test_unknown_season_raises_keyerror():
    g = build_governed_season(year=9115)
    with pytest.raises(KeyError):
        _dashboard(g, season_id="does-not-exist")
