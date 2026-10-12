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

from app.followers import followers_of
from app.models import (
    Message,
    MessageDirection,
    MessageVisibility,
    Ticket,
    TicketEvent,
    User,
)
from app.ticket_service import NotTriaged
from app.visibility import is_untriaged


def is_triaged(ticket: Ticket) -> bool:
    # One definition of "untriaged", shared with access control (visibility.py),
    # so the two cannot disagree. A ticket is triaged once someone saves the triage
    # panel (triaged_at); the per-inbox routing change narrows it in the same place.
    return not is_untriaged(ticket)


def _record(
    session: Session,
    ticket: Ticket,
    *,
    sender: str,
    body: str,
    direction: MessageDirection,
    visibility: MessageVisibility,
    author_user_id: Optional[int] = None,
    cc: Optional[str] = None,
) -> Message:
    """The one place a Message is built, so the invariants cannot drift: an
    internal note is always agents-only, and any new message touches the ticket."""
    if direction == MessageDirection.INTERNAL and visibility != MessageVisibility.AGENTS_ONLY:
        raise ValueError("An internal message must be agents-only")

    message = Message(
        ticket_id=ticket.id,
        sender=sender,
        body=body,
        direction=direction,
        visibility=visibility,
        author_user_id=author_user_id,
        cc=cc,
    )
    session.add(message)
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    return message


class UnknownRecipient(ValueError):
    """The agent asked to copy an address that is not a follower of the ticket."""


def email_requester(
    session: Session, ticket: Ticket, author: User, body: str, cc_emails: Optional[list[str]] = None
) -> Message:
    """A public message from an agent to the requester. Does not send mail yet
    (that arrives with Graph in phase 3); it records what will be sent.

    Every follower is copied unless the agent unticks them for this email: pass
    cc_emails as the addresses to copy (None means all of them). Unticking does not
    remove anyone as a follower. Internal followers are agents and are always
    copied. The list is recorded on the message, so the timeline shows exactly who
    received it even if the followers change later."""
    if not is_triaged(ticket):
        raise NotTriaged()

    requester = ticket.requester_email.lower()
    candidates = [f for f in followers_of(session, ticket) if f.email != requester]
    if cc_emails is None:
        chosen = candidates
    else:
        wanted = {address.strip().lower() for address in cc_emails}
        unknown = wanted - {f.email for f in candidates}
        if unknown:
            raise UnknownRecipient(", ".join(sorted(unknown)))
        chosen = [f for f in candidates if f.is_internal or f.email in wanted]
    cc = ", ".join(f.email for f in chosen) or None
    return _record(
        session, ticket, sender=author.display_name, body=body, author_user_id=author.id,
        direction=MessageDirection.OUTBOUND, visibility=MessageVisibility.PUBLIC, cc=cc,
    )


def add_note(session: Session, ticket: Ticket, author: User, body: str) -> Message:
    """An internal note. Allowed at any stage and does not triage the ticket."""
    return _record(
        session, ticket, sender=author.display_name, body=body, author_user_id=author.id,
        direction=MessageDirection.INTERNAL, visibility=MessageVisibility.AGENTS_ONLY,
    )


def record_inbound(
    session: Session, ticket: Ticket, *, sender: str, body: str, author_user_id: Optional[int] = None
) -> Message:
    """A message from the requester (portal comment, or mail once Graph is wired
    up). Always public: the requester wrote it and may see it."""
    return _record(
        session, ticket, sender=sender, body=body, author_user_id=author_user_id,
        direction=MessageDirection.INBOUND, visibility=MessageVisibility.PUBLIC,
    )


def requester_visible_messages(session: Session, ticket: Ticket) -> list[Message]:
    """The only messages the requester may see, in time order. Public only, and
    never an internal note even if one were wrongly marked public."""
    return list(
        session.exec(
            select(Message)
            .where(
                Message.ticket_id == ticket.id,
                Message.visibility == MessageVisibility.PUBLIC,
                Message.direction != MessageDirection.INTERNAL,
            )
            .order_by(Message.created_at, Message.id)
        ).all()
    )

@dataclass
class TimelineItem:
    at: datetime
    message: Optional[Message] = None
    event: Optional[TicketEvent] = None
    # For a message from the requester's side: "requester" or "follower".
    sender_kind: Optional[str] = None

    @property
    def kind(self) -> str:
        return "message" if self.message is not None else "event"


def agent_timeline(session: Session, ticket: Ticket) -> list[TimelineItem]:
    """Messages, notes and events in one time-ordered list, for agents only."""
    messages = session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    events = session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)).all()
    requester = ticket.requester_email.lower()
    author_ids = {m.author_user_id for m in messages if m.author_user_id is not None}
    emails = (
        {u.id: u.email.lower() for u in session.exec(select(User).where(User.id.in_(author_ids))).all()}
        if author_ids
        else {}
    )

    def kind(message: Message) -> Optional[str]:
        if message.direction != MessageDirection.INBOUND:
            return None
        author = emails.get(message.author_user_id)
        return "follower" if author is not None and author != requester else "requester"

    items = [TimelineItem(at=m.created_at, message=m, sender_kind=kind(m)) for m in messages]
    items += [TimelineItem(at=e.created_at, event=e) for e in events]
    # Ties keep events after the message that caused them, and a stable order.
    return sorted(items, key=lambda i: (i.at, 0 if i.message else 1, (i.message or i.event).id or 0))
