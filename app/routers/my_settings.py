"""A person's own settings (phase 2B): which channels they want for which events. Anyone signed in,
agent or requester, and only their own."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlmodel import Session

from app.auth import get_current_user
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
from app.time_zones import clear_manual_zone, set_browser_zone, set_manual_zone, zone_choices, zone_for_user

router = APIRouter(prefix="/settings", tags=["settings"])


def _channels() -> dict:
    """The channel labels, with Teams no longer marked "not connected yet" once sending to Teams is on."""
    channels = dict(CHANNELS)
    if settings.teams_notifications_enabled:
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
            "user": user, "choices": choices_for(session, user), "channels": _channels(), "saved": saved,
            "is_agent": user.role in AGENT_ROLES, "default_delay": default_teams_delay(session),
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
