"""Architecture/import-boundary tests.

These encode, mechanically, the dependency direction documented in
docs/architecture.md:

    routes  ->  application services  ->  domain/season repositories  ->  db/audit (persistence)

with `app.scoring` kept as a pure, dependency-free leaf reused by the
application layer (the "proven scoring implementation" issue #36 requires
this refactor not disturb), and the Grand Final/SuperScore vertical
(`app.teams`/`app.superscore`/`app.service`/`app.scorer_decisions`) kept a
sibling of -- not a dependency of -- the season-model repositories that
later roadmap packages (season/identity/ownership/fixtures/lineups/
lockouts) already introduced.

Each module in `app/` is placed in exactly one group below. A new module
must be added to a group deliberately -- `test_every_app_module_is_classified`
fails otherwise -- rather than silently picking up whatever imports happen
to compile. Membership in a group does not forbid intra-group imports (e.g.
`app.lockouts` depending on `app.lineups`, or `app.routes.superscore`
depending on `app.routes.admin`); it only bounds which *other* groups a
group may reach into, via the forbidden-edge assertions below.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"

# -- Groups -----------------------------------------------------------------
# Foundation: zero internal dependencies. `app.scoring` is the proven
# scoring engine -- see app/scoring.py's module role and issue #36's
# "preserve the existing proven scoring implementation" constraint.
FOUNDATION = {
    "app.config",
    "app.audit",
    "app.scoring",
    "app.afl_client",
    # Coach authentication leaves (roadmap package 19, issue #74): each
    # takes whatever secret/clock it needs as a parameter rather than
    # importing app.config or anything else internal, so they stay
    # dependency-free exactly like the rest of this group.
    "app.password_hashing",
    "app.csrf",
    "app.auth_rate_limit",
}

# Persistence core: the only place SQL/transaction plumbing lives.
PERSISTENCE_CORE = {"app.db", "app.migrations"}

# Season-model domain: the season/configuration, coach/team/ownership and
# weekly-selection, and competition/fixture/result boundaries introduced by
# roadmap packages 04-12 (see docs/season-competition-schema.md,
# docs/player-pool-ownership.md, docs/fixture-draw.md,
# docs/round-afl-mapping.md, docs/competition-lifecycle.md,
# docs/weekly-lineups.md, docs/scoring-calculations.md). Each class here is
# the combined repository-and-invariant boundary for one aggregate (see
# docs/architecture.md) -- not routed from HTTP yet.
SEASON_MODEL = {
    "app.season",
    "app.identity",
    "app.player_pool",
    "app.draft",
    "app.preseason",
    "app.fixtures",
    "app.round_mapping",
    "app.competition_lifecycle",
    "app.lineups",
    "app.calculations",
    "app.ladder",
}

# Lockouts sits one layer above the season model (it depends on
# app.lineups for the POSITIONS vocabulary) but is still not HTTP-routed.
LOCKOUTS = {"app.lockouts"}

# Non-coach weekly-lineup submission sources (roadmap package 22, issue
# #55): carry-forward and scorer/admin proxy entry. Each sits one layer
# above the season model -- both depend on app.lineups for
# WeeklyLineupRepository/POSITIONS, and app.lineup_proxy also depends on
# app.audit for ActorContext -- but neither is HTTP-routed. Like
# app.lockouts, both accept an optional `lock_guard` as a duck-typed
# collaborator rather than importing app.lockouts directly, keeping
# immutable submission history decoupled from lock evaluation/evidence.
WEEKLY_SUBMISSION_SOURCES = {"app.carry_forward", "app.lineup_proxy", "app.lineup_validation"}

# AFL resilience boundary (roadmap package 05, issue #37): retry/backoff,
# cache/evidence-state and diagnostics wrapped directly around the
# foundation `app.afl_client` transport. Only the composition root
# (app.main) constructs and wires this wrapper -- every domain/service
# module keeps depending on app.afl_client's plain dataclasses/duck-typed
# AflDataSource protocol, never on this wrapper's concrete type, so it stays
# a drop-in replacement rather than a new required dependency.
AFL_RESILIENCE = {"app.afl_resilience", "app.afl_diagnostics"}

# The Grand Final/SuperScore prototype vertical: coach-declared team config,
# the scoring orchestration service, and the scorer-decision application
# service that routes call. A sibling of the season-model domain, not a
# dependency of it.
GRAND_FINAL_VERTICAL = {
    "app.teams",
    "app.superscore",
    "app.presentation",
    "app.service",
    "app.scorer_decisions",
}

# Scorer round-review application service (roadmap package 28, issue #58):
# the ordinary-round counterpart to app.scorer_decisions/GRAND_FINAL_VERTICAL
# -- but unlike that vertical, it sits directly *on top of* the persisted
# season model (app.competition_lifecycle for the round/matchup/official-
# result transaction and its StaleRoundVersionError, app.lineups for the
# roster-slot vocabulary) rather than being a sibling of it, because it is
# meant to be imported directly by routes exactly the way
# app.scorer_decisions is (see routes/admin.py) -- every other SEASON_MODEL
# module is deliberately *not* importable from a route file (see
# test_routes_never_import_persistence_or_season_model_directly) and is
# reached only via an already-constructed instance on `request.app.state`.
ROUND_REVIEW = {"app.round_review"}

# Opening Round deferred-selection configuration/nomination/locking (issue
# #69): sits above *both* the season model and lockouts -- it reuses
# app.lockouts.resolve_match/MatchResolutionError for AFL match resolution
# (the same team-id-to-match logic lockouts already uses, rather than a
# second copy) and app.lineups for the roster-slot vocabulary and
# WeeklyLineupRepository (to preload a nominated slot). Like app.lockouts,
# it is not HTTP-routed and must never be depended upon by the season model,
# lockouts, weekly-submission-sources, the Grand Final vertical, routes, or
# the composition root.
OPENING_ROUND = {"app.opening_round"}

# Coach authentication/session application service (roadmap package 19,
# issue #74): sits above the season model, the same shape as
# app.round_review/app.opening_round -- it resolves logins directly to
# app.identity.IdentityRepository's existing persistent coach identity
# (never a second coach/user model) and owns app.db's coach_credential/
# coach_session tables. Unlike app.round_review, it *is* meant to be
# imported directly by its route module (app/routes/auth.py) for the same
# reason app.scorer_decisions is -- see this file's ROUND_REVIEW comment.
# Like every module in this position, it must stay a sibling of the Grand
# Final vertical and must never depend on routes or the composition root,
# and no lower layer (season model, lockouts, weekly-submission-sources)
# may depend back on it.
AUTH = {"app.auth"}

# AFL participation-evidence classification (roadmap package 26, issue #57):
# a pure function of public afl-api facts (a match, a bye list, a stat line)
# with no state and no dependency beyond the foundation `app.afl_client`
# dataclasses it classifies. Originally only the Grand Final vertical's
# `app.service` consumed it; roadmap package 28 (issue #58)'s
# `app.calculations`/`app.round_review` reuse the same classification for
# the persisted season model, so this sits as its own small sibling group
# rather than inside GRAND_FINAL_VERTICAL -- neither domain depends on the
# other through it.
AFL_EVIDENCE = {"app.participation"}

# Deterministic replay evidence/time and completed-round diagnostics (issue
# #66): an application service above the AFL dataclasses and persisted season
# model. It may read ladder/result state for reporting, but must never reach
# into HTTP routes, the Grand Final sibling vertical, or the composition root.
# `app.replay_checkpoint` (issue #67) is its sibling: pure checkpoint-report
# assembly for the staged-lockout/bye/carry-forward/proxy/Opening-Round
# scenarios, with no imports of its own beyond the standard library.
REPLAY = {"app.replay", "app.replay_checkpoint"}

ROUTES = {
    "app.routes",
    "app.routes.admin",
    "app.routes.public",
    "app.routes.superscore",
    "app.routes.health",
    "app.routes.draft",
    "app.routes.preseason",
    "app.routes.round_review",
    "app.routes.auth",
}

COMPOSITION_ROOT = {"app.main"}

ALL_GROUPS = (
    FOUNDATION
    | PERSISTENCE_CORE
    | SEASON_MODEL
    | LOCKOUTS
    | WEEKLY_SUBMISSION_SOURCES
    | AFL_RESILIENCE
    | AFL_EVIDENCE
    | REPLAY
    | ROUND_REVIEW
    | OPENING_ROUND
    | AUTH
    | GRAND_FINAL_VERTICAL
    | ROUTES
    | COMPOSITION_ROOT
)


def _dotted_module_name(path: Path) -> str:
    relative = path.relative_to(REPO_ROOT)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_app_import(module: str, name: str) -> str:
    """`from <module> import <name>` -- returns the dotted dependency this
    represents: `<module>.<name>` when that names an actual submodule on
    disk (e.g. `from app.routes import admin`, `from app import db`), else
    just `<module>` itself (e.g. `from app.config import BASE_DIR`)."""
    candidate = f"{module}.{name}"
    candidate_path = REPO_ROOT / Path(*candidate.split("."))
    if candidate_path.with_suffix(".py").exists() or (candidate_path / "__init__.py").exists():
        return candidate
    return module


def _resolve_relative_module(dotted: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    """Resolve `from .foo import bar` / `from . import bar` (node.level>=1)
    against the importing module's own package, the same way Python's
    import system does (see importlib._bootstrap._resolve_name). No module
    under app/ uses a relative import today, but a future one must still be
    classified rather than silently dropped from the graph -- an unresolved
    relative import would otherwise let a route bypass the persistence-
    boundary assertions, and a relative import participating in a cycle
    would bypass cycle detection entirely."""
    package_parts = list(dotted.split(".")) if is_package else list(dotted.split("."))[:-1]
    if node.level > 1:
        strip = node.level - 1
        if strip > len(package_parts):
            return None  # climbs above the package root; not a valid app.* edge
        package_parts = package_parts[: len(package_parts) - strip]
    base = ".".join(package_parts)
    if not base:
        return None
    return f"{base}.{node.module}" if node.module else base


def build_import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        dotted = _dotted_module_name(path)
        if not dotted or dotted == "app":
            continue
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text(), filename=str(path))
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app" or alias.name.startswith("app."):
                        deps.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    module = _resolve_relative_module(dotted, is_package, node)
                else:
                    module = node.module
                if module is None:
                    continue
                if module != "app" and not module.startswith("app."):
                    continue
                for alias in node.names:
                    deps.add(_resolve_app_import(module, alias.name))
        deps.discard(dotted)
        deps.discard("app")
        graph[dotted] = deps
    return graph


@pytest.fixture(scope="module")
def graph() -> dict[str, set[str]]:
    return build_import_graph()


def _parse_import_from(source: str) -> ast.ImportFrom:
    (node,) = ast.parse(source).body
    assert isinstance(node, ast.ImportFrom)
    return node


@pytest.mark.parametrize(
    "dotted, is_package, source, expected",
    [
        # A plain module's relative import resolves against its parent
        # package -- app.season's package is "app".
        ("app.season", False, "from .db import transaction", "app.db"),
        # Each extra leading dot climbs one more package level, exactly as
        # importlib resolves it: app.routes.admin's package is "app.routes",
        # so ".." climbs to "app".
        ("app.routes.admin", False, "from ..db import transaction", "app.db"),
        ("app.routes.admin", False, "from . import public", "app.routes"),
        # A package's (__init__.py's) own relative import resolves within
        # itself, not its parent.
        ("app.routes", True, "from .admin import router", "app.routes.admin"),
    ],
)
def test_resolve_relative_module_matches_python_import_semantics(dotted, is_package, source, expected):
    """No module under app/ uses a relative import today (confirmed by
    `test_every_app_module_is_classified` running clean against the real
    tree), but `build_import_graph` must still resolve one correctly rather
    than silently dropping the edge -- see `_resolve_relative_module`'s
    docstring for why an unresolved relative import would defeat this
    suite's whole purpose."""
    node = _parse_import_from(source)
    assert _resolve_relative_module(dotted, is_package, node) == expected


def test_every_app_module_is_classified(graph):
    """A new module under app/ must be deliberately placed in one of this
    file's groups -- see the module docstring -- rather than silently
    joining the dependency graph unclassified."""
    unclassified = set(graph) - ALL_GROUPS
    assert not unclassified, f"unclassified app modules (add to a group above): {sorted(unclassified)}"

    stale = ALL_GROUPS - set(graph)
    assert not stale, f"groups reference modules that no longer exist: {sorted(stale)}"


def test_no_import_cycles(graph):
    """The dependency graph must be acyclic -- a cycle here would mean two
    modules each need the other to already be defined, which Python can
    only paper over via deferred/local imports and is exactly the
    "circular imports" issue #36 asks this refactor to avoid."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {module: WHITE for module in graph}
    path: list[str] = []

    def visit(module: str) -> None:
        color[module] = GRAY
        path.append(module)
        for dep in sorted(graph.get(module, ())):
            if dep not in graph:
                continue
            if color[dep] == GRAY:
                cycle = path[path.index(dep) :] + [dep]
                pytest.fail(f"circular import detected: {' -> '.join(cycle)}")
            if color[dep] == WHITE:
                visit(dep)
        path.pop()
        color[module] = BLACK

    for module in sorted(graph):
        if color[module] == WHITE:
            visit(module)


@pytest.mark.parametrize("module", sorted(FOUNDATION))
def test_foundation_modules_have_no_internal_dependencies(graph, module):
    """`app.config`, `app.audit`, `app.afl_client` and -- most importantly
    for issue #36 -- `app.scoring` (the proven scoring engine) must stay
    dependency-free leaves that any other layer can safely import."""
    assert graph[module] == set(), f"{module} must have zero internal app.* dependencies, found {graph[module]}"


def test_db_depends_only_on_audit(graph):
    """The persistence core may append audit events but must not know about
    any domain's policy -- repositories own persistence mechanics, not
    league policy owns persistence (issue #36)."""
    assert graph["app.db"] == {"app.audit"}


def test_migrations_depends_only_on_db(graph):
    assert graph["app.migrations"] == {"app.db"}


def test_season_model_and_lockouts_do_not_depend_on_routes_or_grand_final_vertical(graph):
    """The season-model domain (and lockouts/weekly-submission-sources, one
    layer above it) must not reach sideways into the Grand Final/SuperScore
    vertical, nor up into routes or the composition root -- new 2027 domain
    rules belong in these modules, never in a route handler (issue #36)."""
    forbidden = GRAND_FINAL_VERTICAL | ROUTES | COMPOSITION_ROOT
    for module in sorted(SEASON_MODEL | LOCKOUTS | WEEKLY_SUBMISSION_SOURCES):
        offending = graph[module] & forbidden
        assert not offending, f"{module} must not depend on {sorted(offending)}"


def test_weekly_submission_sources_stay_decoupled_from_lockouts(graph):
    """app.carry_forward and app.lineup_proxy accept an optional
    `lock_guard` as a duck-typed collaborator (mirroring
    `WeeklyLineupRepository.submit`) rather than importing app.lockouts
    directly -- see this file's WEEKLY_SUBMISSION_SOURCES docstring and
    app/lineups.py's module docstring on why lock evaluation/evidence must
    stay decoupled from immutable submission history."""
    for module in sorted(WEEKLY_SUBMISSION_SOURCES):
        assert "app.lockouts" not in graph[module], f"{module} must not depend on app.lockouts"


def test_afl_resilience_depends_only_on_foundation(graph):
    """The retry/cache/diagnostics wrapper around afl-api (roadmap package
    05, issue #37) sits directly on top of app.afl_client and app.audit --
    it must not reach into any domain, lockouts, routes, the Grand Final
    vertical, or the composition root."""
    forbidden = SEASON_MODEL | LOCKOUTS | GRAND_FINAL_VERTICAL | ROUTES | COMPOSITION_ROOT
    for module in sorted(AFL_RESILIENCE):
        offending = graph[module] & forbidden
        assert not offending, f"{module} must not depend on {sorted(offending)}"


def test_afl_evidence_depends_only_on_foundation(graph):
    """`app.participation` (roadmap package 26, issue #57) must stay a pure
    classification leaf over `app.afl_client` facts -- both the season
    model (`app.calculations`, roadmap package 28) and the Grand Final
    vertical (`app.service`) depend on it, so it must never depend back on
    either, nor on lockouts, routes, or the composition root."""
    forbidden = SEASON_MODEL | LOCKOUTS | GRAND_FINAL_VERTICAL | ROUTES | COMPOSITION_ROOT
    for module in sorted(AFL_EVIDENCE):
        offending = graph[module] & forbidden
        assert not offending, f"{module} must not depend on {sorted(offending)}"


def test_replay_does_not_depend_on_routes_grand_final_or_composition_root(graph):
    """Also asserts REPLAY never depends on AUTH (roadmap package 19, issue
    #74): deterministic replay must stay independent of coach
    authentication -- see docs/coach-authentication.md."""
    forbidden = GRAND_FINAL_VERTICAL | LOCKOUTS | ROUTES | COMPOSITION_ROOT | AUTH
    for module in sorted(REPLAY):
        offending = graph[module] & forbidden
        assert not offending, f"{module} must not depend on {sorted(offending)}"


def test_domain_layers_do_not_depend_on_replay(graph):
    """Replay orchestrates production domains; it is never domain policy."""
    lower_layers = SEASON_MODEL | LOCKOUTS | WEEKLY_SUBMISSION_SOURCES | AFL_EVIDENCE | ROUND_REVIEW
    for module in sorted(lower_layers):
        assert not graph[module] & REPLAY, f"{module} must not depend on replay orchestration"


def test_lineups_and_lockouts_stay_decoupled(graph):
    """app/lineups.py's module docstring documents a deliberate decoupling:
    lockouts depends on lineups (for the POSITIONS vocabulary and as a
    collaborator injected into `submit`), but lineups must never import
    lockouts back -- that would both cycle and entangle immutable
    submission history with lock evaluation/evidence."""
    assert "app.lockouts" not in graph["app.lineups"]


def test_grand_final_vertical_does_not_depend_on_season_model_or_routes(graph):
    """`app.teams`/`app.superscore`/`app.service`/`app.scorer_decisions` are
    a sibling domain to the season model, not a consumer of it, and (like
    every non-route module) must not import route modules."""
    forbidden = SEASON_MODEL | LOCKOUTS | ROUTES | COMPOSITION_ROOT
    for module in sorted(GRAND_FINAL_VERTICAL):
        offending = graph[module] & forbidden
        assert not offending, f"{module} must not depend on {sorted(offending)}"


def test_round_review_does_not_depend_on_routes_grand_final_or_composition_root(graph):
    """`app.round_review` (roadmap package 28, issue #58) sits directly on
    the season model (unlike `app.scorer_decisions`), but must stay a
    sibling of -- never a dependency of -- the Grand Final vertical, and
    must never depend on routes or the composition root, exactly like
    every other application-service module."""
    forbidden = GRAND_FINAL_VERTICAL | LOCKOUTS | ROUTES | COMPOSITION_ROOT
    for module in sorted(ROUND_REVIEW):
        offending = graph[module] & forbidden
        assert not offending, f"{module} must not depend on {sorted(offending)}"


def test_opening_round_does_not_depend_on_grand_final_routes_or_composition_root(graph):
    """`app.opening_round` (issue #69) sits above the season model and
    lockouts -- see this file's OPENING_ROUND docstring -- but must stay a
    sibling of the Grand Final vertical and must never depend on routes or
    the composition root, exactly like every other application-service
    module."""
    forbidden = GRAND_FINAL_VERTICAL | ROUTES | COMPOSITION_ROOT
    for module in sorted(OPENING_ROUND):
        offending = graph[module] & forbidden
        assert not offending, f"{module} must not depend on {sorted(offending)}"


def test_season_model_and_lockouts_do_not_depend_on_opening_round(graph):
    """The reverse direction of the edge above: neither the season model
    nor lockouts (nor weekly-submission-sources) may depend on
    `app.opening_round` -- it is a consumer of those layers, never a
    dependency of them, exactly like app.round_review's relationship to
    app.competition_lifecycle."""
    for module in sorted(SEASON_MODEL | LOCKOUTS | WEEKLY_SUBMISSION_SOURCES):
        assert "app.opening_round" not in graph[module], f"{module} must not depend on app.opening_round"


def test_auth_does_not_depend_on_grand_final_routes_or_composition_root(graph):
    """`app.auth` (roadmap package 19, issue #74) sits above the season
    model -- it depends on app.identity to resolve logins to the existing
    persistent coach identity -- but must stay a sibling of the Grand
    Final vertical and must never depend on routes or the composition
    root, exactly like app.round_review/app.opening_round."""
    forbidden = GRAND_FINAL_VERTICAL | ROUTES | COMPOSITION_ROOT
    for module in sorted(AUTH):
        offending = graph[module] & forbidden
        assert not offending, f"{module} must not depend on {sorted(offending)}"


def test_season_model_and_lockouts_do_not_depend_on_auth(graph):
    """The reverse direction of the edge above: neither the season model
    nor lockouts (nor weekly-submission-sources) may depend on app.auth --
    it is a consumer of app.identity, never a dependency of it."""
    for module in sorted(SEASON_MODEL | LOCKOUTS | WEEKLY_SUBMISSION_SOURCES):
        assert "app.auth" not in graph[module], f"{module} must not depend on app.auth"


def test_scorer_decisions_stays_repository_agnostic(graph):
    """app/scorer_decisions.py is the application-service boundary
    extracted from routes/admin.py and routes/superscore.py (issue #36): it
    must depend only on the scoring vocabulary and audit actor identity, and
    never on app.db, so it stays usable against any `DecisionsRepository`-
    shaped object a caller passes in (HTTP route, admin script, replay,
    test) rather than hard-wiring a persistence dependency."""
    assert graph["app.scorer_decisions"] == {"app.audit", "app.scoring"}


def test_routes_never_import_persistence_or_season_model_directly(graph):
    """Route handlers orchestrate application services; they must not reach
    past that boundary into raw persistence or an unrouted domain
    repository (issue #36: "move orchestration/business rules out of HTTP
    route handlers", "repositories own persistence mechanics")."""
    forbidden = PERSISTENCE_CORE | SEASON_MODEL | LOCKOUTS | COMPOSITION_ROOT
    for module in sorted(ROUTES):
        offending = graph[module] & forbidden
        assert not offending, f"{module} must not depend on {sorted(offending)}"


def test_nothing_outside_routes_or_main_imports_routes(graph):
    """Route modules are consumed by the composition root (app/main.py) and
    by each other (routes/superscore.py reuses routes/admin.py's request
    schemas), never by a domain/service/persistence module -- that
    direction would invert the intended dependency flow."""
    for module, deps in graph.items():
        if module in ROUTES or module in COMPOSITION_ROOT:
            continue
        offending = deps & ROUTES
        assert not offending, f"{module} must not depend on route modules {sorted(offending)}"
