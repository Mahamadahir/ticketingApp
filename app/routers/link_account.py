"""My settings > Link another account (phase 6E, #165): a signed-in person adds a second identity of their own.

They show it is theirs by signing in with it (Microsoft sign-in with the account picker, back through the normal
redirect URI with a state tied to them) or by typing a 6-digit code emailed to that address, and choose which
account is the main one. The rules are in app/linked_identities.py; this file only carries the forms."""

import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app import linked_identities as links
from app.auth import NotAuthenticated, acquire_token_by_auth_code, build_auth_url, get_current_user
from app.config import settings
from app.database import get_session
from app.models import LinkedIdentity, User, UserRole
from app.settings_store import LINK_CODE_MINUTES, count_setting
from app.templating import templates

router = APIRouter(prefix="/settings/link-account", tags=["settings"])

LINK_FLOW_KEY = "link_oauth"  # the session key holding the state of a sign-in made to link a second identity


def _back(**query: str) -> RedirectResponse:
    return RedirectResponse("/settings/link-account?" + urlencode(query), status_code=303)


def _outcome_redirect(request: Request, outcome: links.Outcome) -> RedirectResponse:
    if outcome.signed_out:
        # The signed-in account was merged into the other one: its sessions are over, sign in again as the main one.
        request.session.clear()
        return RedirectResponse(
            "/auth/login?" + urlencode({"error": "Your accounts are linked. Sign in again; you now use the main account."}),
            status_code=303,
        )
    return _back(done="applied" if outcome.applied else "requested")


def _refuse_break_glass(user: User) -> None:
    if user.is_break_glass:
        raise HTTPException(status_code=403, detail="A break-glass account is never linked.")


@router.get("", response_class=HTMLResponse)
def link_account_page(
    request: Request,
    address: str = "",
    sent: int = 0,
    done: str = "",
    error: str = "",
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    _refuse_break_glass(user)
    active = links.rows_for(session, links.ACTIVE, user=user)
    return templates.TemplateResponse(
        request,
        "my_link_account.html",
        {
            "user": user, "active": active,
            "pending": links.rows_for(session, links.PENDING, user=user), "address": address, "sent": bool(sent),
            "done": done, "error": error, "sso_configured": settings.sso_configured,
            "minutes": count_setting(session, LINK_CODE_MINUTES),
            "unlinkable": {row.link.id for row in active if links.may_unlink(session, user, row.link)},
        },
    )


@router.post("/sign-in")
def start_sign_in(request: Request, other_is_main: bool = Form(False), user: User = Depends(get_current_user)):
    """Starts the Microsoft sign-in with the second account. The state is tied to this person, and only the
    callback that finds it again in their own cookie completes the link."""
    _refuse_break_glass(user)
    if not settings.sso_configured:
        return _back(error="Microsoft sign-in is not set up here, so use an emailed code instead.")
    state = secrets.token_urlsafe(16)
    request.session[LINK_FLOW_KEY] = {"state": state, "user_id": user.id, "other_is_main": bool(other_is_main)}
    return RedirectResponse(build_auth_url(state, prompt="select_account"), status_code=303)


def complete_link_sign_in(request: Request, session: Session, code: str, flow: dict) -> RedirectResponse:
    """The Microsoft sign-in with the second account came back (app/routers/auth.py hands it over by its state).
    It proves the identity and starts no session."""
    try:
        me = get_current_user(request, session)
    except NotAuthenticated:
        return RedirectResponse("/auth/login", status_code=303)
    if me.id != flow.get("user_id"):
        return _back(error="That sign-in was started by someone else. Start again.")
    try:
        token = acquire_token_by_auth_code(code)
    except Exception:
        return _back(error="Microsoft sign-in did not complete. Try again.")
    claims = token.get("id_token_claims", {})
    address = claims.get("preferred_username") or claims.get("email")
    oid = claims.get("oid")
    if not address or not oid:
        return _back(error="Microsoft sign-in did not return an address and an object id. Ask an administrator.")
    try:
        outcome = links.self_service_link(
            session, me, address=address, oid=oid, method=links.SIGN_IN, other_is_main=bool(flow.get("other_is_main")),
        )
        session.commit()
    except links.LinkError as refusal:
        session.rollback()
        return _back(error=str(refusal))
    return _outcome_redirect(request, outcome)


@router.post("/code")
def send_code(
    address: str = Form(..., max_length=320),
    other_is_main: bool = Form(False),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    _refuse_break_glass(user)
    try:
        links.request_code(session, user, address, other_is_main)
    except links.LinkError as refusal:
        session.rollback()
        return _back(error=str(refusal), address=address)
    return _back(sent="1", address=links.normal_address(address))


@router.post("/verify")
def verify_code(
    request: Request,
    address: str = Form(..., max_length=320),
    code: str = Form(..., max_length=20),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    _refuse_break_glass(user)
    try:
        outcome = links.link_by_code(session, user, address, code)
        session.commit()
    except links.LinkError as refusal:
        session.rollback()  # a wrong try was already committed by check_code
        return _back(error=str(refusal), address=address, sent="1")
    return _outcome_redirect(request, outcome)


def _own_link(session: Session, user: User, link_id: int) -> LinkedIdentity:
    link = session.get(LinkedIdentity, link_id)
    if link is None or (user.id not in (link.main_user_id, link.requested_by_id) and user.role != UserRole.SUPER_ADMIN):
        raise HTTPException(status_code=404, detail="Link not found")
    return link


@router.post("/{link_id}/unlink")
def unlink(link_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    link = _own_link(session, user, link_id)
    try:
        links.unlink(session, link, user)
        session.commit()
    except links.LinkError as refusal:
        session.rollback()
        return _back(error=str(refusal))
    return _back(done="unlinked")


@router.post("/{link_id}/cancel")
def cancel(link_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    link = _own_link(session, user, link_id)
    try:
        links.decline(session, link, user)
        session.commit()
    except links.LinkError as refusal:
        session.rollback()
        return _back(error=str(refusal))
    return _back(done="cancelled")
