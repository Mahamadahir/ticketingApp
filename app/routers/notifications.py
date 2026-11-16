"""The bell: a person's notifications (phase 2A; everyone since #122 and #130). Own notifications only."""

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import is_htmx, templates
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth import get_current_user, get_current_user_passively
from app.database import get_session
from app.models import Notification, Ticket, User
from app.notifications import (
    PORTAL_KINDS,
    batch_moment,
    batch_number,
    bell_references,
    bell_tickets,
    clear_read,
    count_in_batch,
    count_unread,
    dismiss,
    in_undo_window,
    mark_all_read,
    mark_read,
    notification_text,
    undo_clear_read,
    undo_dismiss,
    visible_to,
)
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


def open_label(notification: Notification, tickets: dict) -> str:
    """The page's button for a row: Open when it goes somewhere, else Mark read."""
    return "Open" if tickets.get(notification.ticket_id) or notification.kind in PORTAL_KINDS else "Mark read"


def count_read(session: Session, user: User) -> int:
    """How many visible notifications Clear all read would dismiss."""
    return session.exec(
        select(func.count()).select_from(Notification).where(visible_to(session, user), Notification.read_at.is_not(None))
    ).one()


@router.get("", response_class=HTMLResponse)
def list_notifications(
    request: Request,
    page: int = 1,
    dismissed: Optional[int] = Query(None, ge=1, le=2**31 - 1),
    cleared: Optional[int] = Query(None, ge=0, le=2**62),
    restored: Optional[int] = Query(None, ge=0, le=10**6),
    user: User = Depends(signed_in),
    session: Session = Depends(get_session),
):
    paging = {k: v for k, v in request.query_params.items() if k not in ("dismissed", "cleared", "restored")}  # a banner is for one view, not for every page
    notifications = paginate(session, newest_first(session, user), page, paging)
    tickets = bell_tickets(session, user, notifications.items)
    texts = texts_of(session, notifications.items)
    # What a dismiss, a Clear all read or an Undo just did (the query says which; the data says whether it still holds, so
    # a made-up address shows nothing). The Undo offered is the one the keep-dismissed window still allows.
    undo_one = undo_text = None
    if dismissed is not None:
        row = session.exec(select(Notification).where(Notification.id == dismissed, visible_to(session, user, include_dismissed=True))).first()
        if row is not None and in_undo_window(session, row):
            undo_one, undo_text = row, texts_of(session, [row])[row.id]
    undo_batch = batch_count = None
    moment = batch_moment(cleared) if cleared else None
    if moment is not None:
        found = count_in_batch(session, user, moment)
        if found:
            undo_batch, batch_count = cleared, found
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "page": notifications, "tickets": tickets, "texts": texts, "unread": count_unread(session, user), "user": user,
            "portal_kinds": PORTAL_KINDS, "read_count": count_read(session, user), "undo_one": undo_one, "undo_text": undo_text,
            "undo_batch": undo_batch, "batch_count": batch_count, "restored": restored, "nothing_to_clear": cleared == 0,
            "open_label": open_label,
        },
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
        request, "notifications_latest.html", {"items": latest, "texts": texts_of(session, latest), "where": "bell"}, headers={"Cache-Control": "no-store"}
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


def _fragment(request: Request, template: str, context: dict) -> HTMLResponse:
    """What an htmx dismiss or undo swaps in for the row: just the row, and a nudge for the bell to recount."""
    return templates.TemplateResponse(request, template, context, headers={"HX-Trigger": "bell-count", "Cache-Control": "no-store"})


def _where(origin: str) -> str:
    return "bell" if origin == "bell" else "page"


@router.post("/clear-read")
def clear_all_read(
    user: User = Depends(signed_in),
    session: Session = Depends(get_session),
):
    """Clear all read (#239): dismisses this person's read notifications, all in one batch that Undo restores."""
    moment, count = clear_read(session, user)
    session.commit()
    return RedirectResponse(f"/notifications?cleared={batch_number(moment) if count else 0}", status_code=303)


@router.post("/undo-clear")
def undo_clear_all_read(
    batch: int = Form(..., ge=0, le=2**62),
    user: User = Depends(signed_in),
    session: Session = Depends(get_session),
):
    """Undo for one Clear all read: restores exactly the notifications that action dismissed, while the window allows."""
    restored = undo_clear_read(session, user, batch_moment(batch))
    session.commit()
    return RedirectResponse(f"/notifications?restored={restored}", status_code=303)


@router.post("/{notification_id}/dismiss")
def dismiss_notification(
    request: Request,
    notification_id: int = Path(..., ge=1, le=2**31 - 1),
    origin: str = Form("page", alias="from"),
    user: User = Depends(signed_in),
    session: Session = Depends(get_session),
):
    """Dismisses one of this person's own notifications (and marks it read). A POST, never a GET. Without script it goes
    back to the page with an Undo; with htmx the row is replaced by "Dismissed. Undo"."""
    notification = dismiss(session, user, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    session.commit()
    if not is_htmx(request):
        return RedirectResponse(f"/notifications?dismissed={notification.id}", status_code=303)
    return _fragment(request, "_notification_undo.html", {"n": notification, "text": texts_of(session, [notification])[notification.id], "where": _where(origin)})


@router.post("/{notification_id}/undo")
def undo_notification(
    request: Request,
    notification_id: int = Path(..., ge=1, le=2**31 - 1),
    origin: str = Form("page", alias="from"),
    user: User = Depends(signed_in),
    session: Session = Depends(get_session),
):
    """Brings a dismissed notification back (it stays read). 404 when it is not theirs or the purge job has deleted it;
    when the keep-dismissed window has passed but the purge has not run yet there is nothing to restore either."""
    notification = undo_dismiss(session, user, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    session.commit()
    restored = notification.dismissed_at is None
    if not is_htmx(request):
        return RedirectResponse(f"/notifications?restored={1 if restored else 0}", status_code=303)
    context = {"n": notification, "text": texts_of(session, [notification])[notification.id], "where": _where(origin)}
    if not restored:
        return _fragment(request, "_notification_undo.html", {**context, "expired": True})
    tickets = bell_tickets(session, user, [notification])
    return _fragment(request, "_notification_item.html", {**context, "label": open_label(notification, tickets)})
