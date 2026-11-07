import logging
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlmodel import Session

from app.activity import (
    ACCOUNT_LOCKED,
    LOGIN_FAILED,
    LOGIN_SUCCESS,
    LOGOUT,
    SSO_FAILED,
    client_ip,
    record_activity,
)
from app import sessions
from app.audit import log_action
from app.breakglass import attempt_break_glass_login
from app.emergency import attempt_emergency_login, needs_one, start_emergency_session
from app.auth import acquire_token_by_auth_code, attempt_local_login, build_auth_url, get_or_create_user
from app.config import settings
from app.database import get_session
from app.identity import SsoRefused, resolve_sso_user
from app.models import User, UserRole
from app.notifications import notify_break_glass_sign_in
from app.routers.link_account import LINK_FLOW_KEY, complete_link_sign_in
from app.rate_limit import BREAK_GLASS, EMERGENCY, rate_limited
from sqlmodel import select

logger = logging.getLogger(__name__)

# Failures that match no account, or an account already locked: not stored.
NOT_WORTH_A_ROW = {"unknown_account", "account_locked"}
# Break-glass: also not an unreadable stored secret (it can be hit without limit until someone runs `reset`).
BREAK_GLASS_NOT_WORTH_A_ROW = NOT_WORTH_A_ROW | {"secret_unreadable"}

router = APIRouter(prefix="/auth", tags=["auth"])


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
    # Linking a second identity (My settings > Link another account) comes back to this same redirect URI, with
    # its own state, tied to the signed-in person. It is never a sign-in.
    link_flow = request.session.get(LINK_FLOW_KEY)
    if isinstance(link_flow, dict) and secrets.compare_digest(state.encode(), str(link_flow.get("state", "")).encode()):
        request.session.pop(LINK_FLOW_KEY, None)
        return complete_link_sign_in(request, session, code, link_flow)
    # Removed as soon as it is checked, so a state value works exactly once.
    expected_state = request.session.pop("oauth_state", None)
    # Compared as bytes: compare_digest raises on non-ASCII text, which would
    # turn a crafted value into a 500 instead of a 400.
    if not expected_state or not secrets.compare_digest(state.encode(), expected_state.encode()):
        # Only a sign-in that was really started (the session holds a state) is worth
        # a row; a cookie-less hit on this URL is noise anyone could send forever.
        if expected_state:
            _sso_failed(session, request, "invalid_state")
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    try:
        token_result = acquire_token_by_auth_code(code)
    except Exception:
        _sso_failed(session, request, "token_exchange_error")
        raise
    claims = token_result.get("id_token_claims", {})
    email = claims.get("preferred_username") or claims.get("email")
    display_name = claims.get("name", email)
    if not email:
        # Only the error code Microsoft gave is kept: its description can contain
        # account details, and the one-time code in the URL is never stored.
        error = token_result.get("error")
        _sso_failed(session, request, f"token_error: {error}" if error else "no_email_claim")
        raise HTTPException(status_code=400, detail="Sign-in did not return an email claim")

    try:
        user = resolve_sso_user(session, claims.get("oid"), email, display_name)
    except SsoRefused as refused:
        # The audit line the refusal wrote is kept, and so is the activity row.
        _sso_failed(session, request, refused.reason)
        return RedirectResponse("/auth/login?" + urlencode({"error": refused.message}), status_code=303)
    except Exception:
        session.rollback()
        _sso_failed(session, request, "account_error")
        raise
    if not user.is_active:
        return _refuse_inactive(session, request, user, "sso")
    sessions.start_session(session, request, user)
    record_activity(session, request, LOGIN_SUCCESS, actor_email=user.email, actor_user_id=user.id, detail="sso")
    session.commit()
    if needs_one(user):
        # An agent with no emergency password is asked for one (outage mode, #164); they can skip it, and are asked again next time.
        return RedirectResponse("/settings/emergency-password?first=1", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.post("/local-login")
def local_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    result = attempt_local_login(session, email, password)
    if result.user is None:
        # The person at the form sees one message whatever went wrong; the log keeps
        # the real reason (and never the password).
        if result.failure in NOT_WORTH_A_ROW:
            # Anyone can send these without limit (a bot guessing addresses, or
            # hammering a locked account), so they go to the application log only and
            # the table cannot be grown from outside. No typed text is kept.
            logger.warning("Rejected local sign-in (%s) from %s", result.failure, client_ip(request))
            return RedirectResponse("/auth/login?error=Invalid+email+or+password", status_code=303)
        record_activity(session, request, LOGIN_FAILED, actor_email=email, detail=result.failure)
        if result.locked_until is not None:
            record_activity(
                session, request, ACCOUNT_LOCKED, actor_email=email,
                detail=f"locked until {result.locked_until.isoformat()}",
            )
        session.commit()
        return RedirectResponse("/auth/login?error=Invalid+email+or+password", status_code=303)

    if not result.user.is_active:
        return _refuse_inactive(session, request, result.user, "local")
    sessions.start_session(session, request, result.user)
    record_activity(
        session, request, LOGIN_SUCCESS, actor_email=result.user.email, actor_user_id=result.user.id, detail="local"
    )
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/break-glass", response_class=HTMLResponse)
def break_glass_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "break_glass.html", {"error": error})


@router.post("/break-glass", dependencies=[Depends(rate_limited(BREAK_GLASS))])
def break_glass_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    code: str = Form(...),
    session: Session = Depends(get_session),
):
    """The emergency sign-in (app/breakglass.py): username, password and the current TOTP code. Every attempt at a
    real break-glass account (except on a locked one) is audited as `break_glass_sign_in`; a success also tells the super admins in the bell.
    Never keeps what was typed. Entra is not involved, so it works while Entra is down."""
    result = attempt_break_glass_login(session, username, password, code)
    refused = RedirectResponse("/auth/break-glass?error=Sign-in+failed", status_code=303)
    if result.user is None:
        if result.failure in BREAK_GLASS_NOT_WORTH_A_ROW:
            # Unknown names and locked accounts can be sent without limit: application log only, as for local accounts.
            logger.warning("Rejected break-glass sign-in (%s) from %s", result.failure, client_ip(request))
            return refused
        account = result.account
        log_action(session, account.email, "user", account.id, "break_glass_sign_in", f"failed ({result.failure}) from {client_ip(request)}")
        record_activity(session, request, LOGIN_FAILED, actor_email=account.email, detail=f"break_glass: {result.failure}")
        if result.locked_until is not None:
            record_activity(
                session, request, ACCOUNT_LOCKED, actor_email=account.email,
                detail=f"locked until {result.locked_until.isoformat()}",
            )
        session.commit()
        return refused

    user = result.user
    if not user.is_active:
        log_action(session, user.email, "user", user.id, "break_glass_sign_in", f"refused (inactive) from {client_ip(request)}")
        return _refuse_inactive(session, request, user, "break_glass")
    sessions.start_session(session, request, user)
    log_action(session, user.email, "user", user.id, "break_glass_sign_in", f"succeeded from {client_ip(request)}")
    record_activity(session, request, LOGIN_SUCCESS, actor_email=user.email, actor_user_id=user.id, detail="break_glass")
    for admin in session.exec(select(User).where(User.role == UserRole.SUPER_ADMIN, User.is_active)).all():
        notify_break_glass_sign_in(session, admin, user, client_ip(request))
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/emergency-login", dependencies=[Depends(rate_limited(EMERGENCY))])
def emergency_login(
    request: Request,
    upn: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    """The agents' sign-in while Entra is down (app/emergency.py): works only while outage mode is on. One message
    whatever went wrong. Attempts at a real agent's account are recorded in the activity log, marked "emergency";
    unknown names, a mode that is off and locked accounts can be sent without limit, so they go to the application
    log only. Never keeps what was typed."""
    result = attempt_emergency_login(session, upn, password)
    refused = RedirectResponse("/auth/login?error=Sign-in+failed", status_code=303)
    if result.user is None:
        if result.failure in NOT_WORTH_A_ROW | {"outage_off", "no_emergency_password"}:
            logger.warning("Rejected emergency sign-in (%s) from %s", result.failure, client_ip(request))
            return refused
        record_activity(session, request, LOGIN_FAILED, actor_email=result.account.email, detail=f"emergency: {result.failure}")
        if result.locked_until is not None:
            record_activity(
                session, request, ACCOUNT_LOCKED, actor_email=result.account.email,
                detail=f"locked until {result.locked_until.isoformat()}",
            )
        session.commit()
        return refused

    user = result.user
    if not user.is_active:
        return _refuse_inactive(session, request, user, "emergency")
    if start_emergency_session(session, request, user) is None:
        session.rollback()  # outage mode was switched off while the password was being checked
        logger.warning("Rejected emergency sign-in (outage_off) from %s", client_ip(request))
        return refused
    log_action(session, user.email, "user", user.id, "emergency_sign_in", f"outage mode, from {client_ip(request)}")
    record_activity(session, request, LOGIN_SUCCESS, actor_email=user.email, actor_user_id=user.id, detail="emergency")
    session.commit()
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
    if not user.is_active:
        return _refuse_inactive(session, request, user, "dev")
    sessions.start_session(session, request, user)
    record_activity(session, request, LOGIN_SUCCESS, actor_email=user.email, actor_user_id=user.id, detail="dev")
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)):
    # A POST, so another site cannot sign someone out with an image tag.
    row = sessions.current_row(session, request)
    user = session.get(User, row.user_id) if row is not None else None
    if user is not None:
        record_activity(session, request, LOGOUT, actor_email=user.email, actor_user_id=user.id)
    token = request.session.get(sessions.COOKIE_KEY)
    if isinstance(token, str):
        sessions.revoke_token(session, token)  # the server-side session ends, not only the cookie
    session.commit()
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)


def _refuse_inactive(session: Session, request: Request, user: User, how: str) -> RedirectResponse:
    """A deactivated person who got through the sign-in itself: no session is started."""
    log_action(session, user.email, "user", user.id, "sign_in_refused", f"inactive user ({how})")
    record_activity(
        session, request, LOGIN_FAILED, actor_email=user.email, actor_user_id=user.id, detail="inactive_account"
    )
    session.commit()
    return RedirectResponse("/auth/login?" + urlencode({"error": sessions.INACTIVE_MESSAGE}), status_code=303)


def _sso_failed(session: Session, request: Request, reason: str) -> None:
    record_activity(session, request, SSO_FAILED, detail=reason)
    session.commit()  # kept even though the request then fails
