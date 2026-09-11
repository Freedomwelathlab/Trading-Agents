"""Phase 68 (D086): an automated regression guard over the ACTUAL constructed
FastAPI `app` (`apps/api/app/main.py`) proving every mounted route requires
SOME authentication dependency.

**What this test checks, precisely.** It walks `app.routes` down to every
real `fastapi.routing.APIRoute` (recursing through the `_IncludedRouter`
wrapper this FastAPI version uses for `app.include_router(...)`, via its
`original_router.routes`), and for every route whose path is NOT on the
explicit, documented public allowlist below, asserts that
`apps.api.app.auth.dependencies.get_current_user` appears SOMEWHERE in that
route's resolved dependency tree (`route.dependant`, recursed through every
`.dependencies` entry - which is where FastAPI merges router-level
`dependencies=[...]` AND per-parameter `Depends(...)` alike, so this one
walk covers both authorization shapes this codebase uses: `dependencies=
[Depends(require_permission(...))]` at the `APIRouter(...)` level, and
`Depends(get_current_user)` / `Depends(require_permission(...))` /
`Depends(require_broker_access(...))` on an individual route function).
Every one of those three ultimately depends on `get_current_user` itself
(`require_permission`'s own `checker` takes `Depends(get_current_user)` as
a parameter default, and `require_broker_access`'s `checker` takes
`Depends(require_permission(permission))`), so finding it anywhere in the
tree is sufficient to prove SOME authentication gate exists, however it was
spelled.

**What this test deliberately does NOT check.** It is a regression guard
against a FUTURE route being added with NO auth dependency at all - it is
NOT a claim that the permission checked is the CORRECT one for that route,
and it is not a re-verification of ownership checks
(`_load_owned_strategy`, `_load_owned_deployment`, `require_broker_access`'s
own `BrokerGrant` lookup) or of an in-handler permission check that happens
AFTER the dependency graph resolves (e.g. `_authorize_live_trade`'s
`SUBMIT_LIVE_TRADE` check in `routes/trades.py`, or
`approve_strategy_deployment`'s in-handler
`STRATEGY_APPROVE_LIVE_DEPLOYMENT` check in `routes/deployments.py` - both
already pass this test via their OWN base dependency, and both already have
dedicated tests in `tests/api/test_trades.py` / `tests/api/test_deployments.py`
that check the permission itself is enforced). Verifying that the
authorization LOGIC is correct for a given route is what that route's own
tests already do; this test exists only to catch the specific failure mode
of a route added with zero auth dependency at all.

**The public allowlist.** Cross-checked directly against the route source
files, not guessed: `GET /health` and `GET /health/ready`
(`apps/api/app/api/routes/health.py`, unauthenticated liveness/readiness
probes by design - D054), `POST /auth/login`
(`apps/api/app/auth/routes/login.py` - there is no token to present yet),
and `POST /auth/password-reset/request` / `POST /auth/password-reset/confirm`
(`apps/api/app/auth/routes/password_reset.py`'s own module docstring: "Public,
unauthenticated password-reset endpoints", D063 - the whole point of the
request endpoint is that it must answer identically for a registered and an
unregistered email, which requires it to run with no caller identity at
all). `GET /auth/session` is NOT on this list - it requires
`get_current_user` like any other authenticated route (see
`apps/api/app/auth/routes/session.py`). FastAPI's own auto-generated
`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, and `/redoc` are Starlette
`Route`s, not `APIRoute`s, and are naturally excluded by this test's
`APIRoute`-only walk rather than needing to be named on the allowlist -
listed here anyway so a reader does not have to go check.
"""

from collections.abc import Iterable, Iterator

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.main import app

_PUBLIC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "/health",
        "/health/ready",
        "/auth/login",
        "/auth/password-reset/request",
        "/auth/password-reset/confirm",
    }
)
"""Every route that is INTENTIONALLY reachable with no caller identity at
all, and the one and only place this test's allowlist is declared. Adding a
path here must be a deliberate, reviewed decision - it is exactly the kind
of line an attacker would want silently added."""


def _iter_api_routes(routes: Iterable[object]) -> Iterator[APIRoute]:
    """Every real `APIRoute` reachable from `routes`, recursing through
    whatever wrapper this FastAPI version uses for `app.include_router(...)`.

    This FastAPI version (see `pyproject.toml`'s pinned floor vs. what is
    actually installed) wraps each included router in a `_IncludedRouter`
    that exposes the original `APIRouter` via `.original_router` rather than
    flattening its routes directly into `app.routes` - so a naive
    `isinstance(route, APIRoute)` filter over `app.routes` alone silently
    finds NOTHING (which would make this whole test vacuously pass every
    route it never inspected). This walk handles that wrapper by name where
    present, and falls back to a generic `.routes` attribute for a plain
    `APIRouter`/`Mount`, so it keeps working across ordinary FastAPI
    versions that flatten sub-routes directly.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _iter_api_routes(route.original_router.routes)
        elif hasattr(route, "routes"):
            yield from _iter_api_routes(route.routes)


def _dependency_call_names(dependant: Dependant, _seen: set[int] | None = None) -> set[str]:
    """Every `__name__` of every `call` in `dependant`'s FULL dependency
    tree - `dependant`'s own direct `.dependencies` plus each of THEIR
    `.dependencies`, recursively - which is where `get_current_user` shows
    up no matter how many layers of `require_permission(...)` /
    `require_broker_access(...)` wrap it.

    `_seen` guards against revisiting the same `Dependant` object twice
    (FastAPI can share cached dependant instances across a route's
    dependency graph); it is keyed on `id()` rather than the object itself
    because a `Dependant` is not hashable in a way this test wants to rely
    on.
    """
    seen = _seen if _seen is not None else set()
    names: set[str] = set()
    for dep in dependant.dependencies:
        if id(dep) in seen:
            continue
        seen.add(id(dep))
        call = getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", repr(call)))
        names |= _dependency_call_names(dep, seen)
    return names


def _all_api_routes() -> list[APIRoute]:
    routes = list(_iter_api_routes(app.routes))
    # A basic sanity floor so a future change to how FastAPI structures
    # `app.routes` (or a bug in `_iter_api_routes` above) that made this
    # walk find nothing would fail LOUDLY here, rather than this test
    # silently passing over zero routes and reporting perfect compliance.
    assert len(routes) >= 60, (
        f"expected at least 60 real APIRoutes, found {len(routes)} - "
        "the route-walk in this test may not match this FastAPI version's "
        "internal structure any more (see _iter_api_routes's docstring)."
    )
    return routes


def test_every_non_public_route_requires_get_current_user_somewhere_in_its_dependency_tree() -> (
    None
):
    """The core regression guard: for every mounted route not on the
    explicit public allowlist, `get_current_user` must appear somewhere in
    its resolved dependency tree. A route failing this either forgot
    `Depends(get_current_user)` / `Depends(require_permission(...))` /
    `Depends(require_broker_access(...))` entirely, at both the per-route
    AND the router level - or was deliberately added as public without
    updating `_PUBLIC_ALLOWLIST` above, which this test treats identically:
    either way, a human must look at it.
    """
    violations: list[str] = []
    for route in _all_api_routes():
        if route.path in _PUBLIC_ALLOWLIST:
            continue
        names = _dependency_call_names(route.dependant)
        if get_current_user.__name__ not in names:
            violations.append(f"{sorted(route.methods)} {route.path}")

    assert violations == [], (
        "Route(s) with NO authentication dependency found in their dependency "
        "tree, and not on the public allowlist:\n" + "\n".join(sorted(violations))
    )


def test_the_public_allowlist_is_exactly_the_documented_intentionally_public_routes() -> None:
    """Guards the allowlist itself in the OTHER direction: every path on it
    must actually exist as a real mounted route (a stale entry - naming a
    route that was renamed or removed - would silently stop protecting
    anything, since the loop above would just never visit it) and must
    genuinely have no `get_current_user` in its tree (if a path on the
    allowlist ever gained real authentication, it now belongs in the
    "protected" set instead, and leaving it on this list would mask that
    the requirement changed)."""
    paths_by_route = {route.path: route for route in _all_api_routes()}

    missing = _PUBLIC_ALLOWLIST - paths_by_route.keys()
    assert missing == set(), f"Allowlisted path(s) do not exist as a mounted route: {missing}"

    unexpectedly_authenticated = [
        path
        for path in _PUBLIC_ALLOWLIST
        if get_current_user.__name__ in _dependency_call_names(paths_by_route[path].dependant)
    ]
    assert unexpectedly_authenticated == [], (
        "Allowlisted public route(s) now require get_current_user - remove them from "
        f"_PUBLIC_ALLOWLIST: {unexpectedly_authenticated}"
    )


def test_every_permission_enum_member_is_referenced_somewhere_in_the_api_routes() -> None:
    """A softer, complementary check: every `Permission` value should be
    named by at least one route file's source, so a permission that exists
    in the enum but gates nothing (dead, or not yet wired up) is visible
    rather than silent. `SUBMIT_LIVE_TRADE` and
    `STRATEGY_APPROVE_LIVE_DEPLOYMENT` are checked IN-HANDLER (after the
    dependency graph resolves, see this module's own docstring) rather than
    via `Depends(...)`, so this check greps route source text rather than
    walking the dependency tree the other tests above use - it would not
    otherwise see either of them."""
    import pathlib

    from apps.api.app.auth.permissions import Permission

    routes_dir = pathlib.Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "api"
    auth_routes_dir = (
        pathlib.Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "auth"
    )
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for directory in (routes_dir, auth_routes_dir)
        for path in directory.rglob("*.py")
    )

    unreferenced = [
        member.name
        for member in Permission
        if f"Permission.{member.name}" not in source
    ]
    assert unreferenced == [], (
        f"Permission enum member(s) never referenced by any route: {unreferenced}"
    )
