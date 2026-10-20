"""A person's own settings (phase 2B): which channels they want for which events. Anyone signed in,
agent or requester, and only their own."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="app/templates")


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
