from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import NotAuthenticated, get_current_user
from app.visibility import NoTicketAccess
from app.config import settings, unsafe_setting_errors
from app.models import User, UserRole
from app.routers import admin, auth, health, portal, tickets, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    errors = unsafe_setting_errors(settings)
    if errors:
        raise RuntimeError("Refusing to start: " + "; ".join(errors))
    yield


app = FastAPI(title="IT Support Ticketing", lifespan=lifespan)
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
app.include_router(admin.router)
app.include_router(webhooks.router)


templates = Jinja2Templates(directory="app/templates")


@app.exception_handler(NoTicketAccess)
def handle_no_ticket_access(request: Request, exc: NoTicketAccess):
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
