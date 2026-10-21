"""Development helpers, served by the web app (never the public one) and refused outside dev."""

from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlmodel import Session

from app.config import settings
from app.database import get_session
from app.inbound import IncomingMail, ingest_mail

router = APIRouter(prefix="/webhooks", tags=["dev"])


@router.post("/dev/simulate-inbound")
def simulate_inbound_email(
    requester_email: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    cc: str = Form(""),
    session: Session = Depends(get_session),
):
    """Dev-only helper to receive an email without Graph, so the UI can be exercised end-to-end. It runs
    the same intake as real mail (app/inbound.py ingest_mail): a subject with a [INC-1042] reference
    adds a reply to that ticket, anything else opens a ticket, and the copied addresses (comma-separated)
    become followers. Only the fetch from Graph is skipped.
    """
    if not settings.is_dev:
        raise HTTPException(status_code=404)

    mail = IncomingMail(
        graph_message_id=f"dev-{uuid4()}",
        sender=requester_email.strip().lower(),
        subject=subject,
        body=body,
        cc=[a.strip().lower() for a in cc.split(",") if a.strip()],
    )
    # One transaction: the ticket, its first message and its created event
    # all exist or none do.
    result = ingest_mail(session, mail)
    session.commit()

    if result.ticket is None:
        return {"outcome": result.outcome}
    return {"outcome": result.outcome, "number": result.ticket.number, "reference": result.ticket.reference}
