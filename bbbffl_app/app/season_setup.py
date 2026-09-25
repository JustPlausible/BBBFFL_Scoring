"""Production-safe fresh-season and phase initialization (issue #237).

`docs/2027-live-season-readiness.md`'s remaining item 1: before this module,
a genuinely new live season could only be populated, drafted, given an
Opening Round rule set, or carried into Finals/SuperScore by running a 2026
replay/bootstrap script against the database. This module is the supported
Scorer/Secretary/Administrator path for each of those boundaries, exposed
through one browser surface (`app.routes.season_setup`,
`/admin/season-setup/{season_id}`) that shows current state, completed
setup, prerequisites, the next safe action and explicit blocked reasons.

It is an orchestration layer only. Every write goes through the domain
boundary that already owns it, never a parallel implementation of the
replay bootstrap:

- player pool -- `app.afl_client.AflApiClient.get_season_players` (live
  `afl-api`, never replay evidence) into `app.player_pool.
  PlayerPoolRepository.refresh_season_pool`;
- ordinary structure -- `app.season.SeasonRepository.
  initialize_ordinary_competition`;
- preseason draft -- `app.player_pool.OwnershipRepository.
  configure_squad_limit` and `app.draft.DraftRepository.accept_order` (the
  existing snake-draft engine the coach-facing draft pages already run on);
- Opening Round -- `app.opening_round.OpeningRoundRuleRepository.
  accept_locked`, with the rule set derived from the live AFL fixture
  (Opening Round participants and each club's first later bye) rather than
  2026-shaped constants;
- Finals -- `app.finals.FinalsBracketRepository.preview_ladder_seed`/
  `ensure_finals_stream`/`create_bracket` (live mathematical ladder only;
  the 2026 historical seeding snapshot is refused, never used);
- SuperScore -- `app.superscore_round.initialize_structure`.

## Safety properties

- Every command re-validates its prerequisites server-side from persisted
  state; the page's own "available/blocked" presentation is advisory.
- Every command is idempotent (an exactly-matching existing structure is a
  no-op reporting `created=False`) or fails closed with a
  `SeasonSetupError` naming the conflicting state -- none of them "repair"
  structure they did not create.
- Multi-write transitions are single transactions in their owning
  repository, serialized on the season row (`SeasonRepository.
  guard_writable`), so a failed or concurrent request never leaves half a
  structure behind. The one deliberate exception is Finals: the finals
  stream is created (idempotently) only after `preview_ladder_seed` says the
  ladder can seed a bracket, and `create_bracket` then re-verifies every
  prerequisite under its own locks. If that final re-verification refuses
  (e.g. a result corrected in between), the empty finals stream remains and
  is reported as "stream created, bracket pending"; a retry reuses it.
- Later phases are never created early: Finals needs every regular-season
  round final and an untied ladder; SuperScore needs the Finals bracket.
- Completed seasons are read-only here too.
- Every mutation is attributed to the acting Scorer/Secretary/Admin
  (`ActorContext`) with an explicit reason.

Nothing here is used by, or uses, replay tooling: replay mode's AFL data
source has no season-player listing, so pool population refuses in replay
mode with an explicit message rather than reading replay evidence.
"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager, nullcontext

from sqlalchemy.exc import IntegrityError

from app.afl_client import AflApiError
from app.audit import ActorContext
from app.db import _for_update_suffix, transaction
from app.draft import DraftRepository
from app.finals import FinalsBracketRepository
from app.fixtures import FixtureRepository
from app.identity import IdentityRepository
from app.opening_round import OpeningRoundRuleRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.season import SeasonRepository
from app.superscore_round import ROUND_LABELS, get_stream, initialize_structure

TEAM_COUNT = 10
LIVE_SOURCE_PROVIDER = "afl-api-v1"
OPENING_ROUND_NUMBER = 0


class SeasonSetupError(ValueError):
    """A setup command was refused: a prerequisite is missing, the persisted
    state conflicts with the request, or the AFL evidence behind it is not
    fresh/consistent. Nothing was written. Mapped to HTTP 409."""


class SeasonSetupAflError(SeasonSetupError):
    """The live AFL evidence a setup command depends on could not be read
    fresh (an `afl-api` failure, a stale-cache fallback, or a replay-mode
    data source without the live endpoint). Mapped to HTTP 502 when caused
    by afl-api itself; otherwise the generic 409."""


def live_source_provider(afl_season_id: int) -> str:
    """The `season_player_pool.source_provider` recorded for a live pool
    populated from one AFL season -- the provenance that stops a season's
    pool being silently mixed with another AFL season's players (see
    `PlayerPoolRepository.refresh_season_pool`)."""
    return f"{LIVE_SOURCE_PROVIDER}/season-{afl_season_id}"


def _reason(reason: str | None) -> str:
    if reason is None or not reason.strip():
        raise SeasonSetupError("an explicit reason is required for every season setup action")
    return reason.strip()


def _season(database, season_id: str):
    season = SeasonRepository(database).get_season(season_id)
    if season is None:
        raise KeyError(season_id)
    return season


def _writable_season(database, season_id: str):
    season = _season(database, season_id)
    if season.lifecycle_state == "completed":
        raise SeasonSetupError(f"the {season.year} season is completed; its setup is historical and read-only")
    return season


def _ordinary_competition(database, season_id: str):
    streams = [c for c in SeasonRepository(database).list_competitions(season_id) if c.stream_type == "ordinary"]
    if len(streams) > 1:
        raise SeasonSetupError(f"this season has {len(streams)} ordinary competition streams; exactly one is supported")
    return streams[0] if streams else None


def _ordinary_rounds(database, competition_id: str) -> dict[int, str]:
    return {r.sequence: r.bbbffl_round_id for r in SeasonRepository(database).list_rounds(competition_id)}


def _ordinary_structure(database, season) -> dict:
    """Read-only view of `SeasonRepository.initialize_ordinary_competition`'s
    target shape, for the setup page and for prerequisite checks."""
    competition = _ordinary_competition(database, season.season_id)
    if competition is None:
        return {"competition": None, "complete": False, "rounds": [], "diagnostic": None}
    rounds = SeasonRepository(database).list_rounds(competition.competition_id)
    expected = [(n, f"round-{n}", f"Round {n}") for n in range(1, season.regular_season_round_count + 1)]
    actual = [(r.sequence, r.round_key, r.label) for r in rounds]
    complete = actual == expected
    return {
        "competition": competition,
        "complete": complete,
        "rounds": rounds,
        "diagnostic": None
        if complete
        else (
            f"the ordinary competition has {len(rounds)} round(s), not exactly Rounds 1-"
            f"{season.regular_season_round_count}"
        ),
    }


def _require_ordinary(database, season):
    structure = _ordinary_structure(database, season)
    if structure["competition"] is None:
        raise SeasonSetupError("initialize the ordinary competition (rules version, stream and rounds) first")
    if not structure["complete"]:
        raise SeasonSetupError(structure["diagnostic"])
    return structure["competition"]


@contextmanager
def _fresh_scope(afl_client):
    """Scope every afl-api read of one setup command into a single
    `evidence_batch` (when the client offers one) and translate an afl-api
    failure into a setup-specific, operator-readable refusal -- never the
    generic scoring-outage 502 message."""
    batch = getattr(afl_client, "evidence_batch", None)
    try:
        with batch() if callable(batch) else nullcontext(None) as evidence:
            yield evidence
    except AflApiError as exc:
        raise SeasonSetupAflError(f"afl-api could not supply the live evidence this step needs: {exc}") from exc


def _require_fresh(evidence) -> None:
    fresh = getattr(evidence, "is_evidence_fresh", None)
    if callable(fresh) and not fresh():
        raise SeasonSetupAflError(
            "afl-api evidence was served from a stale cache; retry once afl-api is reachable so setup is based on "
            "fresh provider data"
        )


def _afl_season(afl_client, afl_season_id: int, season):
    """Resolve and verify the chosen AFL season against the BBBFFL season's
    own year -- the "am I operating on the intended season" check every
    live-evidence setup command makes before reading anything else."""
    matches = [item for item in afl_client.get_seasons() if item.season_id == afl_season_id]
    if not matches:
        raise SeasonSetupError(f"afl-api does not publish an AFL season with id {afl_season_id}")
    afl_season = matches[0]
    if afl_season.year != season.year:
        raise SeasonSetupError(
            f"AFL season {afl_season_id} is the {afl_season.year} season, but this BBBFFL season is {season.year}; "
            "refusing cross-season setup"
        )
    return afl_season


# -- AFL season selection ----------------------------------------------------


def list_afl_seasons(database, afl_client, season_id: str) -> list[dict]:
    """Human-readable AFL season choices for the setup page, flagging which
    one matches this BBBFFL season's year. `season_id` is afl-api's opaque
    identifier and never needs to be typed by the operator."""
    season = _season(database, season_id)
    with _fresh_scope(afl_client):
        afl_seasons = afl_client.get_seasons()
    return [
        {
            "afl_season_id": item.season_id,
            "year": item.year,
            "name": item.name,
            "is_current": item.is_current,
            "matches_season_year": item.year == season.year,
        }
        for item in afl_seasons
    ]


# -- Player pool ---------------------------------------------------------------


def refresh_player_pool(database, afl_client, season_id: str, afl_season_id: int, *, actor: ActorContext, reason):
    """Populate or refresh this season's player pool from the live afl-api
    season-player collection. Safe to repeat: existing players' cached
    display/club facts are updated in place, new players are added
    eligible, ownership is never touched, and nothing is deleted."""
    reason = _reason(reason)
    season = _writable_season(database, season_id)
    if not callable(getattr(afl_client, "get_season_players", None)):
        raise SeasonSetupAflError(
            "the configured AFL data source cannot list a season's players (replay mode?); live player-pool "
            "population requires BBBFFL_AFL_MODE=live"
        )
    with _fresh_scope(afl_client) as evidence:
        _afl_season(afl_client, afl_season_id, season)
        players = afl_client.get_season_players(afl_season_id)
        _require_fresh(evidence)
    try:
        summary = PlayerPoolRepository(database).refresh_season_pool(
            season_id,
            [(p.canonical_player_id, p.display_name, p.team.team_id, p.team.name) for p in players],
            source_provider=live_source_provider(afl_season_id),
            actor=actor,
            reason=reason,
        )
    except ValueError as exc:
        raise SeasonSetupError(str(exc)) from exc
    return {**summary, "afl_season_id": afl_season_id}


# -- Ordinary competition ------------------------------------------------------


def initialize_ordinary_competition(database, season_id: str, *, actor: ActorContext, reason):
    reason = _reason(reason)
    _writable_season(database, season_id)
    try:
        result = SeasonRepository(database).initialize_ordinary_competition(season_id, actor=actor, reason=reason)
    except ValueError as exc:
        raise SeasonSetupError(str(exc)) from exc
    return {
        "created": result["created"],
        "competition_id": result["competition"].competition_id,
        "competition_label": result["competition"].label,
        "rules_version": result["rules_version"].display_label if result["rules_version"] else None,
        "round_count": result["round_count"],
    }


# -- Opening Round ---------------------------------------------------------------


class _KnownAflRounds:
    """`app.round_mapping.AflReferenceValidator` over AFL rounds already
    fetched fresh for this request, so rule acceptance never makes a
    network call while holding the setup transaction's locks."""

    def __init__(self, afl_season_id: int, round_ids):
        self._pairs = {(afl_season_id, round_id) for round_id in round_ids}

    def round_exists(self, afl_season_id: int, afl_round_id: int) -> bool:
        return (afl_season_id, afl_round_id) in self._pairs


def _completed_preseason_picks(conn, season_id: str) -> int:
    """Every preseason pick ever completed, including one since superseded
    by a correction -- the same "has drafting begun at all" signal the 2026
    replay bootstrap uses for its before-Pick-1 Opening Round rule."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM draft_pick p JOIN season_draft d ON d.draft_id=p.draft_id "
        "WHERE d.season_id=? AND d.draft_kind='preseason' AND p.completed_at IS NOT NULL",
        (season_id,),
    ).fetchone()["n"]


def preview_opening_round(database, afl_client, season_id: str, afl_season_id: int) -> dict:
    """Derive the Opening Round compensating-bye rule set from the live AFL
    fixture, read-only. An AFL season with no round numbered 0 simply has no
    Opening Round: `applicable=False`, and the season proceeds without any
    rule. Otherwise every club that plays in the Opening Round gets one
    proposed rule: its compensating bye is the first later AFL round whose
    published `byes` include it (fail-closed as `unresolved` if an earlier
    round's bye list is unpublished, or no bye is found), and the
    recommended BBBFFL target is the ordinary round with the same number --
    advisory only; the operator confirms or changes each target."""
    season = _season(database, season_id)
    competition = _require_ordinary(database, season)
    rounds_by_number = _ordinary_rounds(database, competition.competition_id)
    rule_repo = OpeningRoundRuleRepository(database)
    accepted = {rule.afl_club_id: rule for rule in rule_repo.list_accepted_for_season(season_id)}
    completed_picks = _completed_preseason_picks(database, season_id)
    with _fresh_scope(afl_client) as evidence:
        afl_season = _afl_season(afl_client, afl_season_id, season)
        afl_rounds = sorted(afl_client.get_rounds(afl_season_id), key=lambda r: r.round_number)
        opening = [r for r in afl_rounds if r.round_number == OPENING_ROUND_NUMBER]
        matches = afl_client.get_matches(opening[0].round_id) if len(opening) == 1 else []
        _require_fresh(evidence)
    report = {
        "afl_season_id": afl_season_id,
        "afl_season_name": afl_season.name,
        "applicable": bool(opening),
        "opening_round_id": opening[0].round_id if len(opening) == 1 else None,
        "rules": [],
        "ready": False,
        "diagnostic": None,
        "accepted_rule_count": len(accepted),
        "draft_started": completed_picks > 0,
        "bbbffl_round_numbers": sorted(rounds_by_number),
        "afl_round_ids": [r.round_id for r in afl_rounds],
    }
    if not opening:
        report["diagnostic"] = (
            "This AFL season has no Opening Round (no round 0), so no compensating-bye rules are needed; the "
            "season proceeds without them."
        )
        return report
    if len(opening) > 1:
        report["diagnostic"] = "afl-api lists more than one round 0 for this season; refusing to guess"
        return report
    clubs: dict[int, str] = {}
    for match in matches:
        for team in (match.home_team, match.away_team):
            clubs[team.team_id] = team.name
    if not clubs:
        report["diagnostic"] = "the Opening Round has no published matches yet; its participating clubs are unknown"
        return report
    later = [r for r in afl_rounds if r.round_number > OPENING_ROUND_NUMBER]
    for club_id in sorted(clubs):
        bye_round, unresolved = None, None
        for afl_round in later:
            if afl_round.byes is None:
                unresolved = f"AFL round {afl_round.round_number}'s bye list is not published yet"
                break
            if any(team.team_id == club_id for team in afl_round.byes):
                bye_round = afl_round
                break
        if bye_round is None and unresolved is None:
            unresolved = "no later AFL round lists this club on a bye"
        existing = accepted.get(club_id)
        recommended = bye_round.round_number if bye_round and bye_round.round_number in rounds_by_number else None
        existing_number = None
        if existing is not None:
            existing_number = next((n for n, rid in rounds_by_number.items() if rid == existing.bbbffl_round_id), None)
        report["rules"].append(
            {
                "afl_club_id": club_id,
                "afl_club_name": clubs[club_id],
                "afl_bye_round_id": bye_round.round_id if bye_round else None,
                "afl_bye_round_number": bye_round.round_number if bye_round else None,
                "recommended_bbbffl_round_number": recommended,
                "unresolved": unresolved,
                "accepted": existing is not None,
                "accepted_bbbffl_round_number": existing_number,
                "accepted_matches_fixture": existing is not None
                and existing.afl_season_id == afl_season_id
                and existing.afl_opening_round_id == opening[0].round_id
                and bye_round is not None
                and existing.afl_bye_round_id == bye_round.round_id,
            }
        )
    unexpected = sorted(set(accepted) - set(clubs))
    if unexpected:
        report["diagnostic"] = (
            f"accepted Opening Round rule(s) exist for club(s) {unexpected} that do not play in this Opening Round"
        )
    elif any(rule["unresolved"] for rule in report["rules"]):
        report["diagnostic"] = "one or more clubs' compensating bye cannot be derived from the live fixture yet"
    else:
        report["ready"] = True
    return report


def accept_opening_round_rules(
    database,
    afl_client,
    season_id: str,
    afl_season_id: int,
    targets: dict[int, int],
    *,
    actor: ActorContext,
    reason,
) -> dict:
    """Accept the complete Opening Round rule set -- one rule per
    participating club, each targeting the operator-confirmed BBBFFL round
    -- atomically. The participating clubs, the Opening Round and each
    compensating bye are re-derived here from fresh live evidence (never
    trusted from the request); `targets` supplies only each club's BBBFFL
    round number. An identical already-accepted rule is a no-op; a
    different one fails closed (changing an accepted rule is an audited
    correction, not setup). Establishing a *new* rule is refused once any
    preseason pick has been made: Opening Round configuration is a
    before-Pick-1 prerequisite."""
    reason = _reason(reason)
    season = _writable_season(database, season_id)
    preview = preview_opening_round(database, afl_client, season_id, afl_season_id)
    if not preview["applicable"]:
        raise SeasonSetupError(preview["diagnostic"])
    if not preview["ready"]:
        raise SeasonSetupError(preview["diagnostic"] or "the Opening Round rule set is not ready to accept")
    proposals = {rule["afl_club_id"]: rule for rule in preview["rules"]}
    if set(targets) != set(proposals):
        raise SeasonSetupError(
            "a target BBBFFL round is required for exactly the clubs playing in the Opening Round "
            f"(expected {sorted(proposals)}, received {sorted(targets)})"
        )
    competition = _require_ordinary(database, season)
    rounds_by_number = _ordinary_rounds(database, competition.competition_id)
    for club_id, number in targets.items():
        if number not in rounds_by_number:
            raise SeasonSetupError(
                f"{proposals[club_id]['afl_club_name']}: BBBFFL round {number} is not one of this season's "
                f"ordinary rounds 1-{season.regular_season_round_count}"
            )
    opening_round_id = preview["opening_round_id"]
    validator = _KnownAflRounds(afl_season_id, preview["afl_round_ids"])
    rule_repo = OpeningRoundRuleRepository(database)
    seasons = SeasonRepository(database)
    accepted, unchanged = [], []
    with transaction(database) as conn:
        if database.engine.dialect.name == "sqlite":
            conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
        seasons.guard_writable(conn, season_id)
        if database.engine.dialect.name == "postgresql":
            # The season-scoped advisory lock the 2026 replay bootstrap's
            # Opening Round reconciliation takes, so the two can never
            # interleave rule acceptance for one season.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (season_id,))
        # The same `season_draft` row lock `DraftRepository.execute_pick`/
        # `correct_pick` take before completing a pick (`_locked_draft`), so
        # the before-Pick-1 check below cannot be decided on a snapshot a
        # concurrently completing pick is about to invalidate.
        conn.execute(
            "SELECT draft_id FROM season_draft WHERE season_id=? AND draft_kind='preseason'"
            + _for_update_suffix(database),
            (season_id,),
        )
        completed_picks = _completed_preseason_picks(conn, season_id)
        existing = {rule.afl_club_id: rule for rule in rule_repo.list_accepted_for_season_locked(conn, season_id)}
        unexpected = sorted(set(existing) - set(proposals))
        if unexpected:
            raise SeasonSetupError(
                f"accepted Opening Round rule(s) exist for club(s) {unexpected} that do not play in this Opening Round"
            )
        for club_id in sorted(proposals):
            proposal = proposals[club_id]
            round_id = rounds_by_number[targets[club_id]]
            current = rule_repo.resolve_locked(conn, season_id, club_id)
            if current is not None:
                if (
                    current.afl_season_id != afl_season_id
                    or current.afl_opening_round_id != opening_round_id
                    or current.afl_bye_round_id != proposal["afl_bye_round_id"]
                    or current.bbbffl_round_id != round_id
                ):
                    raise SeasonSetupError(
                        f"{proposal['afl_club_name']} already has an accepted Opening Round rule that differs from "
                        "this configuration; an accepted rule can only change through an audited correction"
                    )
                unchanged.append(proposal["afl_club_name"])
                continue
            if completed_picks:
                raise SeasonSetupError(
                    f"cannot add an Opening Round rule for {proposal['afl_club_name']}: {completed_picks} preseason "
                    "draft pick(s) are already complete, and Opening Round configuration is a before-Pick-1 "
                    "prerequisite"
                )
            rule_repo.accept_locked(
                conn,
                season_id,
                club_id,
                afl_season_id,
                opening_round_id,
                proposal["afl_bye_round_id"],
                round_id,
                validator,
                evidence_classification="known_fact",
                actor=actor,
                reason=reason,
            )
            accepted.append(proposal["afl_club_name"])
    return {"accepted": accepted, "unchanged": unchanged, "created": bool(accepted)}


# -- Preseason draft -------------------------------------------------------------


def configure_squad_limit(database, season_id: str, squad_limit: int, *, actor: ActorContext, reason) -> dict:
    """Set the season's squad limit through the existing ownership
    boundary. Re-submitting the current value is a no-op; any change after
    the draft order is accepted is refused by that boundary itself."""
    reason = _reason(reason)
    _writable_season(database, season_id)
    if not isinstance(squad_limit, int) or isinstance(squad_limit, bool) or squad_limit <= 0:
        raise SeasonSetupError("squad limit must be a positive whole number")
    current = _squad_limit(database, season_id)
    if current == squad_limit:
        return {"changed": False, "squad_limit": squad_limit}
    try:
        OwnershipRepository(database).configure_squad_limit(season_id, squad_limit, actor=actor, reason=reason)
    except ValueError as exc:
        raise SeasonSetupError(str(exc)) from exc
    return {"changed": True, "squad_limit": squad_limit, "previous_squad_limit": current}


def _squad_limit(database, season_id: str) -> int | None:
    row = database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
    ).fetchone()
    return row["squad_limit"] if row else None


def _draft_blockers(database, season, entries, squad_limit, pool) -> list[str]:
    blockers = []
    if len(entries) != TEAM_COUNT:
        blockers.append(f"exactly {TEAM_COUNT} season entries are required (currently {len(entries)})")
    structure = _ordinary_structure(database, season)
    if not structure["complete"]:
        blockers.append("the ordinary competition must be initialized first")
    if squad_limit is None:
        blockers.append("configure the squad limit first")
    elif pool["eligible"] < TEAM_COUNT * squad_limit:
        blockers.append(
            f"the player pool has {pool['eligible']} eligible players; a {TEAM_COUNT}-team draft of "
            f"{squad_limit} players each needs at least {TEAM_COUNT * squad_limit}"
        )
    return blockers


def _live_pool_afl_season_id(database, season_id: str) -> int:
    """The AFL season this season's pool was populated from, read back from
    its `source_provider` (`live_source_provider`) -- the AFL season whose
    fixture the draft's Opening Round gate must check."""
    providers = PlayerPoolRepository(database).summary(season_id)["source_providers"]
    prefix = f"{LIVE_SOURCE_PROVIDER}/season-"
    live = [p for p in providers if p.startswith(prefix) and p[len(prefix) :].isdigit()]
    if len(providers) != 1 or len(live) != 1:
        raise SeasonSetupError(
            "the player pool must be populated from exactly one live afl-api season before the draft order is "
            "accepted -- that AFL season's fixture decides whether Opening Round rules are required"
        )
    return int(live[0][len(prefix) :])


def _require_opening_round_decided(database, afl_client, season_id: str) -> dict:
    """Codex review, PR #247 (P1): Opening Round rules can only be added
    before Pick 1, so the draft must not start while the live fixture has
    an Opening Round whose rule set is not fully accepted. Re-checked here,
    from fresh live evidence, at the one boundary that opens drafting: if
    the AFL season has no round 0 the determination is "not required";
    otherwise every participating club must already have an accepted rule
    matching the fixture. Any afl-api failure fails closed (the draft order
    is not accepted)."""
    afl_season_id = _live_pool_afl_season_id(database, season_id)
    preview = preview_opening_round(database, afl_client, season_id, afl_season_id)
    if not preview["applicable"]:
        return {"afl_season_id": afl_season_id, "opening_round": "not_required"}
    missing = [rule["afl_club_name"] for rule in preview["rules"] if not rule["accepted_matches_fixture"]]
    if preview["opening_round_id"] is None or not preview["rules"] or missing or preview["diagnostic"]:
        detail = preview["diagnostic"] or (
            f"no accepted rule matching the fixture for {', '.join(missing)}" if missing else "rule set incomplete"
        )
        raise SeasonSetupError(
            "the live AFL fixture has an Opening Round: accept its compensating-bye rules before accepting the draft "
            f"order, since they cannot be added after Pick 1 ({detail})"
        )
    return {"afl_season_id": afl_season_id, "opening_round": "configured", "rule_count": len(preview["rules"])}


def accept_draft_order(
    database, afl_client, season_id: str, ordered_entry_ids: list[str], *, actor: ActorContext, reason
):
    """Accept the initial preseason draft order through the existing draft
    engine (`DraftRepository.accept_order`, which materialises every snake
    pick atomically). Re-submitting the identical accepted order is a
    no-op; any other order once one is accepted is refused. Refused while
    the live fixture's Opening Round (if any) is not fully configured --
    see `_require_opening_round_decided`."""
    reason = _reason(reason)
    season = _writable_season(database, season_id)
    ordered_entry_ids = list(ordered_entry_ids)
    draft = DraftRepository(database)
    accepted = [entry_id for _position, entry_id in draft.order(season_id)]
    if accepted:
        if accepted == ordered_entry_ids:
            return {"created": False, "order": accepted}
        raise SeasonSetupError("a different preseason draft order has already been accepted and is frozen")
    entries = IdentityRepository(database).list_entries(season_id)
    blockers = _draft_blockers(
        database, season, entries, _squad_limit(database, season_id), PlayerPoolRepository(database).summary(season_id)
    )
    if blockers:
        raise SeasonSetupError("; ".join(blockers))
    if len(ordered_entry_ids) != len(set(ordered_entry_ids)) or set(ordered_entry_ids) != {
        entry.season_entry_id for entry in entries
    }:
        raise SeasonSetupError("the draft order must list every one of this season's teams exactly once")
    opening_round = _require_opening_round_decided(database, afl_client, season_id)
    try:
        draft.accept_order(season_id, ordered_entry_ids, actor=actor, reason=reason)
    except (IntegrityError, ValueError) as exc:
        # A concurrent acceptance may have won between the read above and
        # this transaction (`accept_order` then refuses the now-frozen
        # order, or `uq_draft_season_kind` rejects the insert): re-read,
        # and treat the identical order as the no-op it is.
        accepted = [entry_id for _position, entry_id in draft.order(season_id)]
        if accepted == ordered_entry_ids:
            return {"created": False, "order": accepted}
        if accepted:
            raise SeasonSetupError("a different preseason draft order was accepted concurrently") from exc
        raise SeasonSetupError(str(exc)) from exc
    return {"created": True, "order": ordered_entry_ids, "opening_round": opening_round}


# -- Finals and SuperScore ---------------------------------------------------------


def _finals_streams(database, season_id: str):
    """This season's `finals` competition streams -- at most one. More than
    one is an ambiguous Finals phase: every caller (including the "already
    initialized" no-op and the SuperScore prerequisite) must fail closed
    rather than pick whichever stream happens to carry a bracket."""
    streams = [c for c in SeasonRepository(database).list_competitions(season_id) if c.stream_type == "finals"]
    if len(streams) > 1:
        raise SeasonSetupError(
            f"this season has {len(streams)} finals competition streams "
            f"({', '.join(stream.stream_key for stream in streams)}); exactly one is supported"
        )
    return streams


def _finals_bracket(database, season_id: str):
    repo = FinalsBracketRepository(database)
    for stream in _finals_streams(database, season_id):
        bracket = repo.get_bracket(season_id, stream.competition_id)
        if bracket is not None:
            return bracket
    return None


def initialize_finals(database, season_id: str, *, actor: ActorContext, reason) -> dict:
    """Create the Finals competition stream and its bracket, seeded from
    the live mathematical ladder -- only once every regular-season round is
    final and the ladder has no unresolved equality (the existing fail-
    closed competition-governance rule is preserved, not bypassed)."""
    reason = _reason(reason)
    season = _writable_season(database, season_id)
    ordinary = _require_ordinary(database, season)
    existing = _finals_bracket(database, season_id)
    if existing is not None:
        return {"created": False, "bracket_id": existing.bracket_id, "seed_source": existing.seed_source}
    repo = FinalsBracketRepository(database)
    preview = repo.preview_ladder_seed(season_id, ordinary.competition_id)
    if preview["historical_snapshot_exists"]:
        raise SeasonSetupError(
            "this season has a 2026 historical finals-seeding snapshot; live Finals initialization seeds only from "
            "the mathematical ladder and will not use replay seeding"
        )
    if not preview["ready"]:
        raise SeasonSetupError(f"Finals cannot be initialized yet: {preview['diagnostic']}")
    try:
        stream = repo.ensure_finals_stream(season_id, ordinary.rules_version_id, actor=actor, reason=reason)
        result = repo.create_bracket(
            season_id, stream["competition_id"], ordinary.competition_id, actor=actor, reason=reason
        )
    except (ValueError, RuntimeError) as exc:
        raise SeasonSetupError(f"Finals initialization refused: {exc}") from exc
    bracket = result["bracket"]
    return {
        "created": result["created"],
        "bracket_id": bracket.bracket_id,
        "seed_source": bracket.seed_source,
        "finals_stream_created": stream["created"],
    }


def initialize_superscore(database, season_id: str, *, actor: ActorContext, reason) -> dict:
    """Create the SuperScore stream and SS1-SS4 logical rounds -- only once
    the Finals bracket exists, since SuperScore runs concurrently with the
    four Finals weeks and its weekly opening is paired with theirs."""
    reason = _reason(reason)
    season = _writable_season(database, season_id)
    ordinary = _require_ordinary(database, season)
    if _finals_bracket(database, season_id) is None:
        raise SeasonSetupError("initialize Finals first: SuperScore runs alongside the Finals weeks")
    try:
        result = initialize_structure(
            database, season_id, ordinary.rules_version_id, ordinary.competition_id, actor=actor, reason=reason
        )
    except ValueError as exc:
        raise SeasonSetupError(str(exc)) from exc
    return {
        "created": result["created"],
        "stream_created": result["stream_created"],
        "created_rounds": result["created_rounds"],
        "competition_id": result["stream"].competition_id,
    }


# -- Read model ----------------------------------------------------------------------


def _step(key, title, status, summary, *, blockers=(), warnings=(), facts=None, links=None):
    return {
        "key": key,
        "title": title,
        "status": status,
        "summary": summary,
        "blockers": list(blockers),
        "warnings": list(warnings),
        "facts": facts or {},
        "links": links or {},
    }


def _all_regular_rounds_final(database, competition_id: str, round_count: int) -> tuple[bool, list[int]]:
    rows = database.execute(
        "SELECT br.sequence, bl.state FROM bbbffl_round br "
        "LEFT JOIN bbbffl_round_lifecycle bl ON bl.bbbffl_round_id=br.bbbffl_round_id "
        "WHERE br.competition_id=? AND br.sequence<=?",
        (competition_id, round_count),
    ).fetchall()
    final = {row["sequence"] for row in rows if row["state"] == "final"}
    outstanding = sorted(set(range(1, round_count + 1)) - final)
    return not outstanding, outstanding


def build_season_setup(database, season_id: str) -> dict:
    """The setup page's read model: every initialization boundary's current
    state, what it needs, and the next safe action. Reads persisted state
    only (no afl-api call); every command re-validates on its own."""
    season = _season(database, season_id)
    completed = season.lifecycle_state == "completed"
    entries = IdentityRepository(database).list_entries(season_id)
    pool = PlayerPoolRepository(database).summary(season_id)
    structure = _ordinary_structure(database, season)
    squad_limit = _squad_limit(database, season_id)
    draft = DraftRepository(database)
    draft_status = draft.status(season_id)
    names = {entry.season_entry_id: entry.team_name for entry in entries}
    accepted_rules = OpeningRoundRuleRepository(database).list_accepted_for_season(season_id)
    steps = []

    steps.append(
        _step(
            "entries",
            "Season entries",
            "complete" if len(entries) == TEAM_COUNT else "action_needed",
            f"{len(entries)} of {TEAM_COUNT} teams established",
            blockers=[] if len(entries) == TEAM_COUNT else [f"add coaches and teams until there are {TEAM_COUNT}"],
            facts={"entries": [dataclasses.asdict(entry) for entry in entries]},
            links={"season_centre": f"/admin/season-centre/{season_id}"},
        )
    )

    live_providers = [p for p in pool["source_providers"] if p.startswith(f"{LIVE_SOURCE_PROVIDER}/")]
    steps.append(
        _step(
            "player_pool",
            "Player pool (live afl-api)",
            "complete" if pool["total"] else "available",
            f"{pool['total']} players cached ({pool['eligible']} eligible)"
            if pool["total"]
            else "No players loaded yet -- populate from the live afl-api season player list",
            warnings=[]
            if not pool["total"] or live_providers
            else [f"this pool was not populated from live afl-api (source: {', '.join(pool['source_providers'])})"],
            facts=pool,
        )
    )

    ordinary_status = "complete" if structure["complete"] else ("conflict" if structure["competition"] else "available")
    ordinary_competition = structure["competition"]
    steps.append(
        _step(
            "ordinary_competition",
            "Ordinary competition",
            ordinary_status,
            f"{ordinary_competition.label}: Rounds 1-{season.regular_season_round_count}"
            if structure["complete"]
            else (structure["diagnostic"] or "Not initialized"),
            blockers=[structure["diagnostic"]] if structure["diagnostic"] else [],
            facts={
                "regular_season_round_count": season.regular_season_round_count,
                "competition_label": ordinary_competition.label if ordinary_competition else None,
                "round_count": len(structure["rounds"]),
            },
            links={"round_preflight": "/admin/round-preflight"} if structure["complete"] else {},
        )
    )

    completed_picks = draft_status.completed_picks if draft_status else 0
    if accepted_rules:
        opening_status, opening_summary = "complete", f"{len(accepted_rules)} compensating-bye rule(s) accepted"
    elif completed_picks:
        opening_status = "not_applicable"
        opening_summary = "No Opening Round rules; drafting has begun, so none can be added now"
    else:
        opening_status = "optional" if structure["complete"] else "blocked"
        opening_summary = "Only needed if the live AFL fixture has an Opening Round (round 0)"
    steps.append(
        _step(
            "opening_round",
            "Opening Round compensating byes",
            opening_status,
            opening_summary,
            blockers=[] if structure["complete"] else ["initialize the ordinary competition first"],
            facts={"accepted_rule_count": len(accepted_rules)},
            links={"nominations": f"/operations/seasons/{season_id}/opening-round"} if accepted_rules else {},
        )
    )

    squad_frozen = draft_status is not None
    steps.append(
        _step(
            "squad_limit",
            "Squad limit",
            "complete" if squad_limit else "available",
            f"{squad_limit} players per team" + (" (frozen by the accepted draft order)" if squad_frozen else "")
            if squad_limit
            else "Not configured",
            facts={"squad_limit": squad_limit, "frozen": squad_frozen},
        )
    )

    draft_blockers = [] if draft_status else _draft_blockers(database, season, entries, squad_limit, pool)
    draft_warnings = []
    if draft_status is None and not accepted_rules:
        draft_warnings.append(
            "If the live AFL fixture has an Opening Round, its compensating-bye rules must be accepted first: "
            "accepting the draft order re-checks the live fixture and is refused until they are."
        )
    order = draft.order(season_id)
    steps.append(
        _step(
            "draft_order",
            "Preseason draft order",
            "complete" if draft_status else ("blocked" if draft_blockers else "available"),
            (
                f"Accepted: {draft_status.completed_picks} of {draft_status.total_picks} picks made"
                + (" (finalized)" if draft_status.is_finalized else "")
            )
            if draft_status
            else "Not accepted",
            blockers=draft_blockers,
            warnings=draft_warnings,
            facts={
                "order": [
                    {"position": position, "season_entry_id": entry_id, "team_name": names.get(entry_id)}
                    for position, entry_id in order
                ]
            },
            links={
                "draft_board": f"/admin/draft/{season_id}",
                "preseason": f"/admin/preseason/{season_id}",
            }
            if draft_status
            else {},
        )
    )

    fixture_draw = FixtureRepository(database).get_draw(season_id)
    fixture_frozen = fixture_draw is not None and fixture_draw.state == "frozen"
    steps.append(
        _step(
            "fixture_draw",
            "Fixture-number draw",
            "complete" if fixture_frozen else ("action_needed" if len(entries) == TEAM_COUNT else "blocked"),
            "Accepted and frozen" if fixture_frozen else ("Proposed, not frozen" if fixture_draw else "Not drawn"),
            blockers=[] if len(entries) == TEAM_COUNT else [f"exactly {TEAM_COUNT} season entries are required"],
            links={"fixture_setup": f"/admin/fixture-setup/{season_id}"},
        )
    )

    try:
        finals_streams = _finals_streams(database, season_id)
        bracket = _finals_bracket(database, season_id)
        finals_conflict = None
    except SeasonSetupError as exc:
        finals_streams, bracket, finals_conflict = [], None, str(exc)
    finals_blockers: list[str] = []
    finals_facts: dict = {"finals_stream_exists": bool(finals_streams)}
    if finals_conflict is not None:
        finals_status, finals_summary = "conflict", "Ambiguous Finals structure"
        finals_blockers.append(finals_conflict)
    elif bracket is not None:
        finals_status = "complete"
        finals_summary = f"Bracket created (seeded from the {bracket.seed_source})"
        finals_facts.update(bracket_id=bracket.bracket_id, seed_source=bracket.seed_source)
    elif not structure["complete"]:
        finals_status, finals_summary = "blocked", "Not yet"
        finals_blockers.append("the ordinary competition must be initialized first")
    else:
        all_final, outstanding = _all_regular_rounds_final(
            database, ordinary_competition.competition_id, season.regular_season_round_count
        )
        if not all_final:
            finals_status, finals_summary = "blocked", "Available once the home-and-away season is complete"
            finals_blockers.append(
                f"{len(outstanding)} regular-season round(s) are not final yet (e.g. Round {outstanding[0]})"
            )
        else:
            preview = FinalsBracketRepository(database).preview_ladder_seed(
                season_id, ordinary_competition.competition_id
            )
            if preview["historical_snapshot_exists"]:
                finals_status, finals_summary = "blocked", "Historical replay seeding present"
                finals_blockers.append("this season carries a 2026 historical seeding snapshot; use replay tooling")
            elif preview["ready"]:
                finals_status, finals_summary = "available", "Ready: seed the top five from the final ladder"
                finals_facts["seed_order"] = [
                    {"seed": n, "team_name": names.get(entry_id, entry_id)}
                    for n, entry_id in enumerate(preview["seed_order"], 1)
                ]
            else:
                finals_status, finals_summary = "blocked", "Ladder cannot seed Finals"
                finals_blockers.append(preview["diagnostic"])
        if finals_streams:
            finals_summary += " -- finals stream created, bracket pending"
    steps.append(
        _step(
            "finals",
            "Finals stream and bracket",
            finals_status,
            finals_summary,
            blockers=finals_blockers,
            facts=finals_facts,
            links={"round_preflight": "/admin/round-preflight"} if bracket else {},
        )
    )

    ss_stream_count = database.execute(
        "SELECT COUNT(*) AS n FROM superscore_stream WHERE season_id=?", (season_id,)
    ).fetchone()["n"]
    stream = get_stream(database, season_id) if ss_stream_count == 1 else None
    ss_rounds = [r for r in SeasonRepository(database).list_rounds(stream.competition_id)] if stream is not None else []
    # The exact `(sequence, round_key, label)` shape `initialize_structure`
    # creates and requires -- never keys alone, so a differently-shaped
    # structure is reported as the conflict that command would refuse, not
    # as usable (Codex review, PR #247).
    expected_ss = [(number, label.lower(), label) for number, label in sorted(ROUND_LABELS.items())]
    actual_ss = [(r.sequence, r.round_key, r.label) for r in ss_rounds]
    unexpected_ss = [shape[1] for shape in actual_ss if shape not in expected_ss]
    if ss_stream_count > 1:
        ss_status, ss_summary = "conflict", "Ambiguous SuperScore structure"
        ss_blockers = [f"this season has {ss_stream_count} SuperScore streams; exactly one is supported"]
    elif stream is not None and actual_ss == expected_ss:
        ss_status, ss_summary, ss_blockers = "complete", "SuperScore stream with SS1-SS4 created", []
    elif unexpected_ss:
        ss_status, ss_summary = "conflict", "SuperScore rounds are differently shaped"
        ss_blockers = [
            f"unexpected or differently-shaped SuperScore round(s) {unexpected_ss}; setup will not modify them"
        ]
    elif bracket is None:
        ss_status, ss_summary = "blocked", "Available once the Finals bracket exists"
        ss_blockers = ["initialize Finals first"]
    else:
        ss_status, ss_summary, ss_blockers = "available", "Ready: create the SuperScore stream and SS1-SS4", []
        if stream is not None:
            ss_summary = f"SuperScore stream exists with {len(ss_rounds)} of 4 rounds -- complete it"
    steps.append(
        _step(
            "superscore",
            "SuperScore stream and SS1-SS4",
            ss_status,
            ss_summary,
            blockers=ss_blockers,
            facts={"rounds": [r.label for r in ss_rounds]},
        )
    )

    if completed:
        for step in steps:
            if step["status"] not in ("complete", "not_applicable"):
                step["status"] = "read_only"
    next_step = next((step for step in steps if step["status"] in ("available", "action_needed")), None)
    return {
        "season": dataclasses.asdict(season),
        "read_only": completed,
        "steps": steps,
        "next_step": next_step["key"] if next_step else None,
        "next_step_title": next_step["title"] if next_step else None,
    }
