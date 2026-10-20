"""Queuing email out of the shared mailbox (phase 3D). Nothing here commits, and nothing here sends:
the worker's mail job (app/mail.py) builds each email when it goes and sends it.

The places that record a reply, a notice, an approval request or a "ticket raised for you" call
these in the same transaction, so what was recorded and what is emailed cannot disagree. With
MAIL_SENDING off nothing is queued, so switching it on later does not send a backlog.

This module imports no other app module that sends or decides what may be emailed (messages.py
does that), so messages.py can call it without a cycle.
"""

from typing import Optional

from sqlmodel import Session

from app.config import settings
from app.models import Message, OutboundEmail, Ticket, TicketApproval, User

REPLY = "reply"  # an agent's email to the requester (counts as a first response)
NOTICE = "notice"  # an automatic message to the requester (an approval decision, say)
RAISED_FOR = "raised_for_you"  # "an agent raised a ticket for you"
APPROVAL = "approval"  # asks an approver to approve or decline

# Kinds that go to the requester without an agent having just written them. These are what the
# per-ticket cap counts; an agent's own reply never is.
AUTOMATIC_TO_REQUESTER = (NOTICE, RAISED_FOR)


def is_on() -> bool:
    return settings.mail_sending != "off"


def queue_message(session: Session, message: Message, ticket: Ticket) -> Optional[OutboundEmail]:
    """Queues the email for a public outbound message: an agent's reply (it has an author) or an
    automatic notice (it has none). One email per message, enforced by the database."""
    if not is_on():
        return None
    session.flush()  # the message needs its id
    row = OutboundEmail(
        kind=REPLY if message.author_user_id is not None else NOTICE,
        ticket_id=ticket.id,
        message_id=message.id,
        to_address=ticket.requester_email,
    )
    session.add(row)
    return row


def queue_raised_for(session: Session, ticket: Ticket) -> Optional[OutboundEmail]:
    """Queues "an agent raised a ticket for you". Whether the person is to be told is asked again
    when it is sent (raising.should_tell_requester), so a later opt-out wins."""
    if not is_on():
        return None
    session.flush()
    row = OutboundEmail(kind=RAISED_FOR, ticket_id=ticket.id, to_address=ticket.requester_email)
    session.add(row)
    return row


def queue_approval(session: Session, approval: TicketApproval, approver: User) -> Optional[OutboundEmail]:
    """Queues the email asking one approver. The link in it is made when it is sent."""
    if not is_on():
        return None
    session.flush()
    row = OutboundEmail(
        kind=APPROVAL, ticket_id=approval.ticket_id, approval_id=approval.id, to_address=approver.email
    )
    session.add(row)
    return row
