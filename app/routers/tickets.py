from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import Message, MessageDirection, Ticket, TicketStatus

router = APIRouter(prefix="/tickets", tags=["tickets"])
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
def list_tickets(request: Request, session: Session = Depends(get_session)):
    tickets = session.exec(select(Ticket).order_by(Ticket.updated_at.desc())).all()
    return templates.TemplateResponse(
        request, "tickets_list.html", {"tickets": tickets}
    )


@router.get("/{ticket_id}", response_class=HTMLResponse)
def view_ticket(ticket_id: int, request: Request, session: Session = Depends(get_session)):
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return templates.TemplateResponse(
        request, "ticket_detail.html", {"ticket": ticket}
    )


@router.post("/{ticket_id}/reply", response_class=HTMLResponse)
def reply_to_ticket(
    ticket_id: int,
    request: Request,
    body: str = Form(...),
    session: Session = Depends(get_session),
):
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")

    message = Message(
        ticket_id=ticket.id,
        sender="engineer",
        body=body,
        direction=MessageDirection.OUTBOUND,
    )
    session.add(message)

    ticket.status = TicketStatus.IN_PROGRESS
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    session.commit()
    session.refresh(ticket)

    # TODO: once Graph credentials are wired up, send this reply as an
    # actual email back to ticket.requester_email via graph_client.send_mail
    # so the requester sees it as a normal reply in their inbox.

    return templates.TemplateResponse(
        request, "ticket_detail.html", {"ticket": ticket}
    )
