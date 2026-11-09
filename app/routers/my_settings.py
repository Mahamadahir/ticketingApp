"""A person's own settings (phase 2B): which channels they want for which events. Anyone signed in,
agent or requester, and only their own."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlmodel import Session

from app.audit import log_action
from app.auth import get_current_user
from app.sessions import revoke_user_sessions
from app import emergency
from app.database import get_session
from app.models import User
from app.config import settings
from app.notification_settings import (
    CHANNELS,
    TEAMS,
    MAX_TEAMS_DELAY_MINUTES,
    InvalidDelay,
    choices_for,
    parse_teams_delay,
    save_choices,
    save_teams_delay,
)
from app.notifications import AGENT_ROLES, default_teams_delay
from app.settings_store import EMERGENCY_PASSWORD_MIN_LENGTH, count_setting
from app.teams_conversations import reference_for
from app.time_zones import clear_manual_zone, set_browser_zone, set_manual_zone, zone_choices, zone_for_user

router = APIRouter(prefix="/settings", tags=["settings"])


def _channels(session: Session, user: User) -> dict:
    """The channel labels, with Teams no longer marked "not connected yet" once sending to Teams is on, and called
    "Teams (direct message)" when the bot is on (it sends to the person's chat with the bot)."""
    channels = dict(CHANNELS)
    if settings.teams_bot_configured:
        # A person who has not installed the app is told so here, since the choice does nothing for them until they do.
        note = None if reference_for(session, user) else (
            "install the Teams app to get these" if not settings.teams_notifications_enabled
            else "install the Teams app to get these as messages; until then they go to the support channel"
        )
        channels[TEAMS] = ("Teams (direct message)", note)
    elif settings.teams_notifications_enabled:
        channels[TEAMS] = (CHANNELS[TEAMS][0], None)
    return channels


@router.get("/notifications", response_class=HTMLResponse)
def notification_settings(
    request: Request,
    saved: int = 0,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "my_notification_settings.html",
        {
            "user": user, "choices": choices_for(session, user), "channels": _channels(session, user), "saved": saved,
            "is_agent": user.role in AGENT_ROLES, "can_have_emergency_password": emergency.may_hold_one(user), "default_delay": default_teams_delay(session),
            "max_delay": MAX_TEAMS_DELAY_MINUTES,
            "zone": zone_for_user(session, user), "zone_choices": zone_choices(),
        },
    )


@router.post("/notifications")
async def save_notification_settings(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    form = await request.form()
    try:
        # Read first, so a bad delay saves nothing. Only agents are notified, so only they have one.
        delay = parse_teams_delay(str(form.get("teams_delay", ""))) if user.role in AGENT_ROLES else None
    except InvalidDelay as error:
        raise HTTPException(status_code=400, detail=str(error))
    save_choices(session, user, {str(value) for value in form.getlist("on")})
    if user.role in AGENT_ROLES:
        save_teams_delay(session, user, delay)
    session.commit()
    return RedirectResponse("/settings/notifications?saved=1", status_code=303)


@router.post("/time-zone/browser")
def report_browser_time_zone(
    time_zone: str = Form("", max_length=64),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The page tells us the browser's time zone (Intl.DateTimeFormat().resolvedOptions().timeZone) so
    times follow the person as they travel. A zone chosen by hand is left alone, and an unknown name
    is ignored. Answers with the zone now in force and whether it changed."""
    changed = set_browser_zone(user, time_zone)
    if changed:
        session.add(user)
        session.commit()
    return {"time_zone": zone_for_user(session, user), "source": user.time_zone_source, "changed": changed}


@router.post("/time-zone")
def save_time_zone(
    time_zone: str = Form("", max_length=64),
    clear: str = Form(""),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Choose a time zone by hand (it then wins over the browser), or clear the choice."""
    if clear:
        clear_manual_zone(user)
    elif not set_manual_zone(user, time_zone):
        raise HTTPException(status_code=400, detail="Choose a time zone from the list, for example Europe/London")
    session.add(user)
    session.commit()
    return RedirectResponse("/settings/notifications?saved=1", status_code=303)


@router.post("/sign-out-everywhere")
def sign_out_everywhere(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Ends every browser this person is signed in on, this one included (#157)."""
    ended = revoke_user_sessions(session, user.id)
    log_action(session, user.email, "user", user.id, "signed_out_everywhere", f"by themselves; {ended} ended")
    session.commit()
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)


def _emergency_page(request: Request, user: User, session: Session, first: bool = False, error: str = "", saved: bool = False, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "my_emergency_password.html",
        {
            "user": user, "first": first, "error": error, "saved": saved,
            "has_password": bool(user.emergency_password_hash), "set_at": user.emergency_password_set_at,
            "min_length": count_setting(session, EMERGENCY_PASSWORD_MIN_LENGTH),
        },
        status_code=status_code,
    )


@router.get("/emergency-password", response_class=HTMLResponse)
def emergency_password_page(
    request: Request, first: int = 0, saved: int = 0,
    user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """Set or change the emergency password (#164): what an agent signs in with while outage mode is on. Agents
    are sent here at Entra sign-in until they have one."""
    if not emergency.may_hold_one(user):
        raise HTTPException(status_code=403, detail="Emergency passwords are for agents.")
    return _emergency_page(request, user, session, first=bool(first), saved=bool(saved))


@router.post("/emergency-password")
def save_emergency_password(
    request: Request,
    password: str = Form(..., max_length=1000),
    confirm: str = Form(..., max_length=1000),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not emergency.may_hold_one(user):
        raise HTTPException(status_code=403, detail="Emergency passwords are for agents.")
    try:
        if password != confirm:
            raise emergency.PasswordRefused("The two passwords are not the same.")
        emergency.set_password(session, user, password)
    except emergency.PasswordRefused as refusal:
        session.rollback()
        return _emergency_page(request, user, session, error=str(refusal), status_code=400)
    session.commit()
    return RedirectResponse("/settings/emergency-password?saved=1", status_code=303)
