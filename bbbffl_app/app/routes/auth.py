"""Coach sign-in/sign-out browser routes (roadmap package 19, issue #74).

Deliberately minimal: a sign-in page, sign-in submission, sign-out, and a
landing/confirmation page proving which coach identity is authenticated.
No dashboard, weekly selection UI, or coach profile editing here -- those
are later roadmap packages (25 and beyond); see docs/coach-authentication.md.

This module owns every cookie read/write and CSRF check for the coach auth
flow, so no other route file needs to parse a session cookie or verify a
password directly (see app/auth.py's module docstring, "Suggested
implementation boundary"). `get_current_coach` is the one function another
route (e.g. `app/routes/public.py`, for a tiny "signed in as ..." nav link)
should import to learn who, if anyone, is authenticated.
"""

from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import InvalidCredentialsError
from app.auth_rate_limit import RateLimitedError
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

SESSION_COOKIE_NAME = "bbbffl_session"
CSRF_COOKIE_NAME = "bbbffl_csrf"
_CSRF_COOKIE_MAX_AGE_SECONDS = 3600


async def _parse_form(request: Request) -> dict[str, str]:
    """Minimal `application/x-www-form-urlencoded` body parser. Avoids
    requiring the `python-multipart` package Starlette's `Request.form()`
    needs for *any* form parsing -- this module's two forms (sign-in,
    sign-out) never need file uploads or multipart bodies, so a small,
    fully-understood parser is simpler than an extra dependency."""
    body = await request.body()
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


def _issue_csrf_token(request: Request) -> str:
    """A fresh CSRF token to embed in a form-rendering response's context
    *before* rendering (`_attach_csrf_cookie` then sets the matching cookie
    on the resulting response) -- see app/csrf.py's module docstring for
    the double-submit design this pair implements."""
    return issue_token(request.app.state.settings.session_secret)


def _attach_csrf_cookie(request: Request, response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=_CSRF_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=request.app.state.settings.is_production,
        samesite="lax",
    )


def _render_login(request: Request, *, error: str | None, email: str, status_code: int = 200):
    token = _issue_csrf_token(request)
    response = templates.TemplateResponse(
        request, "login.html", {"error": error, "email": email, "csrf_token": token}, status_code=status_code
    )
    _attach_csrf_cookie(request, response, token)
    return response


def _verify_csrf(request: Request, submitted: str) -> bool:
    secret = request.app.state.settings.session_secret
    return verify_token(secret, request.cookies.get(CSRF_COOKIE_NAME), submitted)


def get_current_coach(request: Request):
    """The authenticated `app.identity.Coach` for this request's session
    cookie, or None. The one place any other route should call to learn
    "who is signed in" -- see module docstring. Not type-hinted with
    `Coach` itself: route modules must not import `app.identity` directly
    (see `tests/test_architecture.py::
    test_routes_never_import_persistence_or_season_model_directly`) --
    `app.auth.AuthenticationService.resolve` (a AUTH-group module, which
    routes *are* allowed to import) already returns that type."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return request.app.state.auth_service.resolve(token)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if get_current_coach(request) is not None:
        return RedirectResponse("/account", status_code=303)
    return _render_login(request, error=None, email="")


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await _parse_form(request)
    email = form.get("email", "").strip()
    password = form.get("password", "")
    csrf_submitted = form.get("csrf_token", "")

    if not _verify_csrf(request, csrf_submitted):
        return _render_login(request, error="Your sign-in form expired. Please try again.", email=email, status_code=403)

    if not email or not password:
        return _render_login(request, error="Email and password are required.", email=email, status_code=400)

    auth_service = request.app.state.auth_service
    remote_addr = request.client.host if request.client else "unknown"
    existing_token = request.cookies.get(SESSION_COOKIE_NAME)

    try:
        result = auth_service.login(email, password, remote_addr=remote_addr, existing_token=existing_token)
    except RateLimitedError:
        return _render_login(
            request,
            error="Too many attempts. Please wait a few minutes and try again.",
            email=email,
            status_code=429,
        )
    except InvalidCredentialsError:
        return _render_login(request, error="Invalid email or password.", email=email, status_code=401)

    settings = request.app.state.settings
    response = RedirectResponse("/account", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        result.token,
        max_age=settings.session_lifetime_seconds,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
    )
    response.delete_cookie(CSRF_COOKIE_NAME)
    return response


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    coach = get_current_coach(request)
    if coach is None:
        return RedirectResponse("/login", status_code=303)
    token = _issue_csrf_token(request)
    response = templates.TemplateResponse(request, "account.html", {"coach": coach, "csrf_token": token})
    _attach_csrf_cookie(request, response, token)
    return response


@router.post("/logout")
async def logout(request: Request):
    form = await _parse_form(request)
    csrf_submitted = form.get("csrf_token", "")

    if not _verify_csrf(request, csrf_submitted):
        return RedirectResponse("/account", status_code=303)

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        request.app.state.auth_service.logout(token)

    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE_NAME)
    return response
