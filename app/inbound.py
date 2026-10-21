"""Inbound mail (phase 3B): turning a mail in the shared mailbox into a ticket or a reply.

Three layers, so each is tested on its own:
- `process_notifications` is the worker job. It takes the rows the webhook stored (`GraphNotification`),
  calls `process_message` for each, and backs off and gives up on the ones Graph will not give us.
- `process_message(session, graph_message_id)` is the seam. It fetches one message and its files from
  Graph and hands them on. The delta catch-up job (3C) calls it too, for every message it finds that a
  notification may have missed, so it must be, and is, safe to call twice for the same message.
- `ingest_mail` is the decision, and knows nothing of Graph: skip it if it is a duplicate, our own mail
  or an automatic reply; add it to the ticket it belongs to; or open a new one. The dev helper
  (`/webhooks/dev/simulate-inbound`) calls this same function.

Rules that live here and nowhere else:
- A mail is handled once. `InboundMail` has a unique Graph id and a unique Message-ID header, claimed
  before anything else is done, in the same transaction as the ticket, so concurrent callers (the
  notification job and the catch-up) cannot both make a ticket and a failed run leaves no half-done mail.
- Our own mail and automatic replies (Auto-Submitted, X-Auto-Response-Suppress, bulk and list mail,
  bounces) never become tickets or replies. This is the loop guard on the inbound side; the outbound
  side (the per-ticket cap, never mailing the mailbox itself) is in app/mail.py. A sender who produces
  more than inbound_max_per_sender_hour (an admin setting) tickets and replies in an hour is set aside as a probable loop.
- Which ticket a mail belongs to: the Graph conversation of an earlier mail, then the Message-ID the
  mail replies to, then the [INC-1042] reference in the subject. The requester and followers may always
  add to a ticket. Someone else may only when the match is a STRONG one (the Graph conversation or the
  Message-ID answered, which prove the mail is part of the real thread, for example a colleague copied
  midway who replies-all): the mail is then added to the ticket under their address and they become a
  follower (an event says so). A match on the subject reference alone is not proof, since the reference
  is printed on every email and anyone who saw one could otherwise write on that ticket and become a
  follower: that sender's mail opens a ticket of its own (with an event saying why). If the ticket
  already has its full number of external followers, the stranger is not added and gets a ticket too.
- A reply is recorded through `record_inbound`, so what the requester may see stays in app/messages.py,
  and its status effect is the portal's (`apply_requester_reply`). A reply on a Resolved
  ticket reopens it (apply_requester_reply). A reply on a Closed ticket opens a new ticket linked to it
  (app/reopening.py), and later mail in that thread follows the link to the newest ticket. Who can see the ticket is app/visibility.py's
  business, not this module's: it never decides access.
- The people copied on the mail that opens a ticket become its followers (app/followers.py). A reply
  is passed on to the followers who were not on its To or Cc (app/follower_updates.py).
- Files go through `store_attachment` (app/attachments.py), the only place a file is checked. A file
  that is refused, too large or beyond the limit leaves an event on the ticket, never an error.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.attachments import AttachmentRejected, clean_filename, store_attachment
from app.audit import log_action
from app.config import settings
from app.settings_store import INBOUND_MAX_ATTACHMENTS, INBOUND_MAX_CC, INBOUND_MAX_PER_SENDER_HOUR, count_setting
from app.follower_updates import queue_follower_updates
from app.reopening import latest_follow_on, open_follow_on_ticket
from app.followers import (
    InvalidEmail,
    NeverFollowListed,
    TooManyFollowers,
    add_follower,
    bare_address,
    is_follower,
    normalise_email,
)
from app.forms import MAX_MESSAGE_LENGTH
from app.graph_client import graph_client
from app.mail import describe_failure
from app.messages import record_inbound
from app.models import (
    TICKET_TYPE_PREFIXES,
    GraphNotification,
    InboundMail,
    Message,
    Ticket,
    TicketSource,
    TicketStatus,
    User,
)
from app.never_follow import is_listed
from app.outbox import is_on, queue_acknowledgement
from app.raising import MAX_SUBJECT_LENGTH
from app.splitting import apply_requester_reply
from app.storage import Storage, get_storage
from app.teams import MAX_ATTEMPTS, MAX_BACKOFF_MINUTES
from app.ticket_service import create_ticket, get_by_number, record_event

log = logging.getLogger("app.inbound")

# What came of a mail.
TICKET_CREATED = "ticket_created"
REPLY_ADDED = "reply_added"
DUPLICATE = "duplicate"  # already handled: nothing was done this time
GONE = "gone"  # Graph no longer has the message
IGNORED_AUTOMATIC = "ignored_automatic"
IGNORED_OWN = "ignored_own"
IGNORED_NO_SENDER = "ignored_no_sender"
IGNORED_FLOOD = "ignored_flood"
IGNORED_DRAFT = "ignored_draft"
HANDLED = (TICKET_CREATED, REPLY_ADDED)

EVENT_ATTACHMENT_SKIPPED = "attachment_skipped"
EVENT_REFERENCE_NOT_USED = "mail_reference_not_used"
EVENT_FOLLOWER_FROM_MAIL = "follower_added_from_mail"
STRONG_MATCHES = ("conversation", "in-reply-to")  # the thread itself, not just a reference in the subject

BATCH = 25
NO_SUBJECT = "(no subject)"
NO_TEXT = "(no message text)"
CUT = "\n\n[cut short]"
MAX_STORED_RECIPIENTS = 100  # of each of To and Cc kept on the record of a mail
MAX_ID = 998  # a Message-ID header is at most a line long

# Headers whose presence marks a machine-made mail. A value of "no" for Auto-Submitted means a person.
_AUTO_SUPPRESS_TOKENS = {"all", "oof", "autoreply"}
_AUTO_PRECEDENCE = {"bulk", "junk", "list", "auto_reply", "auto-reply"}
_AUTOMATIC_HEADERS = ("x-autoreply", "x-autorespond", "list-id", "list-unsubscribe")
_AUTOMATIC_SENDERS = ("mailer-daemon", "postmaster")

_REFERENCE = re.compile(
    r"\[(?:" + "|".join(sorted(TICKET_TYPE_PREFIXES.values())) + r")-([1-9][0-9]{0,8})\]", re.IGNORECASE
)


@dataclass
class IncomingAttachment:
    filename: str
    data: Optional[bytes] = None  # None when skipped
    inline: bool = False
    skipped: Optional[str] = None  # why it was not fetched


@dataclass
class IncomingMail:
    """A mail as the intake sees it, whatever it came through: Graph, or the dev helper."""

    graph_message_id: str
    sender: str  # the From address, lower case, "" if there is none
    subject: str = ""
    body: str = ""
    internet_message_id: Optional[str] = None  # normalised, see _message_id
    conversation_id: Optional[str] = None
    cc: list[str] = field(default_factory=list)
    to: list[str] = field(default_factory=list)  # with cc: who the mail was sent to, so who already has it
    headers: dict[str, str] = field(default_factory=dict)  # names lower case
    in_reply_to: list[str] = field(default_factory=list)  # normalised Message-IDs, nearest first
    is_draft: bool = False
    has_attachments: bool = False


@dataclass
class Handled:
    outcome: str
    ticket: Optional[Ticket] = None
    message: Optional[Message] = None


class MailSource(Protocol):
    """What the intake needs from Graph (app/graph_client.py is the real one; tests pass a fake)."""

    def get_message(self, message_id: str) -> Optional[dict]: ...
    def get_attachments(self, message_id: str) -> list[dict]: ...
    def get_attachment_bytes(self, message_id: str, attachment_id: str) -> Optional[bytes]: ...


# ---- Reading a Graph message -------------------------------------------------------------------


def _text(value) -> str:
    """Text safe to store: Postgres refuses a NUL, and other control characters are only noise."""
    if not isinstance(value, str):
        return ""
    # Only control characters (NUL included) and lone surrogates go; spaces such as U+00A0 and joiners stay.
    return "".join(c for c in value if c in "\n\t\r" or unicodedata.category(c) not in ("Cc", "Cs"))


def _message_id(value) -> Optional[str]:
    """A Message-ID as stored and compared: no angle brackets or spaces, lower case."""
    if not isinstance(value, str):
        return None
    cleaned = _text(value).strip().strip("<>").strip().lower()[:MAX_ID]
    return cleaned or None


def _address(entry) -> str:
    address = entry.get("emailAddress", {}).get("address") if isinstance(entry, dict) else None
    return bare_address(address) if isinstance(address, str) else ""  # never a display name, whatever Graph sent


def parse_graph_message(raw: dict) -> IncomingMail:
    """A Graph message (the fields `GraphClient.get_message` selects) as an IncomingMail. Anything
    missing or oddly shaped becomes empty rather than an error: a mail we cannot read is ignored, it
    does not stop the ones behind it."""
    body = raw.get("uniqueBody") or raw.get("body") or {}
    content = body.get("content") if isinstance(body, dict) else None
    headers: dict[str, str] = {}
    for header in raw.get("internetMessageHeaders") or []:
        if isinstance(header, dict) and isinstance(header.get("name"), str):
            # The first of a repeated header is the nearest one to us.
            headers.setdefault(header["name"].strip().lower(), _text(header.get("value")).strip())
    references = re.findall(r"<[^<>\s]+>", headers.get("in-reply-to", ""))
    references = references or ([headers["in-reply-to"]] if headers.get("in-reply-to") else [])
    references += re.findall(r"<[^<>\s]+>", headers.get("references", ""))[::-1]  # nearest first
    sender = _address(raw.get("from")) or _address(raw.get("sender"))
    return IncomingMail(
        graph_message_id=str(raw.get("id") or ""),
        sender=sender,
        subject=_text(raw.get("subject")),
        body=_text(content),
        internet_message_id=_message_id(raw.get("internetMessageId")),
        conversation_id=_text(raw.get("conversationId")) or None,
        cc=[a for a in (_address(e) for e in raw.get("ccRecipients") or []) if a],
        to=[a for a in (_address(e) for e in raw.get("toRecipients") or []) if a],
        headers=headers,
        in_reply_to=[m for m in (_message_id(r) for r in references) if m],
        is_draft=bool(raw.get("isDraft")),
        has_attachments=bool(raw.get("hasAttachments")),
    )


def load_attachments(session: Session, client: MailSource, graph_message_id: str) -> list[IncomingAttachment]:
    """The files on a message, fetched within the limits (settings, TBC): how many, and how large each
    is. One over a limit is listed as skipped, with why, and never downloaded. app/attachments.py then
    decides what may be stored."""
    limit_mb = settings.attachment_max_bytes // (1024 * 1024)
    max_files = count_setting(session, INBOUND_MAX_ATTACHMENTS)
    found: list[IncomingAttachment] = []
    for index, item in enumerate(client.get_attachments(graph_message_id)):
        if not isinstance(item, dict):
            continue
        name = clean_filename(_text(item.get("name")))
        inline = bool(item.get("isInline"))
        if index >= max_files:
            found.append(IncomingAttachment(name, skipped=f"more than {max_files} files on one email", inline=inline))
        elif item.get("@odata.type") != "#microsoft.graph.fileAttachment":
            found.append(IncomingAttachment(name, skipped="not a file (an attached email or a link)", inline=inline))
        elif not isinstance(item.get("size"), int) or item["size"] > settings.attachment_max_bytes:
            found.append(IncomingAttachment(name, skipped=f"larger than the {limit_mb} MB limit", inline=inline))
        else:
            data = client.get_attachment_bytes(graph_message_id, str(item.get("id")))
            if data is None:
                found.append(IncomingAttachment(name, skipped="no longer available", inline=inline))
            elif len(data) > settings.attachment_max_bytes:
                found.append(IncomingAttachment(name, skipped=f"larger than the {limit_mb} MB limit", inline=inline))
            else:
                found.append(IncomingAttachment(name, data=data, inline=inline))
    return found


# ---- Deciding ----------------------------------------------------------------------------------


def automatic_reason(mail: IncomingMail) -> Optional[str]:
    """Why this mail is not from a person, or None. The inbound loop guard: an automatic reply, a
    bounce, a mailing list, our own mail. Nothing here becomes a ticket or wakes one."""
    own = settings.graph_shared_mailbox.strip().lower()
    if own and mail.sender == own:
        return IGNORED_OWN
    if mail.is_draft:
        return IGNORED_DRAFT
    headers = mail.headers
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return IGNORED_AUTOMATIC
    suppress = {t.strip().lower() for t in headers.get("x-auto-response-suppress", "").split(",")}
    if suppress & _AUTO_SUPPRESS_TOKENS:
        return IGNORED_AUTOMATIC
    if headers.get("precedence", "").strip().lower() in _AUTO_PRECEDENCE:
        return IGNORED_AUTOMATIC
    if any(name in headers for name in _AUTOMATIC_HEADERS):
        return IGNORED_AUTOMATIC
    if headers.get("return-path", "").strip() == "<>" or "multipart/report" in headers.get("content-type", "").lower():
        return IGNORED_AUTOMATIC  # a bounce or delivery report
    if mail.sender.partition("@")[0] in _AUTOMATIC_SENDERS:
        return IGNORED_AUTOMATIC
    return None


def _ticket_of_conversation(session: Session, conversation_id: Optional[str]) -> Optional[Ticket]:
    if not conversation_id:
        return None
    ticket_id = session.exec(
        select(InboundMail.ticket_id)
        .where(InboundMail.conversation_id == conversation_id, InboundMail.ticket_id.is_not(None))
        .order_by(InboundMail.id)  # the first mail of the thread: a later stranger's mail must not take it over
        .limit(1)
    ).first()
    return session.get(Ticket, ticket_id) if ticket_id else None


def _ticket_of_message_ids(session: Session, message_ids: list[str]) -> Optional[Ticket]:
    for message_id in message_ids[:10]:
        ticket_id = session.exec(
            select(InboundMail.ticket_id).where(
                InboundMail.internet_message_id == message_id, InboundMail.ticket_id.is_not(None)
            )
        ).first()
        if ticket_id:
            return session.get(Ticket, ticket_id)
    return None


def referenced_ticket(session: Session, subject: str) -> Optional[Ticket]:
    """The ticket named by the first [INC-1042]-style reference in a subject. Only the bracketed form
    counts (it is what app/messages.py mail_subject writes), and only the number, as everywhere."""
    match = _REFERENCE.search(subject)
    return get_by_number(session, int(match.group(1))) if match else None


def find_ticket_for(session: Session, mail: IncomingMail) -> tuple[Optional[Ticket], Optional[str]]:
    """The ticket this mail looks like part of, and how it was found. Not yet a decision: whether the
    sender may add to it is checked by the caller."""
    ticket = _ticket_of_conversation(session, mail.conversation_id)
    if ticket is not None:
        return ticket, "conversation"
    ticket = _ticket_of_message_ids(session, mail.in_reply_to)
    if ticket is not None:
        return ticket, "in-reply-to"
    ticket = referenced_ticket(session, mail.subject)
    if ticket is not None:
        return ticket, "reference"
    return None, None


def may_reply(session: Session, ticket: Ticket, sender: str) -> bool:
    """Only the requester or a follower may add to a ticket by mail. An agent copied as an internal
    follower is not one: agents answer from the ticket page."""
    return sender == ticket.requester_email.lower() or is_follower(session, ticket, sender, include_internal=False)


def _join_thread(session: Session, ticket: Ticket, sender: str) -> bool:
    """A person midway through the ticket's real thread writes to it: make them a follower so their mail
    can be recorded as a reply. False (nothing changed) for the shared mailbox, an existing follower
    (an agent copied as an internal follower answers from the ticket page) or a full follower list."""
    if sender == settings.graph_shared_mailbox.strip().lower():
        return False
    try:
        follower = add_follower(session, ticket, sender, sender)
    except (InvalidEmail, TooManyFollowers, NeverFollowListed):
        return False
    if follower is None:
        return False
    record_event(
        session, ticket, EVENT_FOLLOWER_FROM_MAIL, sender, to_value=sender,
        detail="Replied in the mail thread without being on the ticket, so now follows it",
    )
    return True


def _subject_of(mail: IncomingMail) -> str:
    subject = " ".join(mail.subject.split())
    return subject[:MAX_SUBJECT_LENGTH] if subject else NO_SUBJECT


def _body_of(mail: IncomingMail) -> str:
    body = mail.body.strip()
    if not body:
        return NO_TEXT
    return body if len(body) <= MAX_MESSAGE_LENGTH else body[: MAX_MESSAGE_LENGTH - len(CUT)].rstrip() + CUT


def _has_real_attachment(mail: IncomingMail, files: list[IncomingAttachment]) -> bool:
    """Whether the mail carries a file someone attached. A picture embedded in the body (a signature logo)
    does not count once the attachments have been read; until then Graph's hasAttachments flag decides."""
    if files:
        return any(not item.inline for item in files)
    return mail.has_attachments


def _record_reply(session: Session, ticket: Ticket, sender: str, body: str, author_id: Optional[int]) -> Message:
    return record_inbound(session, ticket, sender=sender, body=body, author_user_id=author_id, reply_by=sender)


def _store_files(
    session: Session, storage: Optional[Storage], ticket: Ticket, message: Message, files: list[IncomingAttachment]
) -> None:
    storage = storage or (get_storage() if any(item.data is not None for item in files) else None)
    for item in files:
        reason = item.skipped
        if reason is None and item.data is not None:
            try:
                store_attachment(
                    session, storage, ticket=ticket, filename=item.filename, data=item.data,
                    message=message, inline=item.inline,
                )
            except AttachmentRejected as error:
                reason = str(error)
        if reason is not None:
            record_event(session, ticket, EVENT_ATTACHMENT_SKIPPED, None, to_value=item.filename[:200], detail=reason)


def _flooding(session: Session, sender: str, now: datetime) -> bool:
    limit = count_setting(session, INBOUND_MAX_PER_SENDER_HOUR)
    count = session.exec(
        select(func.count()).select_from(InboundMail).where(
            InboundMail.sender == sender,
            InboundMail.outcome.in_(HANDLED),
            InboundMail.created_at >= now - timedelta(hours=1),
        )
    ).one()
    return count >= limit


def _claim(session: Session, mail: IncomingMail) -> Optional[InboundMail]:
    """Records that this mail is being handled, or returns None if it already was (by the same Graph id
    or the same Message-ID). The unique indexes decide, so two callers at once cannot both win: the loser
    waits for the winner's commit and then fails here."""
    row = InboundMail(
        graph_message_id=mail.graph_message_id[:MAX_ID],
        internet_message_id=mail.internet_message_id,
        conversation_id=mail.conversation_id[:MAX_ID] if mail.conversation_id else None,
        sender=mail.sender[:254] or None,
        to_addresses=", ".join(mail.to[:MAX_STORED_RECIPIENTS]) or None,
        cc_addresses=", ".join(mail.cc[:MAX_STORED_RECIPIENTS]) or None,
        outcome="processing",
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return None
    return row


def ingest_mail(
    session: Session,
    mail: IncomingMail,
    attachments: Optional[list[IncomingAttachment]] = None,
    storage: Optional[Storage] = None,
    now: Optional[datetime] = None,
) -> Handled:
    """Handles one mail: nothing (a duplicate, automatic or our own), a reply on a ticket, or a new
    ticket. Adds everything to the session; the caller commits, so the mail, its ticket, message,
    files and the record that it was handled stand or fall together."""
    now = now or datetime.now(timezone.utc)
    claim = _claim(session, mail)
    if claim is None:
        return Handled(DUPLICATE)

    def settle(outcome: str, ticket: Optional[Ticket] = None, message: Optional[Message] = None) -> Handled:
        claim.outcome = outcome
        claim.ticket_id = ticket.id if ticket is not None else None
        claim.message_id = message.id if message is not None else None
        session.add(claim)
        return Handled(outcome, ticket, message)

    reason = automatic_reason(mail)
    if reason is not None:
        return settle(reason)
    try:
        sender = normalise_email(mail.sender)
    except InvalidEmail:
        return settle(IGNORED_NO_SENDER)
    if _flooding(session, sender, now):
        log.warning("Mail from one sender set aside: too many tickets and replies in an hour (a probable loop)")
        return settle(IGNORED_FLOOD)

    body = _body_of(mail)
    files = attachments or []
    account = session.exec(select(User).where(func.lower(User.email) == sender)).first()
    author_id = account.id if account is not None else None

    ticket, how = find_ticket_for(session, mail)
    closed = None
    if ticket is not None:
        # A thread that already carried on in a ticket opened after closure goes on in that one.
        newer = latest_follow_on(session, ticket)
        if newer.id != ticket.id and may_reply(session, newer, sender):
            ticket = newer  # not when it is someone else's ticket: the sender then gets one of their own
        if ticket.status == TicketStatus.CLOSED and (may_reply(session, ticket, sender) or how in STRONG_MATCHES):
            # A reply after closure is not added to the closed ticket: it opens a new one, linked to it
            # (below). Nobody joins the closed ticket as a follower.
            closed, ticket = ticket, None
    allowed = ticket is not None and may_reply(session, ticket, sender)
    joined = False
    if ticket is not None and not allowed and how in STRONG_MATCHES:
        allowed = joined = _join_thread(session, ticket, sender)
    if ticket is not None and allowed:
        message = _record_reply(session, ticket, sender, body, author_id)
        message.graph_message_id = mail.graph_message_id
        session.flush()
        moved = apply_requester_reply(
            session, ticket, sender, text=mail.body, has_attachment=_has_real_attachment(mail, files)
        )
        log_action(session, sender, "ticket", ticket.id, "replied_by_email", f"matched by {how}")
        if moved:
            log_action(session, sender, "ticket", ticket.id, "status_changed", f"{moved[0]} -> {moved[1]}")
        _store_files(session, storage, ticket, message, files)
        # Whoever follows the ticket and was not on this mail hears of it (the sender and those on
        # the mail already have it).
        queue_follower_updates(session, ticket, message, sender, copied=mail.to + mail.cc)
        return settle(REPLY_ADDED, ticket, message)

    if closed is not None:
        new = open_follow_on_ticket(
            session, closed, subject=_subject_of(mail), sender_email=sender, source=TicketSource.EMAIL,
            actor_email=None,
        )
    else:
        new = create_ticket(session, subject=_subject_of(mail), requester_email=sender, source=TicketSource.EMAIL)
    if ticket is not None:
        record_event(
            session, new, EVENT_REFERENCE_NOT_USED, None, to_value=ticket.tag,
            detail="The subject or thread names that ticket, but the sender is not its requester or a follower",
        )
    message = record_inbound(session, new, sender=sender, body=body, author_user_id=author_id)
    message.graph_message_id = mail.graph_message_id
    session.flush()
    log_action(session, sender, "ticket", new.id, "created_by_email")
    mailbox = settings.graph_shared_mailbox.strip().lower()
    for address in mail.cc[: count_setting(session, INBOUND_MAX_CC)]:
        if address in (sender, mailbox):
            continue
        try:
            add_follower(session, new, address, sender)
        except (InvalidEmail, NeverFollowListed):
            continue
        except TooManyFollowers:
            break
    _store_files(session, storage, new, message, files)
    # A receipt to a real person only: automatic mail, bounces, our own mail and floods were set
    # aside above, and a never-follow address (a list or shared mailbox) is not answered either.
    if is_on() and not is_listed(session, sender) and sender == new.requester_email:
        queue_acknowledgement(session, new)
    return settle(TICKET_CREATED, new, message)


def process_message(
    session: Session, graph_message_id: str, client: Optional[MailSource] = None, storage: Optional[Storage] = None
) -> Handled:
    """Reads one message from the shared mailbox and handles it (see ingest_mail). The entry point the
    catch-up job (3C) uses. Safe to call twice and from two places at once. Does not commit. Raises if
    Graph fails (the caller retries); a message Graph no longer has is `GONE`."""
    client = client or graph_client
    if session.exec(select(InboundMail.id).where(InboundMail.graph_message_id == graph_message_id)).first():
        return Handled(DUPLICATE)  # the cheap check: no need to fetch a mail we have
    raw = client.get_message(graph_message_id)
    if raw is None:
        return Handled(GONE)
    mail = parse_graph_message(raw)
    mail.graph_message_id = graph_message_id  # the id we asked for is the one we dedupe on
    files: list[IncomingAttachment] = []
    if mail.has_attachments and automatic_reason(mail) is None and not _flooding(session, mail.sender, datetime.now(timezone.utc)):
        files = load_attachments(session, client, graph_message_id)  # before any ticket is made: no lock is held while Graph answers
    return ingest_mail(session, mail, files, storage)


# ---- The worker job ----------------------------------------------------------------------------


def _due(session: Session, now: datetime) -> list[int]:
    return list(
        session.exec(
            select(GraphNotification.id)
            .where(
                GraphNotification.processed_at.is_(None),
                GraphNotification.lifecycle_event.is_(None),  # subscription housekeeping is 3C's
                GraphNotification.attempts < MAX_ATTEMPTS,
                or_(GraphNotification.next_attempt_at.is_(None), GraphNotification.next_attempt_at <= now),
            )
            .order_by(GraphNotification.id)
            .limit(BATCH)
        ).all()
    )


def process_notifications(
    session: Session,
    client: Optional[MailSource] = None,
    storage: Optional[Storage] = None,
    now: Optional[datetime] = None,
    on_handled: Optional[Callable[[Handled], None]] = None,
) -> tuple[int, int]:
    """Handles the mail notifications the webhook stored, oldest first, committing after each so a mail
    that was handled is recorded even if the next one fails. Returns (done, failed). A notification that
    is not about a new message is simply marked done. A failure backs off like the other outboxes and,
    after MAX_ATTEMPTS, is marked done with the reason: the catch-up job (3C) reads the mailbox itself and
    will find the mail."""
    now = now or datetime.now(timezone.utc)
    done = failed = 0
    for notification_id in _due(session, now):
        notification = session.exec(
            select(GraphNotification).where(GraphNotification.id == notification_id).with_for_update(skip_locked=True)
        ).first()
        if notification is None or notification.processed_at is not None:
            continue  # another worker has it, or it was just done
        try:
            if notification.change_type == "created" and notification.resource_id:
                with session.begin_nested():
                    result = process_message(session, notification.resource_id, client, storage)
                if on_handled is not None:
                    on_handled(result)
            notification.processed_at = datetime.now(timezone.utc)
            notification.last_error = None
            done += 1
        except Exception as error:
            log.warning("Mail notification %s failed: %s", notification_id, describe_failure(error))
            session.rollback()
            notification = session.get(GraphNotification, notification_id)
            notification.attempts += 1
            notification.last_error = describe_failure(error)
            if notification.attempts >= MAX_ATTEMPTS:
                notification.processed_at = datetime.now(timezone.utc)
                notification.last_error = f"Gave up after {MAX_ATTEMPTS} tries: {notification.last_error}"
            else:
                notification.next_attempt_at = now + timedelta(minutes=min(2 ** (notification.attempts - 1), MAX_BACKOFF_MINUTES))
            failed += 1
        session.add(notification)
        session.commit()
    return done, failed
