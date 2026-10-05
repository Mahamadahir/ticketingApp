from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import NotAuthenticated, get_current_user
from app.config import settings
from app.database import create_db_and_tables
from app.models import User, UserRole
from app.routers import admin, auth, portal, tickets, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="IT Support Ticketing", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth.router)
app.include_router(tickets.router)
app.include_router(portal.router)
app.include_router(admin.router)
app.include_router(webhooks.router)


@app.exception_handler(NotAuthenticated)
def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/auth/login")


@app.get("/")
def root(user: User = Depends(get_current_user)):
    if user.role == UserRole.END_USER:
        return RedirectResponse(url="/portal")
    return RedirectResponse(url="/tickets")
