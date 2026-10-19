"""A person's own settings (phase 2B): which channels they want for which events. Anyone signed in,
agent or requester, and only their own."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.auth import get_current_user
from app.database import get_session
from app.models import User
from app.notification_settings import CHANNELS, choices_for, save_choices

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="app/templates")


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
        {"user": user, "choices": choices_for(session, user), "channels": CHANNELS, "saved": saved},
    )


@router.post("/notifications")
async def save_notification_settings(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    form = await request.form()
    save_choices(session, user, {str(value) for value in form.getlist("on")})
    session.commit()
    return RedirectResponse("/settings/notifications?saved=1", status_code=303)
