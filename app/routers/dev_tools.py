"""Development helpers, served by the web app (never the public one) and refused outside dev."""

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlmodel import Session

from app.config import settings
from app.database import get_session
from app.messages import record_inbound
from app.models import TicketSource
from app.ticket_service import create_ticket

router = APIRouter(prefix="/webhooks", tags=["dev"])


@router.post("/dev/simulate-inbound")
def simulate_inbound_email(
    requester_email: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    session: Session = Depends(get_session),
):
    """Dev-only helper to create a ticket without Graph wired up yet, so the
    UI can be exercised end-to-end before the app registration exists.
    """
    if not settings.is_dev:
        raise HTTPException(status_code=404)

    # One transaction: the ticket, its first message and its created event
    # all exist or none do.
    ticket = create_ticket(session, subject=subject, requester_email=requester_email, source=TicketSource.EMAIL)
    record_inbound(session, ticket, sender=requester_email, body=body)
    session.commit()

    return {"number": ticket.number, "reference": ticket.reference}
