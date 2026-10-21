"""The bell: an agent's notifications (phase 2A). Own notifications only."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlmodel import Session, select

from app.auth import require_roles
from app.database import get_session
from app.models import Notification, User
from app.notifications import AGENT_ROLES, bell_tickets, count_unread, mark_all_read, mark_read, visible_to
from app.pagination import paginate

router = APIRouter(prefix="/notifications", tags=["notifications"])

agent_only = require_roles(*AGENT_ROLES)


@router.get("", response_class=HTMLResponse)
def list_notifications(
    request: Request,
    page: int = 1,
    user: User = Depends(agent_only),
    session: Session = Depends(get_session),
):
    query = (
        select(Notification)
        .where(visible_to(session, user))
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )
    notifications = paginate(session, query, page, request.query_params)
    tickets = bell_tickets(session, user, notifications.items)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {"page": notifications, "tickets": tickets, "unread": count_unread(session, user), "user": user},
    )


@router.get("/count", response_class=HTMLResponse)
def unread_count(
    user: User = Depends(agent_only),
    session: Session = Depends(get_session),
):
    """The bell's label, polled by the page every 30 seconds."""
    unread = count_unread(session, user)
    return HTMLResponse(f"Notifications ({unread})" if unread else "Notifications", headers={"Cache-Control": "no-store"})


@router.post("/read-all")
def read_all(
    user: User = Depends(agent_only),
    session: Session = Depends(get_session),
):
    mark_all_read(session, user)
    session.commit()
    return RedirectResponse("/notifications", status_code=303)


@router.post("/{notification_id}/open")
def open_notification(
    notification_id: int,
    user: User = Depends(agent_only),
    session: Session = Depends(get_session),
):
    """Marks one read and goes to its ticket. A POST, so a link scanner or a prefetch cannot read it away."""
    notification = mark_read(session, user, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    ticket = bell_tickets(session, user, [notification]).get(notification.ticket_id)
    session.commit()
    return RedirectResponse(f"/tickets/{ticket.number}" if ticket else "/notifications", status_code=303)