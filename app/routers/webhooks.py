import hmac

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from sqlmodel import Session

from app.config import settings
from app.database import get_session
from app.models import Message, MessageDirection, TicketSource
from app.ticket_service import create_ticket

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

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Unexpected payload")

    for notification in payload.get("value", []):
        client_state = notification.get("clientState") if isinstance(notification, dict) else None
        # Constant-time compare, and anything not a string never matches.
        if not isinstance(client_state, str) or not hmac.compare_digest(
            client_state.encode(), settings.graph_webhook_client_state.encode()
        ):
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
    if not settings.is_dev:
        raise HTTPException(status_code=404)

    # One transaction: the ticket, its first message and its created event
    # all exist or none do.
    ticket = create_ticket(session, subject=subject, requester_email=requester_email, source=TicketSource.EMAIL)
    session.add(
        Message(
            ticket_id=ticket.id,
            sender=requester_email,
            body=body,
            direction=MessageDirection.INBOUND,
        )
    )
    session.commit()

    return {"ticket_id": ticket.id, "reference": ticket.reference}
