"""Scorer/admin Season Centre application service (issue #100).

An operator surface *over* the season-model domain roadmap packages 09-10
(issues #19/#20) already established -- season identity/lifecycle
(`app.season.SeasonRepository`) and private-coach/public-season-entry
identity (`app.identity.IdentityRepository`) -- plus read-only readiness
signals already produced by the draft, preseason, player-pool and persisted
ordinary-round-lifecycle repositories later roadmap packages built. This
module never introduces a parallel season/team identity model: every
identity fact returned below is read straight from those repositories, and
every identity write is a direct call into `IdentityRepository`/
`SeasonRepository` -- this module adds validation and a stable read-model
shape, not new storage.

Like `app.round_review`/`app.opening_round`/`app.auth`, this sits directly on
top of the persisted season model and is meant to be imported directly by
its route module (`app.routes.season_centre`), exactly the way those are
(see `tests/test_architecture.py`'s `SEASON_CENTRE` group) -- unlike every
other `SEASON_MODEL` module, which routes may only reach via an already-
constructed instance on `request.app.state`.

This module deliberately does **not** implement draft, fixture-generation,
round-configuration or weekly scoring/selection behaviour -- see those
modules' own routers (`app.routes.draft`, `app.routes.round_review`) -- it
only reads their current status for the readiness summary and links to
their existing pages.
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.audit import ActorContext
from app.identity import UNSET, CoachEmailConflictError
from app.opening_round import OpeningRoundRuleRepository, build_opening_round_readiness


class SeasonCentreError(ValueError):
    """A season-setup edit was rejected: empty/invalid input, or a domain
    uniqueness/foreign-key conflict (duplicate licence, unknown season or
    coach) translated from the underlying `IntegrityError` into the same
    kind of plain, HTTP-400-mapped domain exception every other
    application-service module in this codebase raises (see
    `app.lineups.LineupIntegrityError`/`app.lockouts.LockoutIntegrityError`
    for the established pattern this mirrors)."""


def _require_text(value: str | None, label: str) -> str:
    if value is None or not value.strip():
        raise SeasonCentreError(f"{label} must not be empty")
    return value.strip()


# -- Seasons ------------------------------------------------------------


def list_seasons_overview(seasons) -> list[dict]:
    return [dataclasses.asdict(season) for season in seasons.list_seasons()]


def create_season(seasons, year: int, label: str, *, regular_season_round_count: int = 20) -> dict:
    label = _require_text(label, "season label")
    try:
        season = seasons.create_season(year, label, regular_season_round_count=regular_season_round_count)
    except IntegrityError as exc:
        raise SeasonCentreError(f"a season already exists for year {year}") from exc
    return dataclasses.asdict(season)


# -- Coaches --------------------------------------------------------------


def list_coaches_overview(identities) -> list[dict]:
    return [dataclasses.asdict(coach) for coach in identities.list_coaches()]


def create_coach(identities, display_name: str, *, email=None, phone=None, profile_notes=None) -> dict:
    display_name = _require_text(display_name, "coach display name")
    try:
        coach = identities.create_coach(
            display_name, email=email or None, phone=phone or None, profile_notes=profile_notes or None
        )
    except CoachEmailConflictError as exc:
        raise SeasonCentreError(str(exc)) from exc
    return dataclasses.asdict(coach)


def update_coach(
    identities,
    coach_id: str,
    *,
    display_name: str | None | object = UNSET,
    email: str | None | object = UNSET,
    phone: str | None | object = UNSET,
    profile_notes: str | None | object = UNSET,
    actor: ActorContext,
    reason: str | None = None,
) -> dict:
    """`display_name`/`email`/`phone`/`profile_notes` each default to
    `app.identity.UNSET`, not ``None`` -- a caller that omits a keyword
    leaves that field unchanged, while passing ``email=None`` explicitly
    clears it. See `IdentityRepository.update_coach`'s docstring."""
    if display_name is not UNSET:
        display_name = _require_text(display_name, "coach display name")
    try:
        coach = identities.update_coach(
            coach_id,
            display_name=display_name,
            email=email,
            phone=phone,
            profile_notes=profile_notes,
            actor=actor,
            reason=reason,
        )
    except CoachEmailConflictError as exc:
        raise SeasonCentreError(str(exc)) from exc
    return dataclasses.asdict(coach)


# -- Season entries ---------------------------------------------------------


def create_entry(
    identities,
    season_id: str,
    coach_id: str,
    team_name: str,
    *,
    licence_key: str | None = None,
    actor: ActorContext,
    reason: str | None = None,
) -> dict:
    """Create one season entry with a public team name and current coach.

    `licence_key` is the entry's durable slot identifier within the season
    (see `app.identity`'s module docstring); it is an internal detail an
    operator establishing recognisable replay identities should not need to
    invent, so a caller may omit it and get a generated one -- but may also
    supply an explicit one (e.g. porting a historical licence key), still
    validated for season-scoped uniqueness by the same repository
    constraint every other caller goes through.
    """
    team_name = _require_text(team_name, "public team name")
    key = licence_key.strip() if licence_key else f"entry-{uuid4().hex[:10]}"
    try:
        entry = identities.create_entry(season_id, key, coach_id, team_name, actor=actor, reason=reason)
    except IntegrityError as exc:
        raise SeasonCentreError(
            "could not create the season entry -- check the season and coach exist and the licence is unique"
        ) from exc
    return dataclasses.asdict(entry)


def rename_team(identities, entry_id: str, team_name: str, *, actor: ActorContext, reason: str | None = None) -> dict:
    team_name = _require_text(team_name, "public team name")
    try:
        renamed = identities.rename_team(entry_id, team_name, actor=actor, reason=reason)
    except IntegrityError as exc:
        raise SeasonCentreError("could not rename the team") from exc
    return dataclasses.asdict(renamed)


def transfer_entry(identities, entry_id: str, coach_id: str, *, actor: ActorContext, reason: str | None = None) -> dict:
    try:
        assignment = identities.transfer_entry(entry_id, coach_id, actor=actor, reason=reason)
    except IntegrityError as exc:
        raise SeasonCentreError("could not assign that coach -- check the coach exists") from exc
    return dataclasses.asdict(assignment)


# -- Season Centre read-model -----------------------------------------------


def _opening_round_readiness(database, season_id: str) -> dict | None:
    """`None` -- and therefore hidden entirely, per issue #131's "do not
    expose the operation before it is meaningful" -- exactly while this
    season has never accepted any Opening Round rule. A season that never
    configures Opening Round (issue #69/#126's "explicit season
    configuration" boundary) must show nothing here, not an empty/zero
    readiness block that implies the capability exists.

    Issue #133: readiness is `N/10 teams confirmed`, keyed off explicit
    per-entry Opening Round submission confirmation -- never off how many
    eligible clubs/players an entry happens to own."""
    if not OpeningRoundRuleRepository(database).list_accepted_for_season(season_id):
        return None
    readiness = build_opening_round_readiness(database, season_id)
    return {
        "total_entries": readiness.total_entries,
        "total_confirmed": readiness.total_confirmed,
        "is_ready": readiness.is_ready,
        "entries_awaiting_confirmation": [
            {"season_entry_id": entry.season_entry_id, "team_name": entry.team_name}
            for entry in readiness.entries
            if not entry.is_confirmed
        ],
        "has_integrity_issues": bool(
            readiness.duplicate_nominations or readiness.mismatched_nominations or readiness.conflicting_nominations
        ),
    }


def _readiness(seasons, draft, preseason, player_pool, lifecycle, database, season_id: str, entries) -> dict:
    draft_status = draft.status(season_id)
    window = preseason.get_window(season_id)
    competitions = seasons.list_competitions(season_id)
    competition_ids = {competition.competition_id for competition in competitions}
    ordinary_rounds = [
        round_ for round_ in lifecycle.list_ordinary_rounds() if round_.competition_id in competition_ids
    ]
    return {
        "entries_established": len(entries),
        "distinct_coaches": len({entry.coach_id for entry in entries}),
        "competition_streams_configured": len(competitions),
        "player_pool_loaded": len(player_pool.list_selectable(season_id)),
        "draft": None
        if draft_status is None
        else {
            "is_paused": draft_status.is_paused,
            "is_finalized": draft_status.is_finalized,
            "is_complete": draft_status.is_complete,
            "completed_picks": draft_status.completed_picks,
            "total_picks": draft_status.total_picks,
        },
        "preseason_window": None
        if window is None
        else {"is_open": window.is_open, "opened_at": window.opened_at, "closed_at": window.closed_at},
        "ordinary_rounds_created": len(ordinary_rounds),
        "opening_round": _opening_round_readiness(database, season_id),
    }


def _links(season_id: str, draft_started: bool, ordinary_rounds_created: bool, opening_round_configured: bool) -> dict:
    """Links to available subsequent workflows (issue #100's requirement).
    Only ever points at a page that actually exists and is reachable for
    this season -- an unavailable next step is represented as `None` (the
    template renders that honestly rather than inventing a destination).

    `opening_round` follows issue #131: only shown once at least one
    accepted Opening Round rule exists for the season (the existing
    lifecycle's own clean signal that the operation is meaningful) -- not
    merely once the draft has started, since a season that never
    configures Opening Round must never surface the link at all."""
    return {
        "draft": f"/admin/draft/{season_id}" if draft_started else None,
        "preseason": f"/admin/preseason/{season_id}" if draft_started else None,
        "round_centre": "/scorer/round-centre" if ordinary_rounds_created else None,
        "opening_round": f"/operations/seasons/{season_id}/opening-round" if opening_round_configured else None,
    }


def build_season_centre(
    seasons, identities, draft, preseason, player_pool, lifecycle, season_id: str, database
) -> dict:
    season = seasons.get_season(season_id)
    if season is None:
        raise KeyError(season_id)
    entries = identities.list_entries(season_id)
    competitions = seasons.list_competitions(season_id)
    readiness = _readiness(seasons, draft, preseason, player_pool, lifecycle, database, season_id, entries)
    return {
        "season": dataclasses.asdict(season),
        "competitions": [dataclasses.asdict(competition) for competition in competitions],
        "entries": [dataclasses.asdict(entry) for entry in entries],
        "readiness": readiness,
        "links": _links(
            season_id,
            readiness["draft"] is not None,
            readiness["ordinary_rounds_created"] > 0,
            readiness["opening_round"] is not None,
        ),
    }
