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

from app.attachments import NewFile, store_attachment
from app.followers import copyable_followers
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
from app.request_questions import answer_rows
from app.notifications import notify_agent_emailed, notify_reply
from app.sla import note_first_response
from app.reply_channel import AGENT, EMAIL, MESSAGE_CHANNELS, PORTAL, SYSTEM, TEAMS_CHANNEL, queue_reply, route_for
from app.storage import Storage, get_storage
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
    channel: str,
    author_user_id: Optional[int] = None,
    cc: Optional[str] = None,
) -> Message:
    """The one place a Message is built, so the invariants cannot drift: an
    internal note is always agents-only, any new message touches the ticket, and every message records its
    channel (required, so a new way of creating one cannot forget it; tests/test_message_channel.py guards that
    nothing else builds a Message)."""
    if direction == MessageDirection.INTERNAL and visibility != MessageVisibility.AGENTS_ONLY:
        raise ValueError("An internal message must be agents-only")
    if channel not in MESSAGE_CHANNELS:
        raise ValueError(f"Unknown channel {channel}")

    message = Message(
        ticket_id=ticket.id,
        sender=sender,
        body=body,
        direction=direction,
        visibility=visibility,
        author_user_id=author_user_id,
        cc=cc,
        channel=channel,
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
    candidates = copyable_followers(session, ticket)
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
    by_teams: bool = False  # the message would go to the requester's chat with the bot
    also_email: bool = True  # and by email (always, unless it goes to Teams only)
    files: Optional[list] = None  # (name, size) of each file that would be attached, set by the caller that holds them


# The statuses an agent may pick when emailing the requester or adding a note. New is left only by
# triage, Awaiting approval only by the approvers, and Resolved and Closed only by the Resolve
# action (app/resolution.py), so none of those is offered.
STATUS_CHOICES = (
    TicketStatus.OPEN,
    TicketStatus.IN_PROGRESS,
    TicketStatus.AWAITING_USER,
    TicketStatus.WITH_SUPPLIER,
    TicketStatus.WITH_MSP,
    TicketStatus.SCHEDULED,
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
    to Awaiting user. With supplier, With MSP, Scheduled and Awaiting user stay as they are."""
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
    route = route_for(session, ticket, automatic=False, copies_followers=bool(chosen))
    return EmailPreview(
        to=ticket.requester_email,
        cc=[f.email for f in chosen],
        subject=mail_subject(session, ticket),
        about=about_text(ticket) if in_a_split(session, ticket) else None,
        body=body,
        status_after=status_after,
        by_teams=route.teams,
        also_email=route.email,
    )


def in_a_split(session: Session, ticket: Ticket) -> bool:
    """Whether the ticket is a part of a split request, or the original of one."""
    return ticket.split_from_id is not None or session.exec(
        select(Ticket.id).where(Ticket.split_from_id == ticket.id).limit(1)
    ).first() is not None


def email_requester(
    session: Session, ticket: Ticket, author: User, body: str, cc_emails: Optional[list[str]] = None,
    bell: bool = True, files: Optional[list[NewFile]] = None, storage: Optional[Storage] = None,
) -> Message:
    """A public message from an agent to the requester. Records it and queues the email
    (app/outbox.py); the worker sends it (app/mail.py).

    Every follower is copied unless the agent unticks them for this email: pass
    cc_emails as the addresses to copy (None means all of them). Unticking does not
    remove anyone as a follower. Internal followers are agents and are always
    copied. The list is recorded on the message, so the timeline shows exactly who
    received it even if the followers change later. The requester and the followers with an account get a bell
    entry (#130) unless bell is False: the caller then tells them itself, or says something more specific
    (the status it sets next, the resolution).

    `files` are the agent's attachments (already checked by app/reply_files.py): stored on the new message before the
    email is queued, so the email carries them (app/outbox.queue_message), and the Teams copy points to the portal
    (app/teams.requester_message_post). The reply route records where it went on the message (`channel`)."""
    if not is_triaged(ticket):
        raise NotTriaged()
    if not body.strip():
        raise EmptyEmail()

    chosen = _copied_followers(session, ticket, cc_emails)
    cc = ", ".join(f.email for f in chosen) or None
    note_first_response(ticket, datetime.now(timezone.utc))
    # Where it will go is decided now (the same rule `queue_reply` applies below, in this transaction) and recorded on the message.
    route = route_for(session, ticket, automatic=False, copies_followers=bool(cc))
    message = _record(
        session, ticket, sender=author.display_name, body=body, author_user_id=author.id,
        direction=MessageDirection.OUTBOUND, visibility=MessageVisibility.PUBLIC, cc=cc,
        channel=TEAMS_CHANNEL if route.teams else EMAIL,
    )
    attach_files(session, ticket, message, author, files, storage)
    queue_reply(session, message, ticket)
    if bell:
        notify_agent_emailed(session, ticket, author.email)
    return message


def attach_files(
    session: Session, ticket: Ticket, message: Message, author: User, files: Optional[list[NewFile]],
    storage: Optional[Storage] = None,
) -> list:
    """Stores an agent's files on the message they wrote, each through `store_attachment` (the one place a file is checked).
    The files were checked when chosen, so a refusal here is a bug and propagates. Adds rows; the caller commits."""
    if not files:
        return []
    session.flush()  # the message needs its id
    storage = storage or get_storage()
    return [
        store_attachment(session, storage, ticket=ticket, filename=item.filename, data=item.data, message=message, uploaded_by=author)
        for item in files
    ]


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
        direction=MessageDirection.OUTBOUND, visibility=MessageVisibility.PUBLIC, cc=cc, channel=SYSTEM,
    )
    queue_reply(session, message, ticket)
    return message


def add_note(
    session: Session, ticket: Ticket, author: User, body: str, files: Optional[list[NewFile]] = None,
    storage: Optional[Storage] = None,
) -> Message:
    """An internal note. Allowed at any stage and does not triage the ticket. Its files stay with the note: agents only
    (app/visibility.may_view_attachment), never in the portal or an email."""
    note = _record(
        session, ticket, sender=author.display_name, body=body, author_user_id=author.id,
        direction=MessageDirection.INTERNAL, visibility=MessageVisibility.AGENTS_ONLY, channel=AGENT,
    )
    attach_files(session, ticket, note, author, files, storage)
    return note


def record_inbound(
    session: Session,
    ticket: Ticket,
    *,
    sender: str,
    body: str,
    channel: str,
    author_user_id: Optional[int] = None,
    reply_by: Optional[str] = None,
) -> Message:
    """A message from the requester (portal comment, mail, or the Teams bot). Always public: the requester wrote it and
    may see it. `channel` is where it came from (email, portal, teams), or agent for a ticket an agent logged for someone.

    reply_by is the email of the person replying: pass it for a reply on an existing ticket and the
    assigned agent is notified (whatever the status). Leave it out for the message a ticket is
    created with, which tells nobody."""
    message = _record(
        session, ticket, sender=sender, body=body, author_user_id=author_user_id,
        direction=MessageDirection.INBOUND, visibility=MessageVisibility.PUBLIC, channel=channel,
    )
    if reply_by is not None:
        notify_reply(session, ticket, reply_by)
    return message


def record_msp_message(
    session: Session, ticket: Ticket, *, sender: str, body: str, forward: str
) -> Message:
    """A message from the MSP (app/msp.py decides `forward`: "not_needed" when the requester was on the mail, so they
    already have it, or "pending" when the MSP left them off). One that the requester has is public, like any message
    they may see; one they were left off is agents-only until it is forwarded (app/msp.forward_message flips it). It
    is not an agent's email, so it is never queued as mail, and not a first response. The assigned agent is told, as
    for any update from outside."""
    visible = forward not in ("pending", "internal")
    message = _record(
        session, ticket, sender=sender, body=body, direction=MessageDirection.OUTBOUND, channel=EMAIL,
        visibility=MessageVisibility.PUBLIC if visible else MessageVisibility.AGENTS_ONLY,
    )
    message.msp_forward = forward
    if forward != "internal":  # an acknowledgement tells nobody
        notify_reply(session, ticket, sender)
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


def not_requester_visible_messages(session: Session, ticket: Ticket) -> list[Message]:
    """The ticket's own messages the requester may NOT see (internal notes, MSP mail not yet forwarded), the exact
    complement of `requester_visible_messages` for this ticket. For the subject access export only, which lists
    them apart for the data protection lead (app/subject_access.py); never shown or sent to a requester."""
    visible = {m.id for m in requester_visible_messages(session, ticket)}
    return [
        m for m in session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.created_at, Message.id)).all()
        if m.id not in visible
    ]


def requester_visible_answers(session: Session, ticket: Ticket) -> list[dict]:
    """The form answers the requester may see in the portal (label and display text), in the question order. The
    answers are what the requester (or the agent, for them) gave about their own request, so all of them are
    shown; nothing agent-only is stored with them. Kept here with the other requester-visible rules."""
    return [{"label": row["label"], "display": row["display"]} for row in answer_rows(session, ticket)]


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


def kind_label(message: Message, sender_kind: Optional[str] = None) -> str:
    """The words of a message's kind chip on the agent timeline, from where it came from or went (`Message.channel`):
    "Email from requester", "Teams message from requester", "Portal reply", "Email to requester", "Teams reply to
    requester". A message with no recorded channel (an old row that could not be worked out) gets the plain words. Display
    only: who may see the message is decided by `requester_visible_messages`, never by this."""
    who = "follower" if sender_kind == "follower" else "requester"
    channel = message.channel
    if message.direction == MessageDirection.OUTBOUND:
        return {
            EMAIL: "Email to requester", TEAMS_CHANNEL: "Teams reply to requester", SYSTEM: "Automatic message to requester",
        }.get(channel, "Message to requester")
    if channel == EMAIL:
        return f"Email from {who}"
    if channel == TEAMS_CHANNEL:
        return f"Teams message from {who}"
    if channel == PORTAL:
        return "Portal reply" if who == "requester" else "Portal reply from follower"
    if channel == AGENT:
        return "Logged by an agent"
    return f"From {who}"


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

    @property
    def label(self) -> str:
        return kind_label(self.message, self.sender_kind) if self.message is not None else ""


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
        if message.msp_forward is not None:
            return "msp"
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
