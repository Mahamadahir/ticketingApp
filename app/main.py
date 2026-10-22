from contextlib import asynccontextmanager
from html import escape

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from app.templating import is_htmx, templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.activity import ActivityMiddleware
from app.auth import NotAuthenticated, get_current_user
from app.visibility import NoTicketAccess
from app.config import settings, unsafe_setting_errors
from app.log_redaction import redact_approval_tokens
from app.telemetry import start_telemetry
from app.models import User, UserRole
from app.routers import action_links, admin, approvals, attachments, auth, dev_tools, feedback, health, my_settings, notifications, portal, tickets, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    errors = unsafe_setting_errors(settings)
    if errors:
        raise RuntimeError("Refusing to start: " + "; ".join(errors))
    yield


class AppStaticFiles(StaticFiles):
    """Static files, with long caching for vendor/: those files carry their version in the name."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.replace("\\", "/").startswith("vendor/") and path.endswith(".js") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _htmx_error(message: str, status_code: int) -> HTMLResponse:
    """A small message for a form that was submitted with htmx. htmx ignores 4xx
    responses unless told otherwise, so the response names its own target (the
    #flash area in base.html) and a script there lets it through. Without this a
    refused action (not triaged yet, bad address, no access) looks like a button
    that does nothing."""
    return HTMLResponse(
        f'<p class="error">{escape(message)}</p>',
        status_code=status_code,
        headers={"HX-Retarget": "#flash", "HX-Reswap": "innerHTML"},
    )


async def handle_http_exception(request: Request, exc: StarletteHTTPException):
    if is_htmx(request) and 400 <= exc.status_code < 500:
        return _htmx_error(str(exc.detail), exc.status_code)
    return await http_exception_handler(request, exc)


async def handle_validation_error(request: Request, exc: RequestValidationError):
    if is_htmx(request):
        return _htmx_error("Check what you entered: a field is missing, too long or not valid.", 422)
    return await request_validation_exception_handler(request, exc)


def handle_no_ticket_access(request: Request, exc: NoTicketAccess):
    if is_htmx(request):
        return _htmx_error("You don't have access to do that on this ticket.", 403)
    # The viewer is signed in (a route that raises this has already resolved
    # the user), so the page can show the normal navigation.
    return templates.TemplateResponse(request, "no_access.html", {"user": getattr(request.state, "user", None)}, status_code=403)


def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/auth/login")


def root(user: User = Depends(get_current_user)):
    if user.role == UserRole.END_USER:
        return RedirectResponse(url="/portal")
    return RedirectResponse(url="/tickets")


# Which routers each role serves. The public app is open to the internet, so it carries only what
# Microsoft and people off the VPN must reach (the Graph webhook, the emailed approval link) and
# /health; nothing that needs a signed-in agent. "all" is every route in one process, for local
# development and tests (it is refused outside dev, see unsafe_setting_errors).
PUBLIC_ROUTERS = [health.router, webhooks.router, approvals.router, action_links.router]
WEB_ROUTERS = [
    health.router, auth.router, tickets.router, tickets.reference_router, portal.router,
    notifications.router, my_settings.router, admin.router, dev_tools.router,
    attachments.router, feedback.router,
]
ROLE_ROUTERS = {
    "web": WEB_ROUTERS,
    "public": PUBLIC_ROUTERS,
    "all": WEB_ROUTERS + [webhooks.router, approvals.router, action_links.router],
    "worker": [health.router],  # the worker runs python -m app.worker and serves nothing
}


def build_app(role: str) -> FastAPI:
    app = FastAPI(title="IT Support Ticketing", lifespan=lifespan)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(NoTicketAccess, handle_no_ticket_access)
    app.add_exception_handler(NotAuthenticated, handle_not_authenticated)
    if role != "public":
        # Added first, so it runs inside the session middleware and can see who is signed in. Not on
        # the public app: anyone on the internet can POST to its routes, and each request would cost
        # a database write and a permanent row.
        app.add_middleware(ActivityMiddleware)
        # The public app has no sign-in, so no session cookie and no cookie key.
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret_key,
            https_only=not settings.is_dev,
            same_site="lax",
        )
    app.mount("/static", AppStaticFiles(directory="app/static"), name="static")
    for router in ROLE_ROUTERS[role]:
        if router is dev_tools.router and not settings.is_dev:
            continue  # the dev helper exists only in dev, not merely refuses outside it
        app.include_router(router)
    if role in ("web", "all"):
        app.add_api_route("/", root, methods=["GET"])
    return app


redact_approval_tokens()
start_telemetry()
# An unknown role is not served as anything wider than public: the start-up check refuses it with a
# clear message, and until then it gets the smallest set of routes.
app = build_app(settings.app_role if settings.app_role in ROLE_ROUTERS else "public")
