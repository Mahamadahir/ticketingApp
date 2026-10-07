from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.audit import log_action
from app.auth import get_current_user
from app.database import get_session
from app.forms import MAX_MESSAGE_LENGTH
from app.models import Message, MessageDirection, Ticket, User
from app.pagination import paginate

router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["max_message_length"] = MAX_MESSAGE_LENGTH


def _get_own_ticket(session: Session, ticket_id: int, user: User) -> Ticket:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None or ticket.requester_email.lower() != user.email.lower():
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@router.get("", response_class=HTMLResponse)
def list_my_tickets(
    request: Request,
    page: int = 1,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    query = select(Ticket).where(Ticket.requester_email == user.email).order_by(Ticket.updated_at.desc())
    tickets_page = paginate(session, query, page, request.query_params)
    return templates.TemplateResponse(
        request, "portal_tickets_list.html", {"page": tickets_page, "user": user}
    )


@router.get("/{ticket_id}", response_class=HTMLResponse)
def view_my_ticket(
    ticket_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    ticket = _get_own_ticket(session, ticket_id, user)
    return templates.TemplateResponse(
        request, "portal_ticket_detail.html", {"ticket": ticket, "user": user}
    )


@router.post("/{ticket_id}/comment", response_class=HTMLResponse)
def add_comment(
    ticket_id: int,
    request: Request,
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    ticket = _get_own_ticket(session, ticket_id, user)

    message = Message(
        ticket_id=ticket.id,
        sender=user.display_name,
        body=body,
        direction=MessageDirection.INBOUND,
        author_user_id=user.id,
    )
    session.add(message)

    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    log_action(session, user.email, "ticket", ticket.id, "commented", body[:200])
    session.commit()
    session.refresh(ticket)

    return templates.TemplateResponse(
        request, "portal_ticket_detail.html", {"ticket": ticket, "user": user}
    )
