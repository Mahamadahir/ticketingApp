"""Everything about the messages on a ticket: emailing the requester, internal
notes, what the requester is allowed to see, and the agent timeline.

Rules that live here and nowhere else:
- Only requester_visible_messages may feed the portal or outbound mail. It
  returns PUBLIC messages only, so an internal note cannot reach either.
- A ticket cannot be emailed until it is triaged. Notes can be added any time.
- Which messages show on which ticket of a split request (_shared_conversation), the
  one thread subject (thread_subject) and the "About:" line (about_labels).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.followers import followers_of
from app.outbox import queue_message
from app.models import (
    FINISHED_STATUSES,
    Message,
    MessageDirection,
    MessageVisibility,
    Ticket,
    TicketEvent,
    TicketStatus,
    User,
)
from app.notifications import notify_reply
from app.sla import note_first_response
from app.ticket_service import NotTriaged
from app.visibility import Access, is_untriaged, ticket_access, visible_ticket_condition


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


class EmptyEmail(ValueError):
    """The agent tried to email the requester with nothing to say."""


class UnknownRecipient(ValueError):
    """The agent asked to copy an address that is not a follower of the ticket."""


def _copied_followers(session: Session, ticket: Ticket, cc_emails: Optional[list[str]]) -> list:
    """Who an email on this ticket copies: every follower, or only those the agent left
    ticked (internal followers are always copied). The one place this is worked out, so the
    preview and the email that is then sent cannot disagree."""
    requester = ticket.requester_email.lower()
    candidates = [f for f in followers_of(session, ticket) if f.email != requester]
    if cc_emails is None:
        return candidates
    wanted = {address.strip().lower() for address in cc_emails}
    unknown = wanted - {f.email for f in candidates}
    if unknown:
        raise UnknownRecipient(", ".join(sorted(unknown)))
    return [f for f in candidates if f.is_internal or f.email in wanted]


@dataclass
class EmailPreview:
    """What email_requester would send, for the "are you sure?" step. Nothing is recorded."""

    to: str
    cc: list[str]
    subject: str
    about: Optional[str]
    body: str
    status_after: Optional[TicketStatus] = None  # the status sending would set; None: left as it is


# The statuses an agent may pick when emailing the requester or adding a note. New is left only by
# triage, Awaiting approval only by the approvers, and Resolved and Closed only by the Resolve
# action (app/resolution.py), so none of those is offered.
STATUS_CHOICES = (
    TicketStatus.OPEN,
    TicketStatus.IN_PROGRESS,
    TicketStatus.AWAITING_USER,
    TicketStatus.WITH_SUPPLIER,
    TicketStatus.WITH_MSP,
)


class BadStatusChoice(Exception):
    """The status asked for cannot be set from an email or a note."""


def status_choices(session: Session, ticket: Ticket) -> list[TicketStatus]:
    """The statuses the Email user and Add note dropdowns offer for this ticket; empty means no
    dropdown and the status is left alone: an untriaged ticket, one waiting for approval, a
    finished one, or an original whose status follows its sub-tickets (app/splitting.py)."""
    from app.splitting import has_sub_tickets

    if not is_triaged(ticket) or ticket.status not in STATUS_CHOICES or has_sub_tickets(session, ticket):
        return []
    return list(STATUS_CHOICES)


def default_status_after_email(ticket: Ticket) -> TicketStatus:
    """Emailing the user normally means we are now waiting on them, so Open and In progress default
    to Awaiting user. With supplier, With MSP and Awaiting user stay as they are."""
    if ticket.status in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS):
        return TicketStatus.AWAITING_USER
    return ticket.status


def chosen_status(
    session: Session, ticket: Ticket, requested: Optional[str], default: TicketStatus
) -> Optional[TicketStatus]:
    """The status an email or note should move the ticket to, or None to leave it. requested is the
    dropdown's value ("" or None: not sent, so the default applies, which is how a request without the
    dropdown still behaves). Anything not offered for this ticket is refused."""
    choices = status_choices(session, ticket)
    requested = (requested or "").strip()
    if not requested:
        target = default if choices else ticket.status
    else:
        try:
            target = TicketStatus(requested)
        except ValueError:
            raise BadStatusChoice("That is not a status")
        if target != ticket.status and target not in choices:
            raise BadStatusChoice(f"The status cannot be set to {target.value.replace('_', ' ')} here")
    return None if target == ticket.status else target


def preview_email(
    session: Session,
    ticket: Ticket,
    body: str,
    cc_emails: Optional[list[str]] = None,
    next_status: Optional[str] = None,
) -> EmailPreview:
    """Checks an email exactly as email_requester does and returns what it would send."""
    if not is_triaged(ticket):
        raise NotTriaged()
    if not body.strip():
        raise EmptyEmail()
    chosen = _copied_followers(session, ticket, cc_emails)
    status_after = chosen_status(session, ticket, next_status, default_status_after_email(ticket))
    return EmailPreview(
        to=ticket.requester_email,
        cc=[f.email for f in chosen],
        subject=mail_subject(session, ticket),
        about=about_text(ticket) if in_a_split(session, ticket) else None,
        body=body,
        status_after=status_after,
    )


def in_a_split(session: Session, ticket: Ticket) -> bool:
    """Whether the ticket is a part of a split request, or the original of one."""
    return ticket.split_from_id is not None or session.exec(
        select(Ticket.id).where(Ticket.split_from_id == ticket.id).limit(1)
    ).first() is not None


def email_requester(
    session: Session, ticket: Ticket, author: User, body: str, cc_emails: Optional[list[str]] = None
) -> Message:
    """A public message from an agent to the requester. Records it and queues the email
    (app/outbox.py); the worker sends it (app/mail.py).

    Every follower is copied unless the agent unticks them for this email: pass
    cc_emails as the addresses to copy (None means all of them). Unticking does not
    remove anyone as a follower. Internal followers are agents and are always
    copied. The list is recorded on the message, so the timeline shows exactly who
    received it even if the followers change later."""
    if not is_triaged(ticket):
        raise NotTriaged()
    if not body.strip():
        raise EmptyEmail()

    chosen = _copied_followers(session, ticket, cc_emails)
    cc = ", ".join(f.email for f in chosen) or None
    note_first_response(ticket, datetime.now(timezone.utc))
    message = _record(
        session, ticket, sender=author.display_name, body=body, author_user_id=author.id,
        direction=MessageDirection.OUTBOUND, visibility=MessageVisibility.PUBLIC, cc=cc,
    )
    queue_message(session, message, ticket)
    return message


def record_notice(session: Session, ticket: Ticket, body: str, copy_followers: bool = False) -> Message:
    """An automatic public message to the requester (an approval decision, say). Not an agent's
    email, so it does not count as a first response. It is queued as an email too. Followers are
    copied only when copy_followers is set (an automatic resolution does), by the same rule as an
    agent's email."""
    cc = None
    if copy_followers:
        cc = ", ".join(f.email for f in _copied_followers(session, ticket, None)) or None
    message = _record(
        session, ticket, sender="IT Support (automatic)", body=body,
        direction=MessageDirection.OUTBOUND, visibility=MessageVisibility.PUBLIC, cc=cc,
    )
    queue_message(session, message, ticket)
    return message


def add_note(session: Session, ticket: Ticket, author: User, body: str) -> Message:
    """An internal note. Allowed at any stage and does not triage the ticket."""
    return _record(
        session, ticket, sender=author.display_name, body=body, author_user_id=author.id,
        direction=MessageDirection.INTERNAL, visibility=MessageVisibility.AGENTS_ONLY,
    )


def record_inbound(
    session: Session,
    ticket: Ticket,
    *,
    sender: str,
    body: str,
    author_user_id: Optional[int] = None,
    reply_by: Optional[str] = None,
) -> Message:
    """A message from the requester (portal comment, or mail once Graph is wired
    up). Always public: the requester wrote it and may see it.

    reply_by is the email of the person replying: pass it for a reply on an existing ticket and the
    assigned agent is notified (whatever the status). Leave it out for the message a ticket is
    created with, which tells nobody."""
    message = _record(
        session, ticket, sender=sender, body=body, author_user_id=author_user_id,
        direction=MessageDirection.INBOUND, visibility=MessageVisibility.PUBLIC,
    )
    if reply_by is not None:
        notify_reply(session, ticket, reply_by)
    return message


def _sub_tickets(session: Session, ticket: Ticket, viewer=None) -> list[Ticket]:
    """The sub-tickets of this ticket, oldest first. With a viewer (user, access context) only
    those they could open; without one, all of them (the requester's own conversation)."""
    query = select(Ticket).where(Ticket.split_from_id == ticket.id).order_by(Ticket.id)
    if viewer is not None:
        condition = visible_ticket_condition(*viewer)
        if condition is not None:
            query = query.where(condition)
    return list(session.exec(query).all())


def _shared_conversation(session: Session, ticket: Ticket, viewer=None) -> list[Ticket]:
    """The other tickets whose public messages also show on this one. One request split
    into parts is one conversation for the requester: the original's emails and the
    requester's replies show on every open sub-ticket, and a sub-ticket's messages show on
    the original. They are stored once and shown by this rule, never copied. Siblings do not
    see each other's, and internal notes never cross (callers take PUBLIC messages only).
    A finished sub-ticket no longer shows the original's conversation."""
    if ticket.split_from_id is not None:
        original = session.get(Ticket, ticket.split_from_id)
        if original is None or ticket.status in FINISHED_STATUSES:
            return []
        if viewer is not None and ticket_access(viewer[0], original, viewer[1]) < Access.READ:
            return []  # an agent sees the original's conversation only if their team can open it
        return [original]
    return _sub_tickets(session, ticket, viewer)


_PUBLIC = (
    Message.visibility == MessageVisibility.PUBLIC,
    Message.direction != MessageDirection.INTERNAL,
)


def requester_visible_messages(session: Session, ticket: Ticket) -> list[Message]:
    """The only messages the requester may see, in time order. Public only, and
    never an internal note even if one were wrongly marked public. A ticket that was
    split shows its family's shared conversation (see _shared_conversation)."""
    ids = [ticket.id] + [other.id for other in _shared_conversation(session, ticket)]
    return list(
        session.exec(
            select(Message).where(Message.ticket_id.in_(ids), *_PUBLIC).order_by(Message.created_at, Message.id)
        ).all()
    )


def thread_subject(session: Session, ticket: Ticket) -> str:
    """The subject every email in the family goes out under, so the requester's mail client
    keeps them in one conversation: the original's. Used when mail is sent (phase 3)."""
    if ticket.split_from_id is not None:
        original = session.get(Ticket, ticket.split_from_id)
        if original is not None:
            return original.subject
    return ticket.subject


def mail_subject(session: Session, ticket: Ticket) -> str:
    """The subject of every email about this ticket: "[INC-1042] Printer jam". The reference in
    brackets is what lets a reply be matched back to the ticket, so it is the family's: a part of a
    split request goes out under its original's reference and subject, one conversation for the
    requester. The preview shows exactly this."""
    root = ticket
    if ticket.split_from_id is not None:
        original = session.get(Ticket, ticket.split_from_id)
        if original is not None:
            root = original
    return f"[{root.reference}] {thread_subject(session, ticket)}"


def about_text(ticket: Ticket) -> str:
    """The "About:" line naming the part an email is about. The one place its wording lives."""
    return f"About: {ticket.reference} {ticket.subject}"


def about_labels(session: Session, messages: list[Message]) -> dict[int, str]:
    """For outgoing emails that belong to a split request, the line naming the part it is
    about ("About: SR-14 Replace keyboard"), keyed by message id. Emails on a ticket that
    was never split get none."""
    outbound = [m for m in messages if m.direction == MessageDirection.OUTBOUND]
    if not outbound:
        return {}
    tickets = {
        t.id: t
        for t in session.exec(select(Ticket).where(Ticket.id.in_({m.ticket_id for m in outbound}))).all()
    }
    originals = set(
        session.exec(
            select(Ticket.split_from_id).where(Ticket.split_from_id.in_(list(tickets))).distinct()
        ).all()
    )
    return {
        m.id: about_text(tickets[m.ticket_id])
        for m in outbound
        if tickets[m.ticket_id].split_from_id is not None or m.ticket_id in originals
    }


@dataclass
class TimelineItem:
    at: datetime
    message: Optional[Message] = None
    event: Optional[TicketEvent] = None
    # For a message from the requester's side: "requester" or "follower".
    sender_kind: Optional[str] = None
    # The other ticket this item belongs to, when it is shown here because the two are one
    # split request. None for the ticket's own items.
    source: Optional[Ticket] = None

    @property
    def kind(self) -> str:
        return "message" if self.message is not None else "event"


def agent_timeline(session: Session, ticket: Ticket, viewer=None) -> list[TimelineItem]:
    """Messages, notes and events in one time-ordered list, for agents only.

    A split request is one conversation (see _shared_conversation), so the timeline also
    shows the family's public messages, labelled with the ticket they belong to. On an
    original, a part's public messages and status changes show only for parts the viewer
    could open, so pass viewer=(user, access_context); with none, no part's items show.
    Internal notes never cross tickets."""
    messages = session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    events = session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)).all()
    other_items: list[TimelineItem] = []
    if viewer is not None:
        for other in _shared_conversation(session, ticket, viewer):
            for m in session.exec(select(Message).where(Message.ticket_id == other.id, *_PUBLIC)).all():
                other_items.append(TimelineItem(at=m.created_at, message=m, source=other))
            if ticket.split_from_id is None:
                # Progress of a part shows on its original; the original's own events do not
                # show on a part.
                for e in session.exec(
                    select(TicketEvent).where(TicketEvent.ticket_id == other.id, TicketEvent.event_type == "status_changed")
                ).all():
                    other_items.append(TimelineItem(at=e.created_at, event=e, source=other))
    requester = ticket.requester_email.lower()
    author_ids = {m.author_user_id for m in messages + [i.message for i in other_items if i.message] if m.author_user_id is not None}
    emails = (
        {u.id: u.email.lower() for u in session.exec(select(User).where(User.id.in_(author_ids))).all()}
        if author_ids
        else {}
    )

    def kind(message: Message) -> Optional[str]:
        if message.direction != MessageDirection.INBOUND:
            return None
        author = emails.get(message.author_user_id)
        if author is None and message.author_user_id is None:
            # Mail from someone without an account (app/inbound.py) is stored under their address.
            sender = message.sender.strip().lower()
            return "follower" if "@" in sender and " " not in sender and sender != requester else "requester"
        return "follower" if author is not None and author != requester else "requester"

    for item in other_items:
        if item.message is not None:
            item.sender_kind = kind(item.message)
    items = [TimelineItem(at=m.created_at, message=m, sender_kind=kind(m)) for m in messages]
    items += [TimelineItem(at=e.created_at, event=e) for e in events]
    items += other_items
    # Ties keep events after the message that caused them, and a stable order.
    return sorted(items, key=lambda i: (i.at, 0 if i.message else 1, (i.message or i.event).id or 0))
