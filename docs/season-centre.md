# Season Centre

Issue #100 gives the scorer/admin a browser surface over the season/coach/
season-entry identity domain roadmap packages 09-10 (issues #19/#20)
already established, so a human replay operator can establish and inspect
recognisable BBBFFL season state -- actual coach names and BBBFFL team
names -- before proceeding through draft, fixture, round setup, weekly
selection and scoring, without SQL or direct database manipulation.

This is deliberately **an operator surface, not a redesign**: it adds no
parallel season/team identity model. Every identity fact it shows is read
straight from `app/season.py` (season identity/lifecycle, rules versions,
competition streams) and `app/identity.py` (private coach records, public
season-entry team identity), and every edit it makes is a direct call into
those same repositories -- durable relationships (`season_id`,
`season_entry_id`, `coach_id`) never change when a team is renamed or a
coach is reassigned; only the current display value does, exactly like
`IdentityRepository.rename_team`/`transfer_entry` already guaranteed before
this issue.

## What it does not do

Season Centre only links to draft, player pool, opening squads, fixture and
round setup -- it does not implement any of that behaviour itself. It shows
each workflow's current status (from the same repositories those workflows
already use) and links to the existing page when one is reachable; where no
admin page exists yet (player pool, opening squads, fixture setup), it says
so honestly rather than inventing a page. It is scorer/admin season setup,
not coach account/profile management: there is no authentication, password
recovery, or account-lifecycle behaviour here (see `app/auth.py`/
`docs/coach-authentication.md` for that, entirely separate).

## Application service: `app/season_centre.py`

Sits directly on the season model, the same shape as `app/round_review.py`/
`app/opening_round.py`/`app/auth.py` (see `tests/test_architecture.py`'s
`SEASON_CENTRE` group): it is imported directly by its route module
(`app/routes/season_centre.py`), unlike every other season-model module,
which routes may only reach via an already-constructed instance on
`request.app.state`.

- `build_season_centre(seasons, identities, draft, preseason, player_pool,
  lifecycle, season_id)` -- the read model: season identity/lifecycle,
  configured competition streams, every season entry's public team name and
  current coach display name (`IdentityRepository.list_entries`,
  deliberately narrower than `Coach` -- no email/phone/profile_notes), a
  readiness summary (entries established, distinct coaches, competition
  streams configured, player pool size, draft status, preseason window
  status, ordinary rounds created), and links to the draft board / Scorer
  Round Centre when each is actually reachable for this season.
- `create_season`, `create_coach`/`update_coach`, `create_entry`,
  `rename_team`, `transfer_entry` -- thin validated wrappers around the
  matching `SeasonRepository`/`IdentityRepository` methods. Each translates
  the underlying `IntegrityError` (duplicate year/licence, unknown season or
  coach) into a plain `SeasonCentreError` (a `ValueError`), which
  `app/main.py`'s existing generic `ValueError` handler already maps to
  HTTP 400 -- no new exception handler was needed.
- `create_entry` auto-generates a `licence_key` when the operator does not
  supply one: it is the entry's durable internal slot identifier (see
  `app/identity.py`'s module docstring), not something an operator
  establishing recognisable replay identities should need to invent.

`IdentityRepository` gained two small additions to support this:
`list_entries(season_id)` (the joined team-name/coach-display-name view
above) and `update_coach(...)` (in-place edit of a coach's own display
name/contact fields -- there is exactly one current coach row, unlike team
name/coach assignment, which are versioned histories). `SeasonRepository`
gained `list_seasons()` so an operator can pick a season without already
knowing its UUID.

## Routes: `app/routes/season_centre.py`

- `GET/POST /api/admin/season-centre/seasons` -- list/create seasons.
- `GET/POST /api/admin/season-centre/coaches`, `POST .../coaches/{coach_id}`
  -- list/create/edit coach records.
- `GET /api/admin/season-centre/{season_id}` -- the season centre view.
- `POST /api/admin/season-centre/{season_id}/entries` -- create a season
  entry (coach + public team name).
- `POST /api/admin/season-centre/entries/{entry_id}/team-name` -- rename
  the public team.
- `POST /api/admin/season-centre/entries/{entry_id}/coach` -- reassign the
  current coach.
- `GET /admin/season-centre`, `GET /admin/season-centre/{season_id}` --
  the browser page (`templates/season_centre.html`), a client-rendered
  shell over the API above, matching the existing `draft.html`/
  `round_centre.html` pattern (token bar, fetch-and-render, inline
  success/error feedback).

Every endpoint requires `require_secretary_or_admin` (Secretary/League
Manager authority or Administrator) -- **retrofitted by roadmap package
#107** (issue #107, see [acting-context](acting-context.md)) from the
strict admin-only authority this router originally required. Ordinary
league-season setup is Secretary/League Manager's own operational
authority; the legacy shared operator token still resolves to
Administrator by default (narrowing it to `scorer` is refused here, same
as before), but a signed-in coach identity granted Secretary authority
(`app.auth.RoleGrantRepository`) can now run this whole page without also
holding Administrator. The page shell also renders the shared active-
context bar (`app.routes.context`) so the operator can see and switch
their active role/represented context, and an Administrator-only panel to
grant/revoke roles for a coach identity.

## Privacy

The entries table (`list_entries`/`SeasonEntryOverview`) and the season
centre read model never carry coach email/phone/profile_notes -- only
`coach_id` and `coach_display_name`. Coach contact details are visible only
in the (admin-only) coach-management section of the page, itself gated by
the same `require_admin` dependency as everything else here. No public
route serves any Season Centre data.

## 2026 replay usage

The Season Centre requires no seed data or bootstrap script: from a clean
database, an operator creates the 2026 replay season, then its ten coaches
and season entries, entirely through this UI (`POST .../seasons`, then
`POST .../coaches` and `POST .../{season_id}/entries` ten times) --
straight through `app.identity.IdentityRepository`, the same repository
every other identity write in this codebase goes through. The 2026 replay
and 2027 seasons are ordinary, independent `bbbffl_season` rows (see
`app/season.py`); nothing in this issue makes either one an implicit
default -- every Season Centre route takes an explicit `season_id`.
