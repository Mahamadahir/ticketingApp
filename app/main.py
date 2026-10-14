from contextlib import asynccontextmanager
from html import escape

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.activity import ActivityMiddleware
from app.auth import NotAuthenticated, get_current_user
from app.visibility import NoTicketAccess
from app.config import settings, unsafe_setting_errors
from app.models import User, UserRole
from app.routers import admin, approvals, auth, health, portal, tickets, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    errors = unsafe_setting_errors(settings)
    if errors:
        raise RuntimeError("Refusing to start: " + "; ".join(errors))
    yield


app = FastAPI(title="IT Support Ticketing", lifespan=lifespan)
# Added first, so it runs inside the session middleware and can see who is signed in.
app.add_middleware(ActivityMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    https_only=not settings.is_dev,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(tickets.router)
app.include_router(tickets.reference_router)
app.include_router(portal.router)
app.include_router(approvals.router)
app.include_router(admin.router)
app.include_router(webhooks.router)


templates = Jinja2Templates(directory="app/templates")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


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


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException):
    if _is_htmx(request) and 400 <= exc.status_code < 500:
        return _htmx_error(str(exc.detail), exc.status_code)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    if _is_htmx(request):
        return _htmx_error("Check what you entered: a field is missing, too long or not valid.", 422)
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(NoTicketAccess)
def handle_no_ticket_access(request: Request, exc: NoTicketAccess):
    if _is_htmx(request):
        return _htmx_error("You don't have access to do that on this ticket.", 403)
    # The viewer is signed in (a route that raises this has already resolved
    # the user), so the page can show the normal navigation.
    return templates.TemplateResponse(request, "no_access.html", {"user": getattr(request.state, "user", None)}, status_code=403)


@app.exception_handler(NotAuthenticated)
def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/auth/login")


@app.get("/")
def root(user: User = Depends(get_current_user)):
    if user.role == UserRole.END_USER:
        return RedirectResponse(url="/portal")
    return RedirectResponse(url="/tickets")
