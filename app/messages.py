"""Everything about the messages on a ticket: emailing the requester, internal
notes, what the requester is allowed to see, and the agent timeline.

Two rules live here and nowhere else:
- Only requester_visible_messages may feed the portal or outbound mail. It
  returns PUBLIC messages only, so an internal note cannot reach either.
- A ticket cannot be emailed until it is triaged. Notes can be added any time.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models import (
    Message,
    MessageDirection,
    MessageVisibility,
    Ticket,
    TicketEvent,
    TicketStatus,
    User,
)


class NotTriaged(Exception):
    """Raised when someone tries to email the requester about an untriaged ticket."""


def is_triaged(ticket: Ticket) -> bool:
    # Triage is the ticket leaving New. Phase 1D replaces this with the explicit
    # triage action; keep every check going through here.
    return ticket.status != TicketStatus.NEW


def email_requester(session: Session, ticket: Ticket, author: User, body: str) -> Message:
    """A public message from an agent to the requester. Does not send mail yet
    (that arrives with Graph in phase 3); it records what will be sent."""
    if not is_triaged(ticket):
        raise NotTriaged()

    message = Message(
        ticket_id=ticket.id,
        sender=author.display_name,
        body=body,
        direction=MessageDirection.OUTBOUND,
        visibility=MessageVisibility.PUBLIC,
        author_user_id=author.id,
    )
    session.add(message)
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    return message


def add_note(session: Session, ticket: Ticket, author: User, body: str) -> Message:
    """An internal note. Allowed at any stage and does not triage the ticket."""
    message = Message(
        ticket_id=ticket.id,
        sender=author.display_name,
        body=body,
        direction=MessageDirection.INTERNAL,
        visibility=MessageVisibility.AGENTS_ONLY,
        author_user_id=author.id,
    )
    session.add(message)
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    return message


def requester_visible_messages(session: Session, ticket: Ticket) -> list[Message]:
    """The only messages the requester may see, in time order."""
    return list(
        session.exec(
            select(Message)
            .where(Message.ticket_id == ticket.id, Message.visibility == MessageVisibility.PUBLIC)
            .order_by(Message.created_at, Message.id)
        ).all()
    )


@dataclass
class TimelineItem:
    at: datetime
    message: Optional[Message] = None
    event: Optional[TicketEvent] = None

    @property
    def kind(self) -> str:
        return "message" if self.message is not None else "event"


def agent_timeline(session: Session, ticket: Ticket) -> list[TimelineItem]:
    """Messages, notes and events in one time-ordered list, for agents only."""
    messages = session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    events = session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)).all()
    items = [TimelineItem(at=m.created_at, message=m) for m in messages]
    items += [TimelineItem(at=e.created_at, event=e) for e in events]
    # Ties keep events after the message that caused them, and a stable order.
    return sorted(items, key=lambda i: (i.at, 0 if i.message else 1, (i.message or i.event).id or 0))
