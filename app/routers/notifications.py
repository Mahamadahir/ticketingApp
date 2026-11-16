"""The bell: a person's notifications (phase 2A; everyone since #122 and #130). Own notifications only."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import is_htmx, templates
from sqlmodel import Session, select

from app.auth import get_current_user, get_current_user_passively
from app.database import get_session
from app.models import Notification, Ticket, User
from app.notifications import PORTAL_KINDS, bell_references, bell_tickets, notification_text, count_unread, mark_all_read, mark_read, visible_to
from app.pagination import paginate

router = APIRouter(prefix="/notifications", tags=["notifications"])

# Every signed-in person: a requester, follower or approver who is not an agent has a bell too, for their own tickets
# and requests waiting for their vote.
signed_in = get_current_user


def newest_first(session: Session, user: User):
    """What the page and the bell dropdown both list: this person's visible notifications, newest first."""
    return (
        select(Notification)
        .where(visible_to(session, user))
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )


def texts_of(session: Session, notifications) -> dict[int, str]:
    """The wording of each, by notification id, with the ticket's current reference filled in."""
    references = bell_references(session, notifications)
    return {n.id: notification_text(n, references.get(n.ticket_id)) for n in notifications}


@router.get("", response_class=HTMLResponse)
def list_notifications(
    request: Request,
    page: int = 1,
    user: User = Depends(signed_in),
    session: Session = Depends(get_session),
):
    notifications = paginate(session, newest_first(session, user), page, request.query_params)
    tickets = bell_tickets(session, user, notifications.items)
    texts = texts_of(session, notifications.items)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {"page": notifications, "tickets": tickets, "texts": texts, "unread": count_unread(session, user), "user": user, "portal_kinds": PORTAL_KINDS},
    )


BELL_ITEMS = 5  # what the bell's dropdown shows; the rest are on /notifications (View more)


@router.get("/latest", response_class=HTMLResponse)
def latest_notifications(
    request: Request,
    user: User = Depends(signed_in),
    session: Session = Depends(get_session),
):
    """The bell dropdown's list (htmx, loaded when it opens): the latest few this person can see, same rules and
    wording as the page. Reading it marks nothing read; a row's own POST does."""
    if not is_htmx(request):
        return RedirectResponse("/notifications", status_code=303)  # only the dropdown's rows, never a page of their own
    latest = session.exec(newest_first(session, user).limit(BELL_ITEMS)).all()
    return templates.TemplateResponse(
        request, "notifications_latest.html", {"items": latest, "texts": texts_of(session, latest)}, headers={"Cache-Control": "no-store"}
    )


@router.get("/count", response_class=HTMLResponse)
def unread_count(
    user: User = Depends(get_current_user_passively),  # the poll is not the person being active (app/auth.py)
    session: Session = Depends(get_session),
):
    """The bell's label, polled by the page every 30 seconds."""
    unread = count_unread(session, user)
    return HTMLResponse(f"Notifications ({unread})" if unread else "Notifications", headers={"Cache-Control": "no-store"})


@router.post("/read-all")
def read_all(
    user: User = Depends(signed_in),
    session: Session = Depends(get_session),
):
    mark_all_read(session, user)
    session.commit()
    return RedirectResponse("/notifications", status_code=303)


@router.post("/{notification_id}/open")
def open_notification(
    notification_id: int,
    user: User = Depends(signed_in),
    session: Session = Depends(get_session),
):
    """Marks one read and goes to its ticket. A POST, so a link scanner or a prefetch cannot read it away."""
    notification = mark_read(session, user, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    ticket = bell_tickets(session, user, [notification]).get(notification.ticket_id)
    destination = f"/tickets/{ticket.number}" if ticket else "/notifications"
    if notification.kind in PORTAL_KINDS and notification.ticket_id:
        asked_about = ticket or session.get(Ticket, notification.ticket_id)
        if asked_about is not None:
            destination = f"/portal/{asked_about.number}"  # where an approver votes and a requester or follower reads, whether or not they can open it as an agent
    session.commit()
    return RedirectResponse(destination, status_code=303)