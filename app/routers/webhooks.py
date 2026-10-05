from fastapi import APIRouter, Depends, Form, Request, Response
from sqlmodel import Session

from app.config import settings
from app.database import get_session
from app.models import Message, MessageDirection, Ticket

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/graph/notifications")
async def graph_notifications(request: Request, session: Session = Depends(get_session)):
    """Receives Graph change notifications for new mail in the shared
    mailbox. Graph validates a new subscription by POSTing here with a
    validationToken query param that must be echoed back as plain text.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return Response(content=validation_token, media_type="text/plain")

    payload = await request.json()

    for notification in payload.get("value", []):
        if notification.get("clientState") != settings.graph_webhook_client_state:
            continue

        # TODO: notification only carries the message ID, not content.
        # Call graph_client.get_message(notification["resourceData"]["id"])
        # to fetch subject/sender/body, then either attach to an existing
        # ticket (match on graph_conversation_id) or create a new one.

    return {"status": "accepted"}


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
    ticket = Ticket(subject=subject, requester_email=requester_email)
    session.add(ticket)
    session.commit()
    session.refresh(ticket)

    message = Message(
        ticket_id=ticket.id,
        sender=requester_email,
        body=body,
        direction=MessageDirection.INBOUND,
    )
    session.add(message)
    session.commit()

    return {"ticket_id": ticket.id}
