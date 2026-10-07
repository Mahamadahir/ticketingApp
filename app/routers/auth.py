import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.auth import acquire_token_by_auth_code, authenticate_local, build_auth_url, get_or_create_user
from app.config import settings
from app.database import get_session

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"sso_configured": settings.sso_configured, "dev_login_enabled": settings.is_dev, "error": error},
    )


@router.get("/sso/start")
def sso_start(request: Request):
    if not settings.sso_configured:
        raise HTTPException(status_code=400, detail="SSO is not configured")

    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    return RedirectResponse(build_auth_url(state))


@router.get("/callback")
def callback(request: Request, code: str, state: str, session: Session = Depends(get_session)):
    if state != request.session.get("oauth_state"):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    token_result = acquire_token_by_auth_code(code)
    claims = token_result.get("id_token_claims", {})
    email = claims.get("preferred_username") or claims.get("email")
    display_name = claims.get("name", email)
    if not email:
        raise HTTPException(status_code=400, detail="Sign-in did not return an email claim")

    user = get_or_create_user(session, email, display_name)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/local-login")
def local_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    user = authenticate_local(session, email, password)
    if not user:
        return RedirectResponse("/auth/login?error=Invalid+email+or+password", status_code=303)

    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/dev-login", response_class=HTMLResponse)
def dev_login_form(request: Request):
    if not settings.is_dev:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "dev_login.html", {})


@router.post("/dev-login")
def dev_login_submit(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(...),
    session: Session = Depends(get_session),
):
    if not settings.is_dev:
        raise HTTPException(status_code=404)

    user = get_or_create_user(session, email, display_name)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)
